"""Where identities and rules live so a restarted control plane still knows them.

신원과 부여·회수 기록을 저장한다. 에이전트 신원은 control plane 재시작을 넘어야 한다 — 떠 있는
컨테이너의 자격이 재시작마다 무효가 되면 모든 강제 지점이 그 에이전트를 거부한다 (09 Access
Enforcement 4).

자격 **값**은 저장하지 않는다. 해시만 둔다: 저장 파일이 새도 그것으로 에이전트를 사칭할 수 없다.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from aaestro.access.model import Effect, Mode, ResourceKind, Rule
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


@dataclass(frozen=True)
class Identity:
    """An issued agent credential, by its hash."""

    credential_hash: str
    agent: str
    deployment_id: str
    issued_at: float
    revoked_at: float | None = None
    graph: str = ""
    """배포된 그래프 — A2A 선언 판정이 이 에이전트의 ``connections`` 를 찾는 곳."""


TICKETS_PER_EDGE = 4
"""(호출자 신원, 피호출자) 마다 남기는 유효한 표 수 — 갱신 직후에도 진행 중 호출의 옛 표는 산다."""


@dataclass(frozen=True)
class Ticket:
    """A one-callee A2A call ticket, by its hash.

    호출자 자격 자체를 피호출자에게 넘기면 피호출자가 그 자격으로 다른 강제 지점을 통과한다 —
    그래서 피호출자 **하나에만** 쓸 수 있고 곧 만료되는 표를 따로 발급한다 (03 Enforcement).
    """

    ticket_hash: str
    caller_hash: str
    """발급받은 호출자 신원의 해시 — 그 신원이 폐기되면 표도 통하지 않는다."""
    caller: str
    callee: str
    issued_at: float
    expires_at: float


@runtime_checkable
class AccessStore(Protocol):
    def put_identity(self, identity: Identity) -> None: ...
    def identity(self, credential_hash: str) -> Identity | None: ...
    def revoke_identities(self, deployment_id: str, at: float) -> int:
        """Revoke a deployment's identities; bumps the version **in the same write** if any."""
        ...

    def put_rule(self, rule: Rule) -> int:
        """Store a rule and bump the version **in the same write**; returns the new version.

        기록과 버전을 따로 쓰면 그 사이에 죽었을 때 기록은 바뀌고 버전은 그대로 남는다 — 재시작한
        레지스트리가 알림을 내지 않아 강제 지점이 옛 판정을 계속 쓴다.
        """
        ...

    def rule(self, rule_id: str) -> Rule | None: ...
    def rules(self, agent: str) -> Sequence[Rule]: ...
    def live_graphs(self, agent: str) -> frozenset[str]: ...
    def put_ticket(self, ticket: Ticket) -> None:
        """Store a ticket, dropping expired ones and all but the newest few per edge.

        표는 갱신마다 새로 생긴다 — 지우지 않으면 저장소가 끝없이 자라고, 침해된 에이전트가 발급을
        반복해 디스크를 채울 수 있다.
        """
        ...

    def ticket(self, ticket_hash: str) -> Ticket | None: ...

    def touch(self) -> int:
        """Bump the version for a change that has no record — a declaration file changed."""
        ...

    def version(self) -> int: ...


@dataclass
class InMemoryAccessStore:
    """테스트/단일 프로세스용."""

    _identities: dict[str, Identity] = field(default_factory=dict)
    _rules: dict[str, Rule] = field(default_factory=dict)
    _tickets: dict[str, Ticket] = field(default_factory=dict)
    _version: int = 0

    def put_identity(self, identity: Identity) -> None:
        self._identities[identity.credential_hash] = identity

    def identity(self, credential_hash: str) -> Identity | None:
        return self._identities.get(credential_hash)

    def revoke_identities(self, deployment_id: str, at: float) -> int:
        revoked = 0
        for key, found in list(self._identities.items()):
            if found.deployment_id == deployment_id and found.revoked_at is None:
                self._identities[key] = replace(found, revoked_at=at)
                revoked += 1
        if revoked:
            self._version += 1
        return revoked

    def put_rule(self, rule: Rule) -> int:
        self._rules[rule.rule_id] = rule
        self._version += 1
        return self._version

    def rule(self, rule_id: str) -> Rule | None:
        return self._rules.get(rule_id)

    def rules(self, agent: str) -> Sequence[Rule]:
        return sorted((r for r in self._rules.values() if r.agent == agent), key=_order)

    def live_graphs(self, agent: str) -> frozenset[str]:
        return frozenset(
            i.graph
            for i in self._identities.values()
            if i.agent == agent and i.revoked_at is None and i.graph
        )

    def put_ticket(self, ticket: Ticket) -> None:
        now = ticket.issued_at
        for key, found in list(self._tickets.items()):
            if found.expires_at <= now:
                del self._tickets[key]
        self._tickets[ticket.ticket_hash] = ticket
        # 같은 시각에 발급된 표는 나중에 넣은 것이 최신이다 — 삽입 순서를 두 번째 기준으로 쓴다
        edge = [
            t
            for t in self._tickets.values()
            if t.caller_hash == ticket.caller_hash and t.callee == ticket.callee
        ]
        same_edge = [
            t
            for _, t in sorted(enumerate(edge), key=lambda p: (p[1].issued_at, p[0]), reverse=True)
        ]
        for stale in same_edge[TICKETS_PER_EDGE:]:
            del self._tickets[stale.ticket_hash]

    def ticket(self, ticket_hash: str) -> Ticket | None:
        return self._tickets.get(ticket_hash)

    def touch(self) -> int:
        self._version += 1
        return self._version

    def version(self) -> int:
        return self._version


def _order(rule: Rule) -> tuple[float, str]:
    return (rule.created_at, rule.rule_id)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS identities (
    credential_hash TEXT PRIMARY KEY,
    agent           TEXT NOT NULL,
    deployment_id   TEXT NOT NULL,
    issued_at       REAL NOT NULL,
    revoked_at      REAL,
    graph           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS identities_by_deployment ON identities (deployment_id);
CREATE TABLE IF NOT EXISTS tickets (
    ticket_hash TEXT PRIMARY KEY,
    caller_hash TEXT NOT NULL,
    caller      TEXT NOT NULL,
    callee      TEXT NOT NULL,
    issued_at   REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tickets_by_expiry ON tickets (expires_at);
CREATE INDEX IF NOT EXISTS tickets_by_edge ON tickets (caller_hash, callee, issued_at);
CREATE TABLE IF NOT EXISTS rules (
    rule_id      TEXT PRIMARY KEY,
    agent        TEXT NOT NULL,
    kind         TEXT NOT NULL,
    target       TEXT NOT NULL,
    mode         TEXT,
    effect       TEXT NOT NULL,
    decided_by   TEXT NOT NULL,
    requested_by TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL,
    created_at   REAL NOT NULL,
    expires_at   REAL,
    lifted_at    REAL
);
CREATE INDEX IF NOT EXISTS rules_by_agent ON rules (agent);
CREATE TABLE IF NOT EXISTS registry_version (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
INSERT OR IGNORE INTO registry_version (id, version) VALUES (1, 0);
"""


@dataclass
class SqliteAccessStore:
    """파일 하나 — 신원과 기록이 프로세스 재시작을 넘는다. 기록은 지우지 않는다."""

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
                _migrate(self._conn)
                self._conn.executescript(_SCHEMA)
            except sqlite3.Error as err:
                raise AaestroError(
                    category=ErrorCategory.STORAGE,
                    code=ErrorCode.STOR_003,
                    message="access store could not be opened",
                    details={"path": str(self.path)},
                ) from err
        return self._conn

    def put_identity(self, identity: Identity) -> None:
        with self._lock:
            self._connect().execute(
                "INSERT OR REPLACE INTO identities "
                "(credential_hash, agent, deployment_id, issued_at, revoked_at, graph) "
                "VALUES (?,?,?,?,?,?)",
                (
                    identity.credential_hash,
                    identity.agent,
                    identity.deployment_id,
                    identity.issued_at,
                    identity.revoked_at,
                    identity.graph,
                ),
            )

    def identity(self, credential_hash: str) -> Identity | None:
        with self._lock:
            row = (
                self._connect()
                .execute("SELECT * FROM identities WHERE credential_hash = ?", (credential_hash,))
                .fetchone()
            )
        return Identity(**dict(row)) if row else None

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """한 번에 커밋하거나 아무것도 남기지 않는다 — 연결은 autocommit 이라 직접 연다."""
        with self._lock:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except sqlite3.Error as err:
                conn.execute("ROLLBACK")
                raise AaestroError(
                    category=ErrorCategory.STORAGE,
                    code=ErrorCode.STOR_003,
                    message="access store write failed",
                    details={"path": str(self.path)},
                ) from err
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def revoke_identities(self, deployment_id: str, at: float) -> int:
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE identities SET revoked_at = ? "
                "WHERE deployment_id = ? AND revoked_at IS NULL",
                (at, deployment_id),
            )
            if cursor.rowcount:
                _bump(conn)
        return cursor.rowcount

    def put_rule(self, rule: Rule) -> int:
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO rules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rule.rule_id,
                    rule.agent,
                    rule.kind.value,
                    rule.target,
                    rule.mode.value if rule.mode else None,
                    rule.effect.value,
                    rule.decided_by,
                    rule.requested_by,
                    rule.reason,
                    rule.created_at,
                    rule.expires_at,
                    rule.lifted_at,
                ),
            )
            return _bump(conn)

    def rule(self, rule_id: str) -> Rule | None:
        with self._lock:
            row = (
                self._connect()
                .execute("SELECT * FROM rules WHERE rule_id = ?", (rule_id,))
                .fetchone()
            )
        return _rule(row) if row else None

    def rules(self, agent: str) -> Sequence[Rule]:
        with self._lock:
            rows = (
                self._connect()
                .execute(
                    "SELECT * FROM rules WHERE agent = ? ORDER BY created_at, rule_id", (agent,)
                )
                .fetchall()
            )
        return [_rule(row) for row in rows]

    def live_graphs(self, agent: str) -> frozenset[str]:
        with self._lock:
            rows = (
                self._connect()
                .execute(
                    "SELECT DISTINCT graph FROM identities "
                    "WHERE agent = ? AND revoked_at IS NULL AND graph != ''",
                    (agent,),
                )
                .fetchall()
            )
        return frozenset(row["graph"] for row in rows)

    def put_ticket(self, ticket: Ticket) -> None:
        with self._transaction() as conn:
            conn.execute("DELETE FROM tickets WHERE expires_at <= ?", (ticket.issued_at,))
            conn.execute(
                "INSERT OR REPLACE INTO tickets VALUES (?,?,?,?,?,?)",
                (
                    ticket.ticket_hash,
                    ticket.caller_hash,
                    ticket.caller,
                    ticket.callee,
                    ticket.issued_at,
                    ticket.expires_at,
                ),
            )
            conn.execute(
                "DELETE FROM tickets WHERE caller_hash = ? AND callee = ? AND ticket_hash NOT IN "
                "(SELECT ticket_hash FROM tickets WHERE caller_hash = ? AND callee = ? "
                "ORDER BY issued_at DESC, rowid DESC LIMIT ?)",
                (
                    ticket.caller_hash,
                    ticket.callee,
                    ticket.caller_hash,
                    ticket.callee,
                    TICKETS_PER_EDGE,
                ),
            )

    def ticket(self, ticket_hash: str) -> Ticket | None:
        with self._lock:
            row = (
                self._connect()
                .execute("SELECT * FROM tickets WHERE ticket_hash = ?", (ticket_hash,))
                .fetchone()
            )
        return Ticket(**dict(row)) if row else None

    def touch(self) -> int:
        with self._transaction() as conn:
            return _bump(conn)

    def version(self) -> int:
        with self._lock:
            row = (
                self._connect()
                .execute("SELECT version FROM registry_version WHERE id = 1")
                .fetchone()
            )
        return int(row["version"])


def _migrate(conn: sqlite3.Connection) -> None:
    """#277 에 만든 파일에는 ``graph`` 열이 없다 — 스키마 스크립트 전에 열만 더한다."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(identities)")}
    if columns and "graph" not in columns:
        conn.execute("ALTER TABLE identities ADD COLUMN graph TEXT NOT NULL DEFAULT ''")


def _bump(conn: sqlite3.Connection) -> int:
    updated = conn.execute("UPDATE registry_version SET version = version + 1 WHERE id = 1")
    if updated.rowcount != 1:
        raise sqlite3.DatabaseError("registry version row is missing")
    row = conn.execute("SELECT version FROM registry_version WHERE id = 1").fetchone()
    return int(row["version"])


def _rule(row: sqlite3.Row) -> Rule:
    return Rule(
        rule_id=row["rule_id"],
        agent=row["agent"],
        kind=ResourceKind(row["kind"]),
        target=row["target"],
        mode=Mode(row["mode"]) if row["mode"] else None,
        effect=Effect(row["effect"]),
        decided_by=row["decided_by"],
        requested_by=row["requested_by"],
        reason=row["reason"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        lifted_at=row["lifted_at"],
    )


__all__ = ["AccessStore", "Identity", "InMemoryAccessStore", "SqliteAccessStore", "Ticket"]
