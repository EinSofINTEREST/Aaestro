"""Artifact store backends.

``ArtifactStore`` 계약(``core/skill.py``)의 구현. 저장소는 **참조를 돌려주고**
호출자는 그 참조만 들고 다닌다 — 경로를 그대로 노출하면 컨테이너가 호스트
레이아웃을 안다고 가정하게 되고, backend 를 바꾸는 순간 참조가 깨진다.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

if TYPE_CHECKING:
    from collections.abc import Iterable

SCHEME = "artifact://"
"""참조 접두사 — 값이 참조임을 한눈에 드러낸다."""

_REF_PATTERN = re.compile(r"^artifact://(?P<scope>[a-z0-9-]+)/(?P<key>[A-Za-z0-9._/-]+)$")

_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
"""저장 key 로 허용하는 모양 — 경로 탈출과 숨김 파일을 막는다."""


def storage_error(message: str, **details: Any) -> AaestroError:
    """Artifact 저장소 실패를 STORAGE 카테고리로."""
    return AaestroError(
        category=ErrorCategory.STORAGE,
        code=ErrorCode.STOR_003,
        message=message,
        details=details,
    )


def invalid_key(key: str, reason: str) -> AaestroError:
    """받아들일 수 없는 key — 저장 전에 막는다."""
    return AaestroError(
        category=ErrorCategory.VALIDATION,
        code=ErrorCode.VAL_002,
        message=f"invalid artifact key: {reason}",
        details={"key": key},
    )


@dataclass(frozen=True)
class ArtifactRef:
    """A stored artifact's opaque reference.

    저장된 산출물의 참조. state 에 실려 다음 노드로 건너간다.
    """

    scope: str
    key: str

    def __str__(self) -> str:
        """``artifact://{scope}/{key}`` — 이 문자열이 계약이다."""
        return f"{SCHEME}{self.scope}/{self.key}"


def parse_ref(raw: str) -> ArtifactRef:
    """Parse an artifact reference.

    참조 문자열을 해석합니다.

    Args:
        raw: The ``artifact://scope/key`` string.

    Returns:
        The parsed reference.

    Raises:
        AaestroError: VALIDATION/``VAL_002`` if the reference is malformed.
    """
    match = _REF_PATTERN.match(raw)
    if match is None:
        raise invalid_key(raw, "reference must be artifact://scope/key")
    # 참조 안의 key 도 같은 검사를 통과해야 한다 — 읽기 경로로 탈출당하면
    # 쓰기를 막은 의미가 없다
    return ArtifactRef(scope=match["scope"], key=validate_key(match["key"]))


def validate_key(key: str) -> str:
    """Reject keys that would escape the store root.

    저장 루트를 벗어나는 key 를 거부합니다.

    Args:
        key: The requested key.

    Returns:
        The key, unchanged, when it is safe.

    Raises:
        AaestroError: VALIDATION/``VAL_002`` for traversal or malformed keys.
    """
    if not key:
        raise invalid_key(key, "key must not be empty")
    segments = key.split("/")
    if ".." in segments:
        # `../` 한 조각이면 루트 밖 어디든 쓸 수 있다
        raise invalid_key(key, "key must not traverse upwards")
    if "" in segments:
        # `a//b` 는 `a/b` 와 같은 파일이 된다 — 두 key 가 조용히 충돌한다
        raise invalid_key(key, "key must not contain empty segments")
    if "." in segments:
        raise invalid_key(key, "key must not contain '.' segments")
    if key.startswith("/"):
        raise invalid_key(key, "key must be relative")
    if not _SAFE_KEY.match(key):
        raise invalid_key(key, "key has characters outside [A-Za-z0-9._/-]")
    return key


@dataclass
class FilesystemArtifactStore:
    """Stores artifacts under a root directory.

    루트 디렉토리 아래 산출물을 보관합니다. 스코프마다 하위 디렉토리를 두어
    같은 key 가 스코프를 넘어 충돌하지 않게 합니다.

    Attributes:
        root: 저장 루트.
        scope: 이 저장소가 쓰는 스코프 이름 — 참조에 실립니다.
    """

    root: Path
    scope: str = "local"

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    async def put(self, key: str, data: bytes) -> str:
        """Store an artifact and return its reference.

        산출물을 저장하고 참조를 돌려줍니다.

        Args:
            key: Caller-chosen name within this scope.
            data: The bytes to store.

        Returns:
            The ``artifact://`` reference.

        Raises:
            AaestroError: VALIDATION/``VAL_002`` for an unsafe key,
                STORAGE/``STOR_003`` if the write fails.
        """
        target = self._path_for(validate_key(key))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError as err:
            raise storage_error(
                "artifact could not be stored", key=key, cause=type(err).__name__
            ) from err
        return str(ArtifactRef(scope=self.scope, key=key))

    async def get(self, ref: str) -> bytes:
        """Read an artifact by reference.

        참조로부터 산출물을 읽습니다.

        Args:
            ref: The ``artifact://`` reference.

        Returns:
            The stored bytes.

        Raises:
            AaestroError: VALIDATION/``VAL_002`` for a malformed reference,
                NOT_FOUND/``NF_001`` if it is unknown,
                STORAGE/``STOR_003`` if the read fails.
        """
        parsed = parse_ref(ref)
        if parsed.scope != self.scope:
            # 다른 스코프의 참조를 이 저장소가 읽어주면 스코프 경계가 무의미해진다
            raise AaestroError(
                category=ErrorCategory.NOT_FOUND,
                code=ErrorCode.NF_001,
                message="artifact belongs to another scope",
                details={"ref": ref, "scope": self.scope},
            )

        target = self._path_for(parsed.key)
        try:
            return target.read_bytes()
        except FileNotFoundError as err:
            raise AaestroError(
                category=ErrorCategory.NOT_FOUND,
                code=ErrorCode.NF_001,
                message="unknown artifact",
                details={"ref": ref},
            ) from err
        except OSError as err:
            raise storage_error(
                "artifact could not be read", ref=ref, cause=type(err).__name__
            ) from err

    def _path_for(self, key: str) -> Path:
        """key 를 저장 경로로 — 검증된 key 만 들어온다."""
        resolved = (self.root / self.scope / key).resolve()
        root = (self.root / self.scope).resolve()
        if not resolved.is_relative_to(root):
            # 검증을 통과해도 심볼릭 링크로 벗어날 수 있다 — 마지막 방어선
            raise invalid_key(key, "key resolves outside the store root")
        return resolved

    def stored_keys(self) -> Iterable[str]:
        """저장된 key 목록 — 운영 점검용.

        ``keys`` 로 두면 dict 처럼 읽혀 오해를 부른다 (린터도 그렇게 읽는다).
        """
        base = self.root / self.scope
        if not base.exists():
            return []
        return sorted(str(path.relative_to(base)) for path in base.rglob("*") if path.is_file())

    def used_bytes(self) -> int:
        """이 스코프가 차지한 바이트 — quota 검증에 쓴다."""
        base = self.root / self.scope
        if not base.exists():
            return 0
        return sum(path.stat().st_size for path in base.rglob("*") if path.is_file())


def digest_key(prefix: str, data: bytes) -> str:
    """내용 기반 key — 같은 산출물을 두 번 저장하지 않으려는 호출자용."""
    return f"{prefix}/{hashlib.sha256(data).hexdigest()[:32]}"


__all__ = [
    "SCHEME",
    "ArtifactRef",
    "FilesystemArtifactStore",
    "digest_key",
    "invalid_key",
    "parse_ref",
    "storage_error",
    "validate_key",
]
