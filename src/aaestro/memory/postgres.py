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
from aaestro.memory.store import DriverErrors, converts_driver_errors, validate_entry

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
CREATE INDEX IF NOT EXISTS memory_entries_supersedes ON memory_entries(supersedes, space);

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


def _connect(dsn: str) -> psycopg.Connection[dict[str, Any]]:
    """저장소 연결 하나 — 처음 열 때와 끊긴 뒤 다시 열 때 같은 설정을 쓴다."""
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=True)


def _transient(err: Exception) -> bool:
    """연결 · 서버 쪽 운영 실패 (``OperationalError`` 계열) 만 일시 장애다."""
    return isinstance(err, psycopg.OperationalError)


def _reconnect(store: PostgresMemoryStore) -> None:
    """끊긴 연결을 다시 연다 — 일시 장애를 retryable 로 내보내는 약속을 지키는 자리다.

    autocommit 연결이라 실패한 문장이 트랜잭션을 남기지 않으므로 rollback 은 없다. 연결이 멀쩡하면
    아무것도 하지 않는다 (플래그 두 개만 본다 — 왕복이 없다).

    두 자리에서 부른다: 실패 직후 (``recover`` — 끊김을 알게 된 곳) 와 쿼리 앞 (``prepare``).
    실패 직후의 다시 열기가 실패하면 그 예외는 뒷정리 실패로 기록되고 원래 실패가 그대로
    올라간다. 그 다음 호출은 쿼리 앞에서 다시 연다 — 닫힌 연결로 한 번 더 실패하지 않는다.

    겹침: 저장소는 Memory Service 의 이벤트 루프 스레드에서만 동기로 불린다 (``async`` 라우트가
    직접 부르고, 스레드풀 · 백그라운드 스레드가 없다) — 재연결 도중 다른 호출이 끼어들 수 없다.
    """
    conn = store._conn  # noqa: SLF001 — 같은 모듈의 구현 내부다
    if not (conn.closed or conn.broken):
        return
    store._conn = _connect(store.dsn)  # noqa: SLF001


@converts_driver_errors(
    DriverErrors(base=psycopg.Error, transient=_transient, recover=_reconnect, prepare=_reconnect)
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
        self._conn = _connect(self.dsn)
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
        with self._conn.cursor() as cursor:
            cursor.execute(_INSERT, entry.to_row())
        return entry

    def get(self, entry_id: str) -> MemoryEntry | None:
        """항목 하나를 조회한다."""
        with self._conn.cursor() as cursor:
            cursor.execute("SELECT * FROM memory_entries WHERE entry_id = %s", (entry_id,))
            row = cursor.fetchone()
        return MemoryEntry.from_row(row) if row else None

    def space_of(self, entry_id: str) -> str | None:
        """항목이 속한 space id — 행을 항목으로 만들지 않는다."""
        with self._conn.cursor() as cursor:
            cursor.execute("SELECT space FROM memory_entries WHERE entry_id = %s", (entry_id,))
            row = cursor.fetchone()
        return str(row["space"]) if row else None

    def get_many(self, entry_ids: Sequence[str]) -> dict[str, MemoryEntry]:
        """id 들의 항목을 쿼리 한 번으로 조회한다 — 없는 id 는 결과에 빠진다."""
        if not entry_ids:
            return {}
        with self._conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM memory_entries WHERE entry_id = ANY(%s)", (list(entry_ids),)
            )
            rows = cursor.fetchall()
        found = (MemoryEntry.from_row(row) for row in rows)
        return {entry.entry_id: entry for entry in found}

    def superseded(self, space: str, entry_ids: Sequence[str]) -> set[str]:
        """같은 space 안에서 다른 항목이 ``supersedes`` 로 가리키는 id — 쿼리 한 번."""
        if not entry_ids:
            return set()
        with self._conn.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT supersedes FROM memory_entries "
                "WHERE space = %s AND supersedes = ANY(%s)",
                (space, list(entry_ids)),
            )
            rows = cursor.fetchall()
        return {str(row["supersedes"]) for row in rows}

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

    def latest_of_chain(self, space: str, entry_id: str) -> MemoryEntry | None:
        """Follow the correction chain forward, inside one space.

        정정 체인을 앞으로 따라가 최신 항목을 찾습니다 — 대체된 기억을 주입하면
        모델이 이미 틀린 것으로 정정된 사실을 다시 믿습니다. 체인은 ``space`` 를 넘지 않습니다
        (``space`` 조건이 없으면 다른 space 에서 낸 정정이 이 space 의 기억을 가립니다).

        Args:
            space: The space the chain lives in.
            entry_id: Any entry id in the chain.

        Returns:
            The newest entry superseding it, or None if the id is unknown in this space.
        """
        # 시작 행도 space 로 좁혀 읽는다 — 다른 space 의 행은 만들지 않는다
        with self._conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM memory_entries WHERE entry_id = %s AND space = %s",
                (entry_id, space),
            )
            start = cursor.fetchone()
        if start is None:
            return None
        current = MemoryEntry.from_row(start)

        seen = {entry_id}
        while True:
            with self._conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM memory_entries WHERE supersedes = %s AND space = %s "
                    "ORDER BY created_at DESC, ctid DESC LIMIT 1",
                    (current.entry_id, space),
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
