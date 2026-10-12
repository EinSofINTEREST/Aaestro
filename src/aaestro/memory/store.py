"""Memory storage backends.

저장소 계약과 SQLite 구현. append-only 를 저장소 수준에서 강제한다 —
계약을 코드 규율로만 지키면 결국 누군가 UPDATE 를 쓴다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.memory.entry import MAX_CONTENT_BYTES, MemoryEntry

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aaestro.modules.memoryset import MemoryKind

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

    def list_space(
        self, space: str, *, kinds: Sequence[MemoryKind] | None = None, limit: int | None = 100
    ) -> tuple[MemoryEntry, ...]:
        """space 의 항목을 최신순으로 조회한다 — ``limit=None`` 이면 전부."""
        ...

    def spaces(self) -> tuple[str, ...]:
        """항목이 하나라도 있는 space 들 — 재시작 뒤 인덱스를 다시 채울 대상이다."""
        ...

    def latest_of_chain(self, entry_id: str) -> MemoryEntry | None:
        """정정 체인의 최신 항목 — supersedes 로 대체된 항목은 건너뛴다."""
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
        row = entry.to_row()
        try:
            self._conn.execute(
                "INSERT INTO memory_entries "
                "(entry_id, space, kind, content, tags, agent, run_id, task_id, "
                " node_id, created_at, importance, supersedes) "
                "VALUES (:entry_id, :space, :kind, :content, :tags, :agent, :run_id, "
                " :task_id, :node_id, :created_at, :importance, :supersedes)",
                row,
            )
            self._conn.commit()
        except sqlite3.DatabaseError as err:
            raise storage_error(
                "failed to store memory entry",
                space=entry.space,
                agent=entry.source.agent,
                entry_id=entry.entry_id,
            ) from err
        return entry

    def get(self, entry_id: str) -> MemoryEntry | None:
        """항목 하나를 조회한다."""
        row = self._conn.execute(
            "SELECT * FROM memory_entries WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        return MemoryEntry.from_row(dict(row)) if row else None

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

    def latest_of_chain(self, entry_id: str) -> MemoryEntry | None:
        """Follow the correction chain forward.

        정정 체인을 앞으로 따라가 최신 항목을 찾습니다 — 대체된 기억을 주입하면
        모델이 이미 틀린 것으로 정정된 사실을 다시 믿습니다.

        Args:
            entry_id: Any entry id in the chain.

        Returns:
            The newest entry superseding it, or None if the id is unknown.
        """
        current = self.get(entry_id)
        if current is None:
            return None

        seen = {entry_id}
        while True:
            row = self._conn.execute(
                "SELECT * FROM memory_entries WHERE supersedes = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (current.entry_id,),
            ).fetchone()
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
        placeholders = ", ".join("?" for _ in entry_ids)
        cursor = self._conn.execute(
            f"DELETE FROM memory_entries WHERE entry_id IN ({placeholders})",  # noqa: S608
            list(entry_ids),
        )
        self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        """연결을 정리한다."""
        self._conn.close()


__all__ = ["MemoryStore", "SqliteMemoryStore", "storage_error", "validate_entry"]
