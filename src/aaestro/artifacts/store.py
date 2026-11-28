"""Artifact store backends.

``ArtifactStore`` 계약(``core/skill.py``)의 구현. 저장소는 **참조를 돌려주고**
호출자는 그 참조만 들고 다닌다 — 경로를 그대로 노출하면 컨테이너가 호스트
레이아웃을 안다고 가정하게 되고, backend 를 바꾸는 순간 참조가 깨진다.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.modules.registry import inside

if TYPE_CHECKING:
    from collections.abc import Iterable

SCHEME = "artifact://"
"""참조 접두사 — 값이 참조임을 한눈에 드러낸다."""

_REF_PATTERN = re.compile(r"^artifact://(?P<scope>[a-z0-9-]+)/(?P<key>[A-Za-z0-9._/-]+)$")

TEMP_PREFIX = ".tmp-"
"""원자적 쓰기의 임시 파일 접두사 — 읽기에서도 key 로 받지 않는다."""

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


def invalid_key(reason: str) -> AaestroError:
    """받아들일 수 없는 key — 저장 전에 막는다. 받은 값은 싣지 않는다 (#412)."""
    return AaestroError(
        category=ErrorCategory.VALIDATION,
        code=ErrorCode.VAL_002,
        message=f"invalid artifact key: {reason}",
        details={"reason": reason},
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
    match = _REF_PATTERN.fullmatch(raw)
    if match is None:
        raise invalid_key("reference must be artifact://scope/key")
    # 참조 안의 key 도 같은 검사를 통과해야 한다 — 읽기 경로로 탈출당하면
    # 쓰기를 막은 의미가 없다
    return ArtifactRef(scope=match["scope"], key=validate_key(match["key"]))


def validate_key(key: str, *, for_write: bool = False) -> str:
    """Reject keys that would escape the store root.

    저장 루트를 벗어나는 key 를 거부합니다.

    Args:
        key: The requested key.
        for_write: Also reject segments starting with ``.`` — 새 규칙은 쓰기에만
            적용한다. 예전에 저장된 참조를 읽는 길은 막지 않는다.

    Returns:
        The key, unchanged, when it is safe.

    Raises:
        AaestroError: VALIDATION/``VAL_002`` for traversal or malformed keys.
    """
    if not key:
        raise invalid_key("key must not be empty")
    segments = key.split("/")
    if ".." in segments:
        # `../` 한 조각이면 루트 밖 어디든 쓸 수 있다
        raise invalid_key("key must not traverse upwards")
    if "" in segments:
        # `a//b` 는 `a/b` 와 같은 파일이 된다 — 두 key 가 조용히 충돌한다
        raise invalid_key("key must not contain empty segments")
    if "." in segments:
        raise invalid_key("key must not contain '.' segments")
    if any(segment.startswith(TEMP_PREFIX) for segment in segments):
        # 쓰는 중인 임시 파일을 읽거나 덮지 못하게 한다
        raise invalid_key("key segments must not look like temporary files")
    if for_write and any(segment.startswith(".") for segment in segments):
        # 점으로 시작하는 이름은 임시 파일 몫이다 — 새로 쓰는 key 는 쓰지 않는다
        raise invalid_key("key segments must not start with '.'")
    if key.startswith("/"):
        raise invalid_key("key must be relative")
    if not _SAFE_KEY.fullmatch(key):
        raise invalid_key("key has characters outside [A-Za-z0-9._/-]")
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

        취소된 호출의 스레드는 계속 돌 수 있다 — 결과는 앞선 내용 전부 아니면
        새 내용 전부이고 반쪽 파일은 생기지 않지만, 취소된 쓰기의 결과는 확정되지 않는다.

        Raises:
            AaestroError: VALIDATION/``VAL_002`` for an unsafe key,
                STORAGE/``STOR_003`` if the write fails.
        """
        return await asyncio.to_thread(self.put_blocking, key, data)

    def put_blocking(self, key: str, data: bytes) -> str:
        """``put`` 의 동기 핵심 — 스레드 안에서 잠금을 쥔 채 쓰려는 호출자가 쓴다."""
        key = validate_key(key, for_write=True)
        try:
            self._write_atomic(key, data)
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

        try:
            return await asyncio.to_thread(self._read, parsed.key)
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

    def _read(self, key: str) -> bytes:
        """경로 해석 (resolve · lstat) 까지 한 스레드 호출에서 읽는다."""
        return self._path_for(key).read_bytes()

    def _write_atomic(self, key: str, data: bytes) -> None:
        """같은 디렉토리의 임시 파일에 쓴 뒤 교체한다 — 쓰다 실패해도 앞선 내용이 남는다."""
        target = self._path_for(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=TEMP_PREFIX)
        try:
            try:
                handle = os.fdopen(fd, "wb")
            except BaseException:
                os.close(fd)  # fdopen 이 실패하면 fd 의 소유자가 없다
                raise
            with handle:
                handle.write(data)
            Path(tmp_name).replace(target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def _path_for(self, key: str) -> Path:
        """key 를 저장 경로로 — 검증된 key 만 들어온다."""
        resolved = inside(self.root / self.scope, self.root / self.scope / key)
        if resolved is None:
            # 검증을 통과해도 심볼릭 링크로 벗어날 수 있다 — 마지막 방어선
            raise invalid_key("key resolves outside the store root")
        return resolved

    def stored_keys(self) -> Iterable[str]:
        """저장된 key 목록 — 운영 점검용.

        ``keys`` 로 두면 dict 처럼 읽혀 오해를 부른다 (린터도 그렇게 읽는다).
        """
        base = self.root / self.scope
        if not base.exists():
            return []
        return sorted(
            str(path.relative_to(base))
            for path in base.rglob("*")
            if path.is_file()
            and not any(part.startswith(".") for part in path.relative_to(base).parts)
        )

    async def delete(self, key: str) -> None:
        """Delete one artifact by key; a missing key is not an error.

        key 하나를 지웁니다 — 이미 없으면 조용히 끝납니다 (정리가 겹쳐 돌아도 안전).

        Raises:
            AaestroError: VALIDATION/``VAL_002`` for an unsafe key,
                STORAGE/``STOR_003`` if the delete fails.
        """
        key = validate_key(key)
        try:
            await asyncio.to_thread(self._delete, key)
        except OSError as err:
            raise storage_error(
                "artifact could not be deleted", key=key, cause=type(err).__name__
            ) from err

    def _plain_path(self, key: str) -> Path | None:
        """링크를 따라가지 않고, 경로 전체가 일반 파일일 때만 그 경로 — 아니면 None."""
        path = self.root.resolve() / self.scope / key
        try:
            if not stat.S_ISREG(path.lstat().st_mode) or path.resolve() != path:
                return None
        except FileNotFoundError:
            return None
        return path

    def _delete(self, key: str) -> None:
        # 링크는 지우지도, 따라가지도 않는다 — 대상이 스코프 안의 다른 파일일 수 있다
        path = self._plain_path(key)
        if path is not None:
            path.unlink(missing_ok=True)

    async def modified_at(self, key: str) -> float | None:
        """Modified time of one stored plain file (epoch seconds), None if it is gone.

        key 하나의 수정 시각 — 일반 파일이 아니거나 없으면 None.
        """
        key = validate_key(key)

        def read() -> float | None:
            path = self._plain_path(key)
            return None if path is None else path.stat().st_mtime

        try:
            return await asyncio.to_thread(read)
        except OSError as err:
            raise storage_error(
                "artifact could not be inspected", key=key, cause=type(err).__name__
            ) from err

    async def stored_entries(self) -> list[tuple[str, float]]:
        """Stored ``(key, modified time as epoch seconds)`` pairs, oldest first.

        저장된 key 와 수정 시각 — 임시 파일 (``.`` 로 시작하는 조각) 은 빠집니다.
        """
        try:
            return await asyncio.to_thread(self._entries)
        except OSError as err:
            raise storage_error("artifacts could not be listed", cause=type(err).__name__) from err

    def _entries(self) -> list[tuple[str, float]]:
        found: list[tuple[str, float]] = []
        for key in self.stored_keys():
            try:
                path = self._plain_path(key)
                if path is not None:
                    found.append((key, path.stat().st_mtime))
            except FileNotFoundError:
                continue  # 목록을 만드는 사이 지워졌다
        return sorted(found, key=lambda entry: entry[1])

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
