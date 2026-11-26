"""Cross-process run registry.

``RunHandle`` 은 run 을 구동하는 **프로세스의 메모리에만** 있다. 그래서 다른
프로세스는 그 run 을 조회할 수도, drain 을 요청할 수도 없다 (#102).

이 저장소는 그 격차만 메운다 — **state 는 담지 않는다**. state 는 checkpointer
소관이고, 두 곳에 두면 어느 쪽이 진실인지 모호해진다.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import agent_name

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aaestro.core.topology import GraphTopology

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    graph          TEXT NOT NULL,
    mode           TEXT NOT NULL,
    status         TEXT NOT NULL,
    iteration      INTEGER NOT NULL DEFAULT 0,
    failure_streak INTEGER NOT NULL DEFAULT 0,
    drain          INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL,
    model_overrides TEXT NOT NULL DEFAULT '{}',
    memory_spaces  TEXT NOT NULL DEFAULT '{}',
    agents         TEXT NOT NULL DEFAULT '[]',
    transitions    INTEGER NOT NULL DEFAULT 0,
    owner          TEXT NOT NULL DEFAULT '',
    lease_until    REAL NOT NULL DEFAULT 0
);
"""

_ADDED_COLUMNS: dict[str, tuple[str, Any]] = {
    "model_overrides": ("TEXT NOT NULL DEFAULT '{}'", "{}"),
    # 이 컬럼이 없던 시절의 run 은 빈 스냅샷 — run space 가 없다 (#608)
    "memory_spaces": ("TEXT NOT NULL DEFAULT '{}'", "{}"),
    "agents": ("TEXT NOT NULL DEFAULT '[]'", "[]"),
    "transitions": ("INTEGER NOT NULL DEFAULT 0", 0),
    # 임대가 없던 시절의 run 은 소유자 없음 · 만료된 임대 — running 이면 재개할 수 있다 (#662)
    "owner": ("TEXT NOT NULL DEFAULT ''", ""),
    "lease_until": ("REAL NOT NULL DEFAULT 0", 0.0),
}
"""첫 스키마 이후 더해진 컬럼 — 이름 → (선언, 그 기본값). `_migrate` 가 옛 DB 에 선언을 채우고,
읽기 전용 저장소는 마이그레이션 없이 옛 DB 를 기본값으로 읽는다. 새로 더할 때 여기에도 적는다."""


def _storage_error(message: str, **details: str) -> AaestroError:
    """Run 레지스트리 실패를 STORAGE 카테고리로 옮긴다."""
    return AaestroError(
        category=ErrorCategory.STORAGE,
        code=ErrorCode.STOR_003,
        message=message,
        details=details,
    )


@dataclass(frozen=True)
class RunRecord:
    """One run as other processes can see it.

    다른 프로세스가 볼 수 있는 run 한 건. ``drain`` 은 **요청 플래그**이고,
    실제 정지는 구동 프로세스가 iteration 경계에서 수행한다.
    """

    run_id: str
    graph: str
    mode: str
    status: str
    iteration: int = 0
    failure_streak: int = 0
    drain: bool = False
    updated_at: str = ""
    model_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    """제출 시 배포에서 **복사**한 노드별 모델 override — resume 이 같은 모델로 잇는다.
    배포 기록을 다시 찾지 않으므로 배포가 사라져도 run 은 시작할 때의 모델을 쓴다.

    한 번 정해지면 바뀌지 않는다: 빈 값으로 갱신하면 기존 값을 보존한다. run 기록은
    핸들만 아는 여러 곳(슬롯 · iteration 진행)이 다시 쓰므로, 그 갱신이 지우면 안 된다."""
    memory_spaces: Mapping[str, str] = field(default_factory=dict)
    """run 이 시작할 때 그래프가 선언한 run scope space — 별칭 → memoryset ref (#608).

    처음 기록할 때만 쓰이고 그 뒤의 갱신은 바꾸지 않는다: run space 는 run 이 시작할 때의 그래프
    버전으로 정해지고 (#468 결정 a), 재개가 현재 카탈로그의 그래프로 덮으면 안 된다."""
    agents: tuple[str, ...] = ()
    """같은 시점의 노드 에이전트 이름 — run space 에 닿는 참여자. ``memory_spaces`` 와 같이
    고정이다."""
    transitions: int = 0
    """상태가 바뀐 횟수 — 저장소가 센다 (쓰는 쪽이 준 값은 무시한다). 단조 증가라 한 폴링 주기
    안에 끝 → 재개 → 끝이 몰려도 ``liveness`` 가 달라진다 (#608)."""
    owner: str = ""
    """run 을 구동하는 소유자 (``RunLease.owner``) — 빈 값은 소유자 없음 (mission · 옛 기록, #662).

    소유자가 없는 **service** 기록에 소유자가 있는 갱신이 오면 그 소유자가 처음 소유자가 된다
    (임대도 같이 — mission 기록은 소유자를 갖지 않는다).
    그 뒤로는 ``claim`` 만 바꾼다. 소유자가 있는 기록은 같은 소유자의 갱신만 받는다 — 임대를 잃은
    옛 소유자가 새 소유자의 기록을 덮지 못한다."""
    lease_until: float = 0.0
    """소유자의 임대 만료 시각 (epoch 초) — 소유자가 ``renew`` 로 미룬다. 이 시각이 지난 ``running``
    run 은 소유자가 죽은 것으로 보고 다른 프로세스가 ``claim`` 으로 가져갈 수 있다."""

    def __post_init__(self) -> None:
        # 스냅샷은 권한 판정의 근거다 — 넘겨준 dict 나 읽어 간 값을 바꿔 run space 가 늘면 안 된다
        object.__setattr__(self, "memory_spaces", MappingProxyType(dict(self.memory_spaces)))
        object.__setattr__(self, "agents", tuple(self.agents))

    def orphaned(self, now: float) -> bool:
        """소유자가 죽은 run 인가 — ``running`` 인데 임대가 ``now`` 에 만료됐다 (#662).

        draining 은 대상이 아니다: 정지를 요청받은 run 이라 재개하지 않는다. 임대가 없는
        옛 기록 (``lease_until`` 0) 은 만료로 본다.
        """
        return self.status == "running" and self.lease_until <= now

    @property
    def live(self) -> bool:
        """진행 중인가 — run space 를 쓸 수 있는 동안 (끝난 run 은 읽기만, 09 Scope Rule 1)."""
        return self.status in LIVE_STATUSES


LIVE_STATUSES = frozenset({"running", "draining"})
"""진행 중인 run 의 상태 — ``RunStatus`` 의 RUNNING · DRAINING (run.py 가 이 모듈을 import 해
여기서는 enum 을 쓰지 못한다. 두 집합이 같다는 것은 테스트가 지킨다)."""


def participation(topology: GraphTopology) -> dict[str, Any]:
    """The run-start snapshot a record keeps — run scope spaces and node agents (#608).

    run 시작 시점의 참여 스냅샷 — ``RunRecord`` 의 ``memory_spaces`` · ``agents`` 인자.
    서브그래프 노드는 에이전트가 아니라 들어가지 않는다.
    """
    return {
        "memory_spaces": {s.alias: s.ref for s in topology.spec.memory.spaces},
        "agents": tuple(sorted({agent_name(n.agent) for n in topology.spec.nodes if n.agent})),
    }


@runtime_checkable
class RunStore(Protocol):
    """Where run records live so other processes can reach them."""

    def upsert(self, record: RunRecord) -> bool:
        """run 기록을 남긴다 (없으면 생성, 있으면 갱신).

        소유자가 있는 기록을 다른 소유자가 갱신하면 쓰지 않고 False — 호출자는 이것을 무시하지
        않는다 (#662). 소유자 · 임대는 처음 기록 (또는 소유자 없는 기록을 넘겨받는 첫 갱신) 에서만
        정해지고 그 뒤로는 ``claim`` · ``renew`` 가 바꾼다.
        """
        ...

    def claim(self, run_id: str, owner: str, lease_until: float, *, now: float) -> bool:
        """소유자가 죽은 run 을 가져온다 — 원자적 조건부 갱신 (compare-and-set).

        ``RunRecord.orphaned(now)`` 일 때만 소유자와 임대를 바꾸고 True. 동시에 둘이 가져가려 해도
        하나만 이긴다: 이긴 쪽의 임대가 진 쪽의 조건을 거짓으로 만든다.
        """
        ...

    def renew(self, run_id: str, owner: str, lease_until: float) -> bool:
        """임대를 미룬다 — ``owner`` 가 아직 소유자일 때만. False 면 임대를 잃었다."""
        ...

    def release(self, run_id: str, owner: str) -> None:
        """임대를 놓는다 (만료 0) — ``owner`` 가 아직 소유자일 때만. 곧바로 다시 가져갈 수 있다."""
        ...

    def get(self, run_id: str) -> RunRecord | None:
        """run 기록 — 없으면 None."""
        ...

    def list(self, *, mode: str | None = None) -> Sequence[RunRecord]:
        """기록된 run 목록 — mode 로 좁힐 수 있다."""
        ...

    def request_drain(self, run_id: str) -> bool:
        """drain 요청을 남긴다 — 미지의 run 이면 False."""
        ...

    def liveness(self) -> tuple[int, int]:
        """기록 수와 상태 전이 수의 합 — run 의 시작 · 상태 전이마다 오른다.

        iteration 진행 (상태 불변) 으로는 오르지 않는다.

        권한 레지스트리가 이 값의 변화로 판정 캐시를 버린다 (#608). 저장소를 쓰는 프로세스가
        여럿이라 (CLI 구동 run) 쓰는 자리가 아니라 읽는 자리에서 알아챈다.
        """
        ...


@dataclass
class InMemoryRunStore:
    """A run store for a single process.

    한 프로세스 안에서만 쓰는 저장소 — 테스트와 dev 용입니다.
    """

    _records: dict[str, RunRecord] = field(default_factory=dict, init=False)

    def upsert(self, record: RunRecord) -> bool:
        """기록을 남긴다 — drain 요청은 보존한다."""
        existing = self._records.get(record.run_id)
        if existing is not None and (
            existing.owner not in ("", record.owner) or existing.mode != record.mode
        ):
            return False
        owner, lease_until = record.owner, record.lease_until
        if existing is not None and (existing.owner or existing.mode != "service"):
            owner, lease_until = existing.owner, existing.lease_until
        # 구동 프로세스의 갱신이 다른 프로세스의 drain 요청을 지우면
        # 그 요청은 영원히 전달되지 않는다
        drain = record.drain or (existing.drain if existing else False)
        overrides = record.model_overrides or (existing.model_overrides if existing else {})
        snapshot = existing if existing is not None else record
        transitions = (
            0 if existing is None else existing.transitions + (existing.status != record.status)
        )
        self._records[record.run_id] = RunRecord(
            **{
                **record.__dict__,
                "drain": drain,
                "model_overrides": overrides,
                "memory_spaces": snapshot.memory_spaces,
                "agents": snapshot.agents,
                "transitions": transitions,
                # 소유자 없는 service 기록만 처음 온 소유자가 넘겨받는다 — 그 뒤로는 claim 만 바꾼다
                "owner": owner,
                "lease_until": lease_until,
                "updated_at": _now(),
            }
        )
        return True

    def claim(self, run_id: str, owner: str, lease_until: float, *, now: float) -> bool:
        """소유자가 죽은 run 을 가져온다 — 한 프로세스 안이라 검사와 쓰기 사이에 끼어들 수 없다."""
        existing = self._records.get(run_id)
        if existing is None or not existing.orphaned(now):
            return False
        self._records[run_id] = RunRecord(
            **{**existing.__dict__, "owner": owner, "lease_until": lease_until}
        )
        return True

    def renew(self, run_id: str, owner: str, lease_until: float) -> bool:
        """임대를 미룬다 — 소유자일 때만."""
        existing = self._records.get(run_id)
        if existing is None or existing.owner != owner:
            return False
        self._records[run_id] = RunRecord(**{**existing.__dict__, "lease_until": lease_until})
        return True

    def release(self, run_id: str, owner: str) -> None:
        """임대를 놓는다 — 소유자일 때만."""
        self.renew(run_id, owner, 0.0)

    def get(self, run_id: str) -> RunRecord | None:
        """기록 조회."""
        return self._records.get(run_id)

    def list(self, *, mode: str | None = None) -> Sequence[RunRecord]:
        """기록 목록."""
        found = list(self._records.values())
        return [record for record in found if mode is None or record.mode == mode]

    def request_drain(self, run_id: str) -> bool:
        """drain 요청 플래그를 세운다."""
        existing = self._records.get(run_id)
        if existing is None:
            return False
        self._records[run_id] = RunRecord(
            **{**existing.__dict__, "drain": True, "updated_at": _now()}
        )
        return True

    def liveness(self) -> tuple[int, int]:
        """기록 수와 상태 전이 수의 합."""
        return len(self._records), sum(r.transitions for r in self._records.values())


@dataclass
class SqliteRunStore:
    """A run store other processes can read.

    같은 파일을 여는 프로세스끼리 run 을 공유합니다 — #102 의 cross-process
    조작이 성립하는 지점입니다.
    """

    path: str | Path = ":memory:"
    read_only: bool = False
    """True 면 sqlite 를 ``mode=ro`` 로 연다 — 파일을 만들지도 스키마를 쓰지도 않는다 (#665)."""
    _conn: sqlite3.Connection = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        try:
            # Control Plane 은 별도 스레드에서 서빙된다 — 기본값(스레드 고정)이면
            # 서버 스레드의 첫 질의가 ProgrammingError 로 죽는다. 쓰기는 아래
            # 락으로 직렬화한다
            if self.read_only:
                self._conn = sqlite3.connect(
                    f"{Path(self.path).resolve().as_uri()}?mode=ro",
                    uri=True,
                    isolation_level=None,
                    check_same_thread=False,
                )
                self._conn.row_factory = sqlite3.Row
                return
            self._conn = sqlite3.connect(
                str(self.path), isolation_level=None, check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute(_SCHEMA)
            _migrate(self._conn)
        except sqlite3.Error as err:
            raise _storage_error("run store could not be opened", path=str(self.path)) from err

    def upsert(self, record: RunRecord) -> bool:
        """기록을 남긴다 — 기존 drain 요청은 보존한다."""
        try:
            with self._lock:
                cursor = self._conn.execute(
                    """
                INSERT INTO runs (run_id, graph, mode, status, iteration,
                                  failure_streak, drain, updated_at, model_overrides,
                                  memory_spaces, agents, transitions, owner, lease_until)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    -- 오른쪽은 갱신 전 행을 읽는다 — status 가 바뀐 갱신만 센다
                    transitions = runs.transitions + (runs.status <> excluded.status),
                    status = excluded.status,
                    iteration = excluded.iteration,
                    failure_streak = excluded.failure_streak,
                    -- 구동 프로세스의 갱신이 다른 프로세스의 drain 요청을
                    -- 덮어쓰면 그 요청은 영원히 전달되지 않는다
                    drain = MAX(runs.drain, excluded.drain),
                    -- 핸들만 아는 갱신(슬롯 · iteration)이 제출 때 복사한 override 를 지우면
                    -- resume 이 다른 모델로 이어진다
                    model_overrides = CASE WHEN excluded.model_overrides = '{}'
                        THEN runs.model_overrides ELSE excluded.model_overrides END,
                    -- memory_spaces · agents 는 갱신하지 않는다 — run 시작 때의 스냅샷이다.
                    -- owner · lease_until 은 소유자 없는 기록만 넘겨받는다 (그 뒤는 claim · renew)
                    owner = CASE WHEN runs.owner = '' AND runs.mode = 'service'
                        THEN excluded.owner ELSE runs.owner END,
                    lease_until = CASE WHEN runs.owner = '' AND runs.mode = 'service'
                        THEN excluded.lease_until ELSE runs.lease_until END,
                    updated_at = excluded.updated_at
                -- 임대를 잃은 옛 소유자의 갱신이 새 소유자의 기록을 덮지 않는다 (#662)
                -- mode 가 다른 갱신은 다른 run 이다 — 같은 id 를 먼저 쓴 mission 을 덮지 않는다
                WHERE (runs.owner = '' OR runs.owner = excluded.owner)
                    AND runs.mode = excluded.mode
                """,
                    (
                        record.run_id,
                        record.graph,
                        record.mode,
                        record.status,
                        record.iteration,
                        record.failure_streak,
                        int(record.drain),
                        _now(),
                        json.dumps(record.model_overrides),
                        json.dumps(dict(record.memory_spaces)),
                        json.dumps(list(record.agents)),
                        record.owner,
                        record.lease_until,
                    ),
                )
        except sqlite3.Error as err:
            raise _storage_error("run record could not be stored", run_id=record.run_id) from err
        return cursor.rowcount > 0

    def claim(self, run_id: str, owner: str, lease_until: float, *, now: float) -> bool:
        """소유자가 죽은 run 을 가져온다 — 조건과 쓰기가 한 UPDATE 라 프로세스 사이에도 원자적."""
        try:
            with self._lock:
                cursor = self._conn.execute(
                    """
                UPDATE runs SET owner = ?, lease_until = ?, updated_at = ?
                WHERE run_id = ? AND status = 'running' AND lease_until <= ?
                """,
                    (owner, lease_until, _now(), run_id, now),
                )
        except sqlite3.Error as err:
            raise _storage_error("run could not be claimed", run_id=run_id) from err
        return cursor.rowcount > 0

    def renew(self, run_id: str, owner: str, lease_until: float) -> bool:
        """임대를 미룬다 — 소유자일 때만. 갱신 순서 (``updated_at``) 는 건드리지 않는다."""
        try:
            with self._lock:
                cursor = self._conn.execute(
                    "UPDATE runs SET lease_until = ? WHERE run_id = ? AND owner = ?",
                    (lease_until, run_id, owner),
                )
        except sqlite3.Error as err:
            raise _storage_error("run lease could not be renewed", run_id=run_id) from err
        return cursor.rowcount > 0

    def release(self, run_id: str, owner: str) -> None:
        """임대를 놓는다 — 소유자일 때만."""
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE runs SET lease_until = 0 WHERE run_id = ? AND owner = ?",
                    (run_id, owner),
                )
        except sqlite3.Error as err:
            raise _storage_error("run lease could not be released", run_id=run_id) from err

    def get(self, run_id: str) -> RunRecord | None:
        """기록 조회 — 다른 프로세스가 쓴 것도 보인다."""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            return _record(row) if row is not None else None
        except sqlite3.Error as err:
            raise _storage_error("run record could not be read", run_id=run_id) from err
        except ValueError as err:
            # 손상된 행(깨진 JSON)도 저장소 읽기 실패다
            raise _storage_error("run record could not be decoded", run_id=run_id) from err

    def list(self, *, mode: str | None = None) -> Sequence[RunRecord]:
        """기록 목록 — 최근 갱신 순."""
        try:
            with self._lock:
                if mode is None:
                    rows = self._conn.execute(
                        "SELECT * FROM runs ORDER BY updated_at DESC"
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        "SELECT * FROM runs WHERE mode = ? ORDER BY updated_at DESC", (mode,)
                    ).fetchall()
            return [_record(row) for row in rows]
        except sqlite3.Error as err:
            raise _storage_error("run records could not be listed") from err
        except ValueError as err:
            raise _storage_error("run records could not be decoded") from err

    def request_drain(self, run_id: str) -> bool:
        """drain 요청을 남긴다 — 구동 프로세스가 iteration 경계에서 읽는다."""
        try:
            with self._lock:
                cursor = self._conn.execute(
                    "UPDATE runs SET drain = 1, updated_at = ? WHERE run_id = ?",
                    (_now(), run_id),
                )
        except sqlite3.Error as err:
            raise _storage_error("drain request could not be recorded", run_id=run_id) from err
        return cursor.rowcount > 0

    def liveness(self) -> tuple[int, int]:
        """기록 수와 상태 전이 수의 합 — 다른 프로세스가 쓴 전이도 보인다."""
        try:
            with self._lock:
                count, transitions = self._conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(transitions), 0) FROM runs"
                ).fetchone()
        except sqlite3.Error as err:
            raise _storage_error("run liveness could not be read") from err
        return int(count), int(transitions)

    def close(self) -> None:
        """연결을 닫는다."""
        try:
            self._conn.close()
        except sqlite3.Error as err:
            raise _storage_error("run store could not be closed") from err


def _record(row: sqlite3.Row) -> RunRecord:
    """저장 행을 기록으로 — 마이그레이션 전 DB (읽기 전용) 의 없는 컬럼은 기본값으로 읽는다."""
    present = set(row.keys())

    def column(name: str) -> Any:
        return row[name] if name in present else _ADDED_COLUMNS[name][1]

    return RunRecord(
        run_id=row["run_id"],
        graph=row["graph"],
        mode=row["mode"],
        status=row["status"],
        iteration=row["iteration"],
        failure_streak=row["failure_streak"],
        drain=bool(row["drain"]),
        updated_at=row["updated_at"],
        model_overrides=json.loads(column("model_overrides")),
        memory_spaces=json.loads(column("memory_spaces")),
        agents=tuple(json.loads(column("agents"))),
        transitions=column("transitions"),
        owner=column("owner"),
        lease_until=column("lease_until"),
    )


def _migrate(conn: sqlite3.Connection) -> None:
    """있는 DB 에 빠진 컬럼을 더한다 — `CREATE TABLE IF NOT EXISTS` 는 그러지 않는다 (#254)."""
    for name, (declaration, _default) in _ADDED_COLUMNS.items():
        if name in _columns(conn):
            continue
        try:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {declaration}")  # noqa: S608 — 상수
        except sqlite3.OperationalError:
            # 같은 옛 DB 를 연 다른 프로세스가 먼저 더했으면 성공이다 — 아니면 진짜 실패
            if name not in _columns(conn):
                raise


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}


def _now() -> str:
    """갱신 시각 — 정렬과 관측에 쓴다."""
    return datetime.now(UTC).isoformat()


__all__ = [
    "LIVE_STATUSES",
    "InMemoryRunStore",
    "RunRecord",
    "RunStore",
    "SqliteRunStore",
    "participation",
]
