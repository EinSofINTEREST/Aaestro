"""PostgreSQL-backed memory store.

프로덕션 저장소. SQLite 구현과 **같은 계약**을 지키며, append-only 도 동일하게
저장소 수준에서 강제한다 — 규칙을 코드 규율로만 지키면 결국 누군가 UPDATE 를 쓴다.

인덱스는 아직 이 계층의 책임이 아니다 (pgvector/tsvector 는 후속) — 여기서는
저장 계약만 담당하고 검색은 기존 ``SpaceIndex`` 가 맡는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row

from aaestro.memory.entry import MemoryEntry
from aaestro.memory.store import storage_error, validate_entry

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aaestro.modules.memoryset import MemoryKind

APPEND_ONLY_MESSAGE = "memory entries are append-only"

_SCHEMA = f"""
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
    importance DOUBLE PRECISION NOT NULL,
    supersedes TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_space ON memory_entries(space, created_at);

-- append-only 를 저장소가 강제한다. retention 삭제는 이 트리거를 우회하는
-- 전용 경로(purge)로만 수행한다 — SQLite 의 BEFORE UPDATE RAISE 와 동등하다
CREATE OR REPLACE FUNCTION memory_entries_reject_update() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '{APPEND_ONLY_MESSAGE}';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS memory_entries_no_update ON memory_entries;
CREATE TRIGGER memory_entries_no_update
BEFORE UPDATE ON memory_entries
FOR EACH ROW EXECUTE FUNCTION memory_entries_reject_update();
"""

_INSERT = (
    "INSERT INTO memory_entries "
    "(entry_id, space, kind, content, tags, agent, run_id, task_id, "
    " node_id, created_at, importance, supersedes) "
    "VALUES (%(entry_id)s, %(space)s, %(kind)s, %(content)s, %(tags)s, %(agent)s, "
    " %(run_id)s, %(task_id)s, %(node_id)s, %(created_at)s, %(importance)s, %(supersedes)s)"
)


@dataclass
class PostgresMemoryStore:
    """PostgreSQL-backed memory store (prod).

    프로덕션 PostgreSQL 저장소. ``SqliteMemoryStore`` 와 동일한 계약을 구현하므로
    백엔드 교체가 호출부에 보이지 않는다.
    """

    dsn: str
    _conn: psycopg.Connection[dict[str, Any]] = field(init=False)

    def __post_init__(self) -> None:
        self._conn = psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True)
        with self._conn.cursor() as cursor:
            cursor.execute(_SCHEMA)

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
        try:
            with self._conn.cursor() as cursor:
                cursor.execute(_INSERT, entry.to_row())
        except psycopg.Error as err:
            raise storage_error(
                "failed to store memory entry",
                space=entry.space,
                agent=entry.source.agent,
                entry_id=entry.entry_id,
            ) from err
        return entry

    def get(self, entry_id: str) -> MemoryEntry | None:
        """항목 하나를 조회한다."""
        with self._conn.cursor() as cursor:
            cursor.execute("SELECT * FROM memory_entries WHERE entry_id = %s", (entry_id,))
            row = cursor.fetchone()
        return MemoryEntry.from_row(row) if row else None

    def list_space(
        self, space: str, *, kinds: Sequence[MemoryKind] | None = None, limit: int | None = 100
    ) -> tuple[MemoryEntry, ...]:
        """Read a space's entries, newest first.

        space 의 항목을 최신순으로 읽습니다 — 검색이 space 경계를 넘지 않습니다.
        """
        query = "SELECT * FROM memory_entries WHERE space = %s"
        params: list[Any] = [space]
        # kinds=[] 는 "아무 종류도 원하지 않는다" 지 "필터 없음" 이 아니다
        if kinds is not None:
            if not kinds:
                return ()
            query += " AND kind = ANY(%s)"
            params.append([str(k) for k in kinds])
        # ctid 는 SQLite rowid 에 대응하는 물리 순서 — 같은 타임스탬프의 tie-break
        query += " ORDER BY created_at DESC, ctid DESC"
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)

        with self._conn.cursor() as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()
        return tuple(MemoryEntry.from_row(r) for r in rows)

    def spaces(self) -> tuple[str, ...]:
        """항목이 있는 space 들."""
        with self._conn.cursor() as cursor:
            cursor.execute("SELECT DISTINCT space FROM memory_entries ORDER BY space")
            rows = cursor.fetchall()
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
            with self._conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM memory_entries WHERE supersedes = %s "
                    "ORDER BY created_at DESC, ctid DESC LIMIT 1",
                    (current.entry_id,),
                )
                row = cursor.fetchone()
            if row is None:
                return current
            successor = MemoryEntry.from_row(row)
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
        with self._conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM memory_entries WHERE entry_id = ANY(%s)", (list(entry_ids),)
            )
            return cursor.rowcount

    def close(self) -> None:
        """연결을 정리한다."""
        self._conn.close()


__all__ = ["APPEND_ONLY_MESSAGE", "PostgresMemoryStore"]
