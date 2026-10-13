"""Memory storage backends.

저장소 계약과 SQLite 구현. append-only 를 저장소 수준에서 강제한다 —
계약을 코드 규율로만 지키면 결국 누군가 UPDATE 를 쓴다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from functools import wraps
from itertools import batched
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

import structlog

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.memory.entry import MAX_CONTENT_BYTES, MemoryEntry

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from aaestro.modules.memoryset import MemoryKind

log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_entries (
    entry_id   TEXT PRIMARY KEY,
    space      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    content    TEXT NOT NULL,
    tags       TEXT NOT NULL DEFAULT '',
    agent      TEXT NOT NULL,
    run_id     TEXT,
    task_id    TEXT,
    node_id    TEXT,
    created_at TEXT NOT NULL,
    importance REAL NOT NULL,
    supersedes TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_space ON memory_entries(space, created_at);
CREATE INDEX IF NOT EXISTS memory_entries_supersedes ON memory_entries(supersedes, space);

-- append-only 를 저장소가 강제한다. retention 삭제는 이 트리거를 우회하는
-- 전용 경로(purge)로만 수행한다
CREATE TRIGGER IF NOT EXISTS memory_entries_no_update
BEFORE UPDATE ON memory_entries
BEGIN
    SELECT RAISE(ABORT, 'memory entries are append-only');
END;
"""


def storage_error(message: str, *, space: str, agent: str, **details: Any) -> AaestroError:
    """저장 실패를 구조화 에러로 만든다."""
    return AaestroError(
        category=ErrorCategory.MEMORY,
        code=ErrorCode.MEM_002,
        message=message,
        agent=agent,
        details={"memory_space": space, **details},
    )


@runtime_checkable
class MemoryStore(Protocol):
    """Storage contract for memory entries.

    메모리 저장 계약. 백엔드 교체 시 이 계약은 바뀌지 않는다.
    """

    def append(self, entry: MemoryEntry) -> MemoryEntry:
        """항목을 추가한다 — 기존 항목은 절대 수정하지 않는다."""
        ...

    def get(self, entry_id: str) -> MemoryEntry | None:
        """항목 하나를 조회한다."""
        ...

    def space_of(self, entry_id: str) -> str | None:
        """항목이 속한 space id — 없는 id 는 ``None``.

        행을 항목으로 만들지 않는다: 호출자가 그 space 를 읽을 수 있는지 정하기 **전에** 부르는
        조회라서, 남의 행의 내용 (과 그 손상 여부) 이 답을 바꾸면 안 된다.
        """
        ...

    def get_many(self, entry_ids: Sequence[str]) -> dict[str, MemoryEntry]:
        """id 들의 항목을 한 번에 조회한다 — 저장소에 없는 id 는 결과에 빠진다.

        검색 hit 을 materialize 하는 경로다. 쿼리 한 번이어야 한다 (hit 마다 왕복하지 않는다).
        """
        ...

    def superseded(self, space: str, entry_ids: Sequence[str]) -> set[str]:
        """``space`` 안에서 다른 항목이 ``supersedes`` 로 가리키는 id — 정정으로 대체된 항목이다.

        정정은 **같은 space 의 항목만** 대체한다 — 다른 space 에서 낸 정정이 이 space 의 기억을
        숨기면 자기 권한 밖에 영향을 준다. 쿼리 한 번이어야 하고, 빈 입력은 빈 set 이다.
        """
        ...

    def list_space(
        self, space: str, *, kinds: Sequence[MemoryKind] | None = None, limit: int | None = 100
    ) -> tuple[MemoryEntry, ...]:
        """space 의 항목을 최신순으로 조회한다 — ``limit=None`` 이면 전부."""
        ...

    def spaces(self) -> tuple[str, ...]:
        """항목이 하나라도 있는 space 들 — 재시작 뒤 인덱스를 다시 채울 대상이다."""
        ...

    def latest_of_chain(self, space: str, entry_id: str) -> MemoryEntry | None:
        """``space`` 안에서 정정 체인의 최신 항목 — supersedes 로 대체된 항목은 건너뛴다.

        체인은 **space 를 넘지 않는다** (``superseded`` 와 같은 규칙): 시작 항목이 이 space 에
        없으면 ``None`` 이고 — 없는 id 와 답이 같다 — 다른 space 에서 낸 정정은 후속이 아니다.
        다른 space 의 행은 읽지도 않는다.
        """
        ...

    def purge(self, entry_ids: Sequence[str]) -> int:
        """retention 정책 전용 삭제 — 일반 경로에서 호출 금지."""
        ...

    def close(self) -> None:
        """연결을 정리한다 — 백엔드가 살아있는 연결을 소유할 수 있다."""
        ...


def validate_entry(entry: MemoryEntry) -> None:
    """Check the storage invariants before writing.

    저장 전 불변식을 확인합니다.

    Args:
        entry: The entry about to be stored.

    Raises:
        AaestroError: MEMORY/``MEM_002`` if provenance is missing or the
            content exceeds the size cap.
    """
    if not entry.source.agent:
        raise storage_error("memory entry requires provenance", space=entry.space, agent="unknown")

    size = len(entry.content.encode("utf-8"))
    if size > MAX_CONTENT_BYTES:
        # 대용량 원문을 그대로 담으면 검색 품질과 주입 비용이 함께 망가진다
        raise storage_error(
            "memory content exceeds the size cap",
            space=entry.space,
            agent=entry.source.agent,
            size_bytes=size,
            limit_bytes=MAX_CONTENT_BYTES,
        )


UNCONVERTED_METHODS: Final = frozenset({"close"})
"""변환하지 않는 계약 메서드 — 연결 정리는 쿼리가 아니고, 실패해도 호출자가 할 일이 없다."""

WRITE_METHODS: Final = frozenset({"append", "purge"})
"""저장소를 바꾸는 메서드 — 실패는 ``MEM_002``. 나머지 쿼리 메서드의 실패는 ``MEM_004``."""

SPACE_SCOPED_METHODS: Final = frozenset({"superseded", "list_space", "latest_of_chain"})
"""첫 인자가 space id 인 메서드 — 실패 사유에 어느 space 였는지 싣는다."""


def query_methods() -> tuple[str, ...]:
    """``MemoryStore`` 계약에서 드라이버를 부르는 메서드 이름.

    계약에서 뽑는다 — 목록을 따로 적으면 계약에 메서드가 늘 때 변환이 빠진다.
    """
    return tuple(
        name
        for name, member in vars(MemoryStore).items()
        if callable(member) and not name.startswith("_") and name not in UNCONVERTED_METHODS
    )


def query_failure(op: str, args: Sequence[Any], *, retryable: bool = False) -> AaestroError:
    """저장소 실패 하나를 구조화 에러로 만든다 — 쓰기는 ``MEM_002``, 읽기는 ``MEM_004``.

    싣는 것은 연산 이름과 space · 항목 식별자까지다. 쿼리 인자와 행의 내용은 싣지 않는다.
    """
    first = args[0] if args else None
    details: dict[str, Any] = {"op": op}
    message = "failed to query memory store"
    if isinstance(first, MemoryEntry):
        details = {"memory_space": first.space, "entry_id": first.entry_id}
        message = "failed to store memory entry"
    elif op in SPACE_SCOPED_METHODS and isinstance(first, str):
        details["memory_space"] = first
    if op in WRITE_METHODS and not isinstance(first, MemoryEntry):
        message = "failed to modify memory store"
    return AaestroError(
        category=ErrorCategory.MEMORY,
        code=ErrorCode.MEM_002 if op in WRITE_METHODS else ErrorCode.MEM_004,
        message=message,
        agent=first.source.agent if isinstance(first, MemoryEntry) else None,
        retryable=retryable,
        details=details,
    )


@dataclass(frozen=True)
class DriverErrors:
    """What a backend's driver raises, and how the conversion point reads it.

    Attributes:
        base: 드라이버의 예외 기반 클래스 — 이것만 드라이버 실패로 잡는다.
        transient: 운영 계열 (연결 끊김 · 잠금 · 타임아웃) — 다시 걸어볼 만한 실패다.
            무결성 · 프로그래밍 · 데이터 오류는 다시 걸어도 같으므로 여기 넣지 않는다.
        recover: 실패한 쿼리 뒤의 뒷정리 (열린 트랜잭션 rollback) — 필요한 백엔드만 준다.
    """

    base: type[Exception]
    transient: tuple[type[Exception], ...] = ()
    recover: Callable[[Any], None] | None = None


def converts_driver_errors[S](driver: DriverErrors) -> Callable[[type[S]], type[S]]:
    """Wrap every query method of a store so failures leave as ``AaestroError``.

    저장소 구현의 **유일한 변환 지점**. 계약의 쿼리 메서드 전부를 감싸므로 메서드마다 ``try`` 를
    두지 않고, 계약에 메서드가 늘면 구현이 그것을 갖추는 순간 함께 감싸진다.

    잡는 것은 둘이다: 드라이버 예외, 그리고 읽기 메서드가 행을 항목으로 바꾸다 낸
    ``ValueError`` (손상된 행 — pydantic 검증 실패 포함). 그 밖의 예외와 ``AaestroError`` 는
    그대로 지난다.

    Args:
        driver: The backend's driver error classes and recovery hook.

    Returns:
        The class decorator.

    Raises:
        TypeError: If the class does not define a contract query method itself.
    """

    def convert(cls: type[S]) -> type[S]:
        own = vars(cls)
        for name in query_methods():
            if name not in own:
                # 상속받은 메서드는 부모 쪽에서 이미 감싸였거나 전혀 감싸이지 않았다 —
                # 어느 쪽인지 여기서 알 수 없으므로 조용히 넘어가지 않는다
                raise TypeError(
                    f"{cls.__name__} must define MemoryStore.{name} itself "
                    "for its driver errors to be converted"
                )
            setattr(cls, name, _converting(name, own[name], driver))
        return cls

    return convert


def _converting(op: str, method: Callable[..., Any], driver: DriverErrors) -> Callable[..., Any]:
    corrupt_row: tuple[type[Exception], ...] = () if op in WRITE_METHODS else (ValueError,)

    @wraps(method)
    def call(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except driver.base as err:
            _recover(self, driver, op)
            retryable = isinstance(err, driver.transient)
            raise query_failure(op, args, retryable=retryable) from err
        except corrupt_row as err:
            # 손상된 행은 다시 읽어도 같다. 원인은 체인에만 두고 에러 본문에는 싣지 않는다 —
            # 검증 실패의 글은 행의 값을 인용한다
            raise query_failure(op, args) from err

    return call


def _recover(store: Any, driver: DriverErrors, op: str) -> None:
    """실패한 쿼리 뒤 백엔드의 뒷정리를 돌린다 — 뒷정리의 실패가 원래 실패를 가리지 않게 한다."""
    if driver.recover is None:
        return
    try:
        driver.recover(store)
    except driver.base as err:
        log.warning("memory store recovery failed", op=op, exc_info=err)


def _rollback(store: SqliteMemoryStore) -> None:
    """실패한 문장이 연 트랜잭션을 버린다.

    남겨 두면 다음에 성공한 호출의 commit 이 실패한 호출의 앞부분 (묶음 삭제의 앞 묶음) 을
    함께 확정한다.
    """
    store._conn.rollback()  # noqa: SLF001 — 같은 모듈의 구현 내부다


# SQLite 의 바인드 변수 상한은 빌드마다 다르다 (3.32 이전 999, 이후 32766) —
# 낮은 쪽 아래로 잡아 ID 묶음이 아무리 커도 `IN (?, …)` 문이 상한을 넘지 않게 한다
SQLITE_BATCH_SIZE = 500


def superseded_sql(count: int) -> str:
    """``superseded`` 의 쿼리 — 구현과 색인 plan 테스트가 같은 문자열을 쓴다."""
    placeholders = ", ".join("?" for _ in range(count))
    return (
        "SELECT DISTINCT supersedes FROM memory_entries "  # noqa: S608
        f"WHERE space = ? AND supersedes IN ({placeholders})"
    )


def successor_sql() -> str:
    """정정 체인의 후속 한 건 — 구현과 색인 plan 테스트가 같은 문자열을 쓴다.

    ``space`` 조건이 없으면 다른 space 에서 낸 정정이 이 space 의 기억을 가린다.
    색인을 지정한다: 정렬을 피하려는 planner 가 ``(space, created_at)`` 색인을 골라 space 전체를
    훑는다 — 같은 항목의 정정은 몇 건뿐이라 그것만 찾아 정렬하는 쪽이 싸다.
    """
    return (
        "SELECT * FROM memory_entries INDEXED BY memory_entries_supersedes "
        "WHERE supersedes = ? AND space = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1"
    )


@converts_driver_errors(
    DriverErrors(base=sqlite3.Error, transient=(sqlite3.OperationalError,), recover=_rollback)
)
@dataclass
class SqliteMemoryStore:
    """SQLite-backed memory store (dev).

    개발 환경용 SQLite 저장소. 논리적 space 분리로 격리를 구현한다.
    """

    path: str = ":memory:"
    _conn: sqlite3.Connection = field(init=False)

    def __post_init__(self) -> None:
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def append(self, entry: MemoryEntry) -> MemoryEntry:
        """Store one entry.

        항목 하나를 저장합니다 — 저장은 즉시 commit 되고, 인덱싱은 별도입니다.

        Args:
            entry: The entry to store.

        Returns:
            The stored entry.

        Raises:
            AaestroError: MEMORY/``MEM_002`` on validation or storage failure.
        """
        validate_entry(entry)
        self._conn.execute(
            "INSERT INTO memory_entries "
            "(entry_id, space, kind, content, tags, agent, run_id, task_id, "
            " node_id, created_at, importance, supersedes) "
            "VALUES (:entry_id, :space, :kind, :content, :tags, :agent, :run_id, "
            " :task_id, :node_id, :created_at, :importance, :supersedes)",
            entry.to_row(),
        )
        self._conn.commit()
        return entry

    def get(self, entry_id: str) -> MemoryEntry | None:
        """항목 하나를 조회한다."""
        row = self._conn.execute(
            "SELECT * FROM memory_entries WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        return MemoryEntry.from_row(dict(row)) if row else None

    def space_of(self, entry_id: str) -> str | None:
        """항목이 속한 space id — 행을 항목으로 만들지 않는다."""
        row = self._conn.execute(
            "SELECT space FROM memory_entries WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        return str(row["space"]) if row else None

    def get_many(self, entry_ids: Sequence[str]) -> dict[str, MemoryEntry]:
        """id 들의 항목을 쿼리 한 번으로 조회한다 — 없는 id 는 결과에 빠진다."""
        if not entry_ids:
            return {}
        found: dict[str, MemoryEntry] = {}
        for batch in batched(entry_ids, SQLITE_BATCH_SIZE):
            placeholders = ", ".join("?" for _ in batch)
            rows = self._conn.execute(
                f"SELECT * FROM memory_entries WHERE entry_id IN ({placeholders})",  # noqa: S608
                list(batch),
            ).fetchall()
            for row in rows:
                entry = MemoryEntry.from_row(dict(row))
                found[entry.entry_id] = entry
        return found

    def superseded(self, space: str, entry_ids: Sequence[str]) -> set[str]:
        """같은 space 안에서 다른 항목이 ``supersedes`` 로 가리키는 id — 쿼리 한 번."""
        if not entry_ids:
            return set()
        result: set[str] = set()
        for batch in batched(entry_ids, SQLITE_BATCH_SIZE):
            rows = self._conn.execute(superseded_sql(len(batch)), [space, *batch]).fetchall()
            result.update(str(row["supersedes"]) for row in rows)
        return result

    def list_space(
        self, space: str, *, kinds: Sequence[MemoryKind] | None = None, limit: int | None = 100
    ) -> tuple[MemoryEntry, ...]:
        """Read a space's entries, newest first.

        space 의 항목을 최신순으로 읽습니다 — 검색이 space 경계를 넘지 않습니다.
        ``limit=None`` 이면 전부 — 인덱스 warm-up 용입니다.
        """
        query = "SELECT * FROM memory_entries WHERE space = ?"
        params: list[Any] = [space]
        # kinds=[] 는 "아무 종류도 원하지 않는다" 지 "필터 없음" 이 아니다.
        # 빈 목록을 falsy 로 흘려보내면 필터링 결과가 전체 조회로 뒤집힌다
        if kinds is not None:
            if not kinds:
                return ()
            placeholders = ", ".join("?" for _ in kinds)
            query += f" AND kind IN ({placeholders})"
            params.extend(str(k) for k in kinds)
        query += " ORDER BY created_at DESC, rowid DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        rows = self._conn.execute(query, params).fetchall()
        return tuple(MemoryEntry.from_row(dict(r)) for r in rows)

    def spaces(self) -> tuple[str, ...]:
        """항목이 있는 space 들."""
        rows = self._conn.execute(
            "SELECT DISTINCT space FROM memory_entries ORDER BY space"
        ).fetchall()
        return tuple(str(r["space"]) for r in rows)

    def latest_of_chain(self, space: str, entry_id: str) -> MemoryEntry | None:
        """Follow the correction chain forward, inside one space.

        정정 체인을 앞으로 따라가 최신 항목을 찾습니다 — 대체된 기억을 주입하면
        모델이 이미 틀린 것으로 정정된 사실을 다시 믿습니다. 체인은 ``space`` 를 넘지 않습니다.

        Args:
            space: The space the chain lives in.
            entry_id: Any entry id in the chain.

        Returns:
            The newest entry superseding it, or None if the id is unknown in this space.
        """
        # 시작 행도 space 로 좁혀 읽는다 — 다른 space 의 행은 만들지 않는다
        start = self._conn.execute(
            "SELECT * FROM memory_entries WHERE entry_id = ? AND space = ?", (entry_id, space)
        ).fetchone()
        if start is None:
            return None
        current = MemoryEntry.from_row(dict(start))

        seen = {entry_id}
        while True:
            row = self._conn.execute(successor_sql(), (current.entry_id, space)).fetchone()
            if row is None:
                return current
            successor = MemoryEntry.from_row(dict(row))
            # 순환 정정이 들어와도 무한 루프에 빠지지 않는다
            if successor.entry_id in seen:
                return current
            seen.add(successor.entry_id)
            current = successor

    def purge(self, entry_ids: Sequence[str]) -> int:
        """Delete entries under the retention policy.

        보존 정책에 따라 항목을 삭제합니다 — **retention 전용 경로**이며
        일반 코드에서 호출하지 않습니다.

        Args:
            entry_ids: The entries to remove.

        Returns:
            The number of rows removed.
        """
        if not entry_ids:
            return 0
        removed = 0
        for batch in batched(entry_ids, SQLITE_BATCH_SIZE):
            placeholders = ", ".join("?" for _ in batch)
            cursor = self._conn.execute(
                f"DELETE FROM memory_entries WHERE entry_id IN ({placeholders})",  # noqa: S608
                list(batch),
            )
            removed += cursor.rowcount
        # 묶음마다 commit 하지 않는다 — 중간에 실패하면 일부만 지워진 채 남는다
        self._conn.commit()
        return removed

    def close(self) -> None:
        """연결을 정리한다."""
        self._conn.close()


__all__ = [
    "DriverErrors",
    "MemoryStore",
    "SqliteMemoryStore",
    "converts_driver_errors",
    "query_failure",
    "query_methods",
    "storage_error",
    "validate_entry",
]
