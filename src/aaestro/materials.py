"""Build materials — the files an agent image is baked from (#264).

커스텀 에이전트는 선언만으로 부족하다: 실행기 코드(`src/`)와, 필요하면 빌드 레시피
(`Dockerfile`)가 있어야 이미지를 구울 수 있다. 그 재료가 작업 트리에 흩어져 있으면
화면에서 에이전트를 만들 자리가 없고, 지울지 말지도 정할 수 없다 (#258).

여기서는 재료를 **에이전트 이름 + 매니페스트 버전**으로 스토어에 담는다. 빌드는 이
스토어에서 임시 디렉토리를 조립하므로 (#265), 작업 트리에 커스텀 코드가 남지 않는다.

담기는 것은 `Dockerfile` 과 `src/**` 뿐이다 — `manifest.yaml` 과 `modules/` 는 빌드 시점에
카탈로그에서 해석해 넣는다. 재료가 아니라 선언이기 때문이다.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode, printable_label

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

DOCKERFILE = "Dockerfile"
SOURCE_ROOT = "src"
MAX_FILE_BYTES = 256 * 1024
"""파일 하나의 상한 — 재료는 소스이지 아티팩트가 아니다. 큰 것은 이미지에 굽거나 볼륨으로."""

MAX_FILES = 200
"""한 에이전트의 파일 수 상한 — 스토어가 아티팩트 저장소로 흘러가는 것을 막는다."""


@dataclass(frozen=True)
class Materials:
    """One agent version's build materials.

    에이전트 한 버전의 빌드 재료. `files` 의 키는 빌드 컨텍스트 기준 상대 경로다
    (#263 의 레이아웃 규약).
    """

    agent: str
    version: str
    files: Mapping[str, str]
    updated_at: str = ""

    @property
    def dockerfile(self) -> str | None:
        """사용자가 넣은 Dockerfile — 없으면 스켈레톤 기본을 쓴다 (#265)."""
        return self.files.get(DOCKERFILE)


@runtime_checkable
class MaterialStore(Protocol):
    """Where build materials live between authoring and building."""

    def put(self, materials: Materials) -> None: ...
    def get(self, agent: str, version: str) -> Materials | None: ...
    def versions(self, agent: str) -> Sequence[str]: ...
    def delete(self, agent: str, version: str) -> bool: ...


def _storage_error(message: str, **details: str) -> AaestroError:
    return AaestroError(
        category=ErrorCategory.STORAGE, code=ErrorCode.STOR_003, message=message, details=details
    )


def _invalid(message: str, **details: object) -> AaestroError:
    return AaestroError(
        category=ErrorCategory.VALIDATION,
        code=ErrorCode.VAL_002,
        message=message,
        # 재료 경로는 요청 본문의 **키** 다 — 다른 키 출력과 같이 길이 · 문자를 정제해 싣는다 (#386)
        details={
            key: printable_label(value) if key == "path" and isinstance(value, str) else value
            for key, value in details.items()
        },
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def check_path(path: str) -> str:
    """Validate one material path against the build-context layout.

    빌드 컨텍스트 안의 경로인지 검사한다. 컨텍스트를 벗어나는 경로를 받아 두면 조립 단계가
    임시 디렉토리 밖에 파일을 쓰게 된다 — 검사는 **적재 시점**이어야 한다.

    Args:
        path: Context-relative path (``Dockerfile`` or ``src/...``).

    Returns:
        The normalised path.

    Raises:
        AaestroError: VALIDATION/``VAL_002`` for anything outside the layout.
    """
    if not path or path != path.strip():
        raise _invalid("material path must not be empty or padded", path=path)
    if path.startswith("/") or ":" in path or "\\" in path:
        raise _invalid("material path must be relative and posix-style", path=path)
    parts = PurePosixPath(path).parts
    if any(part in ("..", ".") for part in parts):
        raise _invalid("material path must not traverse the build context", path=path)
    if str(PurePosixPath(path)) != path:
        # `src/./agent.py` 는 탈출은 아니지만 같은 파일의 다른 이름이다 — 둘 다 담기면
        # 하나가 조용히 덮인다. 정규형만 받는다
        raise _invalid("material path must already be normalised", path=path)
    if path == DOCKERFILE:
        return path
    if parts and parts[0] == SOURCE_ROOT and len(parts) > 1:
        return path
    raise _invalid(
        f"material path must be {DOCKERFILE!r} or under {SOURCE_ROOT}/",
        path=path,
    )


def check_files(files: Mapping[str, str]) -> dict[str, str]:
    """Validate a whole material set — paths, sizes, and count.

    빈 집합도 받는다: 재료를 지우는 것과 같은 뜻이 아니라, "이 버전에는 재료가 없다" 를
    명시적으로 적는 경우가 있다.
    """
    if len(files) > MAX_FILES:
        raise _invalid("too many material files", count=len(files), limit=MAX_FILES)
    checked: dict[str, str] = {}
    for path, content in files.items():
        if not isinstance(content, str):
            raise _invalid("material content must be text", path=path)
        size = len(content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise _invalid(
                "material file is too large", path=path, bytes=size, limit=MAX_FILE_BYTES
            )
        checked[check_path(path)] = content
    return checked


_SCHEMA = """
CREATE TABLE IF NOT EXISTS materials (
    agent      TEXT NOT NULL,
    version    TEXT NOT NULL,
    files      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (agent, version)
);
"""


@dataclass
class InMemoryMaterialStore:
    """테스트/단일 프로세스용."""

    _records: dict[tuple[str, str], Materials] = field(default_factory=dict)

    def put(self, materials: Materials) -> None:
        # sqlite 구현과 같은 계약 — 적재 시점을 찍는다
        stamped = (
            materials
            if materials.updated_at
            else Materials(**{**materials.__dict__, "updated_at": _now()})
        )
        self._records[materials.agent, materials.version] = stamped

    def get(self, agent: str, version: str) -> Materials | None:
        return self._records.get((agent, version))

    def versions(self, agent: str) -> Sequence[str]:
        return sorted(version for name, version in self._records if name == agent)

    def delete(self, agent: str, version: str) -> bool:
        return self._records.pop((agent, version), None) is not None


@dataclass
class SqliteMaterialStore:
    """deployment_store 와 같은 배치 — 파일 하나, 프로세스 재시작을 넘긴다.

    Control plane 은 별도 스레드에서 서빙되므로 연결 생성과 모든 쿼리를 `_lock` 으로
    직렬화한다 (`SqliteDeploymentStore` 와 같은 사정).
    """

    path: str | Path
    _conn: sqlite3.Connection | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            try:
                self._conn = sqlite3.connect(
                    str(self.path), isolation_level=None, check_same_thread=False
                )
                self._conn.row_factory = sqlite3.Row
                self._conn.execute(_SCHEMA)
            except sqlite3.Error as err:
                raise _storage_error(
                    "material store could not be opened", path=str(self.path)
                ) from err
        return self._conn

    def put(self, materials: Materials) -> None:
        try:
            with self._lock:
                self._connect().execute(
                    "INSERT INTO materials VALUES (?,?,?,?) "
                    "ON CONFLICT(agent, version) DO UPDATE SET "
                    "files=excluded.files, updated_at=excluded.updated_at",
                    (
                        materials.agent,
                        materials.version,
                        json.dumps(dict(materials.files)),
                        materials.updated_at or _now(),
                    ),
                )
        except sqlite3.Error as err:
            raise _storage_error(
                "materials could not be stored", agent=materials.agent, version=materials.version
            ) from err

    def get(self, agent: str, version: str) -> Materials | None:
        with self._lock:
            row = (
                self._connect()
                .execute(
                    "SELECT * FROM materials WHERE agent = ? AND version = ?", (agent, version)
                )
                .fetchone()
            )
        return _row(row) if row else None

    def versions(self, agent: str) -> Sequence[str]:
        with self._lock:
            rows = (
                self._connect()
                .execute("SELECT version FROM materials WHERE agent = ? ORDER BY version", (agent,))
                .fetchall()
            )
        return [row["version"] for row in rows]

    def delete(self, agent: str, version: str) -> bool:
        with self._lock:
            cursor = self._connect().execute(
                "DELETE FROM materials WHERE agent = ? AND version = ?", (agent, version)
            )
        return cursor.rowcount > 0


def _row(row: sqlite3.Row) -> Materials:
    return Materials(
        agent=row["agent"],
        version=row["version"],
        files=json.loads(row["files"]),
        updated_at=row["updated_at"],
    )


__all__ = [
    "DOCKERFILE",
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "SOURCE_ROOT",
    "InMemoryMaterialStore",
    "MaterialStore",
    "Materials",
    "SqliteMaterialStore",
    "check_files",
    "check_path",
]
