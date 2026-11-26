"""Service run ownership leases — resuming a run whose driving process died (#662).

run 저장소는 control plane 과 CLI 가 같이 쓰고 orchestrator 인스턴스는 여럿일 수 있다. 그래서
"이 프로세스가 모른다" 로는 죽은 run 을 가르지 못한다 — 소유자가 임대를 계속 미루고, 임대가 지난
``running`` run 만 다른 프로세스가 조건부 갱신 (compare-and-set) 으로 가져간다.

"프로세스" 는 같은 sqlite 파일을 여는 별도 ``SqliteRunStore`` 와 manager 로, 시간은 fake clock 으로
흉내 낸다 (06 — 실제 sleep 금지). checkpointer 는 외부 저장소를 흉내 내 두 프로세스가 공유한다.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from typing import Any

import pytest
import structlog

from aaestro.core.agent import TaskResult
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.orchestrator import run as run_module
from aaestro.orchestrator import submit as submit_module
from aaestro.orchestrator.checkpoint import build_checkpointer
from aaestro.orchestrator.run import DEFAULT_RUN_LEASE_S, RunLease, RunManager, RunStatus
from aaestro.orchestrator.runstore import InMemoryRunStore, RunRecord, SqliteRunStore
from aaestro.orchestrator.submit import RunSubmitter
from tests.fixtures.topologies import make_service

T0 = 1_000_000.0
AFTER_EXPIRY = T0 + DEFAULT_RUN_LEASE_S + 1


@pytest.fixture(autouse=True)
def fresh_loggers(monkeypatch):
    """이 파일이 모듈 로거를 굳히지 않게 한다 — 앞선 테스트가 `cache_logger_on_first_use` 로
    설정해 두면 여기서 처음 쓴 로거가 굳어, 뒤 테스트의 `capture_logs` 가 보지 못한다."""
    monkeypatch.setattr(run_module, "log", structlog.get_logger("test"))
    monkeypatch.setattr(submit_module, "log", structlog.get_logger("test"))


class Clock:
    """주입하는 시계 — 테스트가 시간을 옮긴다."""

    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def no_sleep(_delay: float) -> None:
    """idle backoff 를 즉시 통과시킨다."""


async def never(_delay: float) -> None:
    """갱신이 멈춘 소유자 — 죽은 프로세스의 임대는 다시 미뤄지지 않는다."""
    await asyncio.Event().wait()


class Gate:
    """첫 갱신 주기만 테스트가 열어 준다 — 그 뒤 주기는 오지 않는다 (busy loop 방지)."""

    def __init__(self) -> None:
        self.opened = asyncio.Event()
        self.calls = 0

    async def __call__(self, _delay: float) -> None:
        self.calls += 1
        await (self.opened if self.calls == 1 else asyncio.Event()).wait()


class Watcher:
    """watcher 노드를 센다 — ``block_at`` 번째 호출에서 영원히 멈춘다 (죽기 직전의 회차)."""

    def __init__(self, block_at: int | None = None) -> None:
        self.block_at = block_at
        self.calls = 0
        self.blocked = asyncio.Event()
        self.cancelled = False

    async def invoke(self, node: Any, task: Any) -> TaskResult:
        if task.node_id != "watcher":
            return TaskResult.completed(task, output={})
        self.calls += 1
        if self.calls == self.block_at:
            self.blocked.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return TaskResult.completed(task, output={"plan": f"plan-{self.calls}"})


REQUIRED_STATE = {"schema": "aaestro.graphs.schemas:ResearchState"}
NODES = [
    {"id": "watcher", "agent": "agents/feed-watcher@0.1.0", "output_map": {"plan": "output.plan"}},
    {"id": "notifier", "agent": "agents/notifier@0.1.0"},
]


def topology() -> Any:
    return make_service(state=REQUIRED_STATE, nodes=NODES)


def process(
    path: str,
    runtime: Any,
    checkpointer: Any,
    *,
    owner: str,
    clock: Clock,
    sleep: Any = never,
    store: Any = None,
) -> RunSubmitter:
    """저장소 파일과 checkpointer 를 공유하는 프로세스 하나."""
    return RunSubmitter(
        runtime=runtime,
        manager=RunManager(
            store=store if store is not None else SqliteRunStore(path=path),
            lease=RunLease(owner=owner, clock=clock, sleep=sleep),
        ),
        checkpointer=checkpointer,
    )


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "runs.db")


async def died_mid_iteration(path: str, checkpointer: Any, **kwargs: Any) -> Any:
    """두 회차를 마치고 세 번째 회차 도중 멈춘 소유자 — 임대는 더 미뤄지지 않는다."""
    runtime = Watcher(block_at=3)
    owner = process(path, runtime, checkpointer, owner="a", clock=Clock(), **kwargs)
    await owner.start_service(topology(), {"query": "q"}, run_id="svc", sleep=no_sleep)
    await runtime.blocked.wait()
    return owner, runtime


async def bury(owner: RunSubmitter) -> None:
    """죽은 소유자의 태스크를 거둔다 — 테스트가 태스크를 남기지 않게."""
    for task in list(owner.services.values()):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# --- 저장소 계약 -------------------------------------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryRunStore()
        return
    opened = SqliteRunStore(path=tmp_path / "runs.db")
    try:
        yield opened
    finally:
        opened.close()


def owned(status: str = "running", *, owner: str = "a", lease_until: float = T0) -> RunRecord:
    return RunRecord(
        run_id="svc",
        graph="feed-monitor",
        mode="service",
        status=status,
        owner=owner,
        lease_until=lease_until,
    )


def test_a_live_lease_cannot_be_claimed(store):
    store.upsert(owned())

    assert store.claim("svc", "b", T0 + 30, now=T0 - 1) is False
    assert store.get("svc").owner == "a"


def test_an_expired_running_lease_is_claimed_once(store):
    store.upsert(owned())

    assert store.claim("svc", "b", AFTER_EXPIRY + 30, now=AFTER_EXPIRY) is True
    # 이긴 쪽의 임대가 진 쪽의 조건을 거짓으로 만든다
    assert store.claim("svc", "c", AFTER_EXPIRY + 30, now=AFTER_EXPIRY) is False
    assert store.get("svc").owner == "b"
    assert store.get("svc").lease_until == AFTER_EXPIRY + 30


@pytest.mark.parametrize("status", ["halted", "stopped", "draining", "completed"])
def test_only_running_runs_are_claimed(store, status):
    """drain 으로 정지 · 정지 요청 중인 run 은 죽었어도 재개 대상이 아니다."""
    store.upsert(owned(status))

    assert store.claim("svc", "b", AFTER_EXPIRY + 30, now=AFTER_EXPIRY) is False


def test_a_record_without_a_lease_counts_as_expired(store):
    """임대가 없던 시절의 running 기록 — 소유자가 살아 있는지 알 길이 없어 만료로 본다."""
    store.upsert(RunRecord(run_id="svc", graph="feed-monitor", mode="service", status="running"))

    assert store.claim("svc", "b", T0 + 30, now=T0) is True


def test_another_owner_cannot_overwrite_the_record(store):
    """임대를 잃은 옛 소유자의 갱신이 새 소유자의 기록을 덮지 않는다."""
    store.upsert(owned())
    store.claim("svc", "b", AFTER_EXPIRY + 30, now=AFTER_EXPIRY)

    written = store.upsert(
        RunRecord(**{**owned().__dict__, "status": "stopped", "iteration": 9, "owner": "a"})
    )

    assert written is False
    assert store.get("svc").status == "running"
    assert store.get("svc").iteration == 0
    assert store.upsert(RunRecord(**{**owned().__dict__, "iteration": 3, "owner": "b"})) is True
    assert store.get("svc").iteration == 3
    assert store.get("svc").lease_until == AFTER_EXPIRY + 30  # 갱신은 임대를 바꾸지 않는다


def test_only_the_owner_renews(store):
    store.upsert(owned())

    assert store.renew("svc", "b", T0 + 99) is False
    assert store.renew("svc", "a", T0 + 99) is True
    assert store.get("svc").lease_until == T0 + 99


def test_concurrent_claims_from_two_processes_have_one_winner(tmp_path):
    """두 연결이 동시에 가져가도 하나만 이긴다 — 검사와 쓰기가 한 UPDATE 다."""
    path = tmp_path / "runs.db"
    SqliteRunStore(path=path).upsert(owned())
    stores = [SqliteRunStore(path=path) for _ in range(8)]
    barrier = threading.Barrier(len(stores))
    wins: list[bool] = []

    def claim(store: SqliteRunStore, owner: str) -> None:
        barrier.wait()
        wins.append(store.claim("svc", owner, AFTER_EXPIRY + 30, now=AFTER_EXPIRY))

    threads = [threading.Thread(target=claim, args=(s, f"p{i}")) for i, s in enumerate(stores)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(wins) == [False] * (len(stores) - 1) + [True]


def test_a_database_from_before_leases_opens_and_its_running_runs_are_claimable(tmp_path):
    path = tmp_path / "runs.db"
    with sqlite3.connect(path) as old:
        old.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, graph TEXT NOT NULL, "
            "mode TEXT NOT NULL, status TEXT NOT NULL, iteration INTEGER NOT NULL DEFAULT 0, "
            "failure_streak INTEGER NOT NULL DEFAULT 0, drain INTEGER NOT NULL DEFAULT 0, "
            "updated_at TEXT NOT NULL)"
        )
        old.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)",
            ("svc", "feed-monitor", "service", "running", 4, 0, 0, "t"),
        )

    store = SqliteRunStore(path=path)
    try:
        assert store.get("svc").owner == ""
        assert store.get("svc").lease_until == 0
        assert store.claim("svc", "b", T0 + 30, now=T0) is True
    finally:
        store.close()


# --- 재개 --------------------------------------------------------------------------


async def test_a_run_whose_owner_died_resumes_from_its_last_iteration(path):
    """완료 조건 1 — 임대가 지난 running run 을 같은 id 로, 마지막 checkpoint 에서 잇는다."""
    checkpointer = build_checkpointer("memory")
    dead, _ = await died_mid_iteration(path, checkpointer)
    runtime = Watcher()
    successor = process(path, runtime, checkpointer, owner="b", clock=Clock(AFTER_EXPIRY))

    handle = await successor.resume_service(topology(), "svc", max_iterations=4, sleep=no_sleep)
    await successor.services[handle.run_id]

    # 원 기록을 잇는다 — 새 id 를 만들면 원 기록이 running 인 채 남는다
    assert handle.run_id == "svc"
    # 죽으며 멈춘 회차 (2) 는 실패한 회차로 본다 — 그 thread 를 다시 쓰지 않고 3 부터 연다
    assert runtime.calls == 1
    assert handle.iteration == 4
    assert handle.state["query"] == "q"  # 필수 필드가 checkpoint 에서 왔다
    record = successor.manager.store.get("svc")
    assert (record.owner, record.status, record.iteration) == ("b", "stopped", 4)

    await bury(dead)
    # 죽은 소유자의 마지막 기록 (취소 · 슬롯 반납) 이 새 소유자의 기록을 덮지 않는다
    record = successor.manager.store.get("svc")
    assert (record.owner, record.status, record.iteration) == ("b", "stopped", 4)


async def test_a_run_with_a_live_lease_is_not_resumed(path):
    """완료 조건 2 — 다른 프로세스든 이 프로세스든 임대가 살아 있으면 GRAPH_006."""
    checkpointer = build_checkpointer("memory")
    owner, _ = await died_mid_iteration(path, checkpointer)
    elsewhere = process(path, Watcher(), checkpointer, owner="b", clock=Clock(T0 + 1))

    for resumer in (elsewhere, owner):
        with pytest.raises(AaestroError) as excinfo:
            await resumer.resume_service(topology(), "svc", max_iterations=4, sleep=no_sleep)
        assert excinfo.value.code == ErrorCode.GRAPH_006

    assert elsewhere.manager.store.get("svc").owner == "a"
    await bury(owner)


async def test_two_resumes_of_a_dead_run_have_one_winner(path):
    checkpointer = build_checkpointer("memory")
    dead, _ = await died_mid_iteration(path, checkpointer)
    resumers = [
        process(path, Watcher(), checkpointer, owner=name, clock=Clock(AFTER_EXPIRY))
        for name in ("b", "c")
    ]

    outcomes = await asyncio.gather(
        *(r.resume_service(topology(), "svc", max_iterations=4, sleep=no_sleep) for r in resumers),
        return_exceptions=True,
    )

    errors = [o for o in outcomes if isinstance(o, AaestroError)]
    assert [e.code for e in errors] == [ErrorCode.GRAPH_006]
    winner = resumers[outcomes.index(next(o for o in outcomes if not isinstance(o, Exception)))]
    await winner.services["svc"]
    await bury(dead)


async def test_a_drained_run_is_still_not_resumed(path):
    """완료 조건 3 — drain 으로 정상 정지한 run 은 임대가 지나도 재개 대상이 아니다."""
    checkpointer = build_checkpointer("memory")
    first = process(path, Watcher(), checkpointer, owner="a", clock=Clock())
    handle = await first.start_service(topology(), {"query": "q"}, run_id="svc", sleep=no_sleep)
    await first.drain_service(handle.run_id)
    later = process(path, Watcher(), checkpointer, owner="b", clock=Clock(AFTER_EXPIRY))

    with pytest.raises(AaestroError) as excinfo:
        await later.resume_service(topology(), "svc", max_iterations=4, sleep=no_sleep)

    assert excinfo.value.code == ErrorCode.GRAPH_006


# --- 임대 상실 ---------------------------------------------------------------------


async def test_an_owner_that_lost_its_lease_stops_mid_iteration(path):
    """완료 조건 4 — 갱신이 남에게 넘어간 것을 알면 진행 중인 회차를 끊고 더 쓰지 않는다."""
    checkpointer = build_checkpointer("memory")
    gate = Gate()
    owner, runtime = await died_mid_iteration(path, checkpointer, sleep=gate)
    task = owner.services["svc"]
    taker = process(path, Watcher(), checkpointer, owner="b", clock=Clock(AFTER_EXPIRY))
    assert taker.manager.claim("svc") is True

    gate.opened.set()
    handle = await asyncio.wait_for(task, timeout=5)

    assert handle.lease_lost
    assert runtime.cancelled  # 진행 중이던 노드를 끊었다 — 더는 checkpoint 를 남기지 않는다
    record = taker.manager.store.get("svc")
    assert (record.owner, record.status, record.iteration) == ("b", "running", 2)


async def test_an_owner_that_cannot_renew_stops_once_its_lease_ends():
    """저장소에 닿지 못한 채 임대가 끝나면 남이 곧 가져갈 수 있다 — 스스로 멈춘다."""

    class Unreachable(InMemoryRunStore):
        def renew(self, run_id: str, owner: str, lease_until: float) -> bool:
            raise AaestroError(
                category=ErrorCategory.STORAGE, code=ErrorCode.STOR_003, message="db locked"
            )

    gate = Gate()
    owner, runtime = await died_mid_iteration(
        "", build_checkpointer("memory"), sleep=gate, store=Unreachable()
    )
    owner.manager.lease.clock.now = AFTER_EXPIRY  # 갱신을 못 한 채 임대 기간이 지났다

    gate.opened.set()
    handle = await asyncio.wait_for(owner.services["svc"], timeout=5)

    assert handle.lease_lost
    assert runtime.cancelled


async def test_an_owner_sees_the_loss_at_the_iteration_boundary():
    """회차 사이에 소유자가 바뀐 것을 보면 다음 회차를 열지 않는다."""
    store = InMemoryRunStore()

    class TakenOver(Watcher):
        async def invoke(self, node: Any, task: Any) -> TaskResult:
            if task.node_id == "watcher" and self.calls == 0:
                assert store.claim("svc", "b", AFTER_EXPIRY + 30, now=AFTER_EXPIRY)
            return await super().invoke(node, task)

    owner = process(
        "", TakenOver(), build_checkpointer("memory"), owner="a", clock=Clock(), store=store
    )
    handle = await owner.start_service(
        topology(), {"query": "q"}, run_id="svc", max_iterations=5, sleep=no_sleep
    )
    await owner.services["svc"]

    assert handle.lease_lost
    assert handle.iteration == 1
    # 회차 0 의 진행 기록도 새 소유자의 기록을 덮지 않았다
    assert (store.get("svc").owner, store.get("svc").iteration) == ("b", 0)


def test_mission_records_carry_no_owner(store):
    """mission 재개는 임대를 보지 않는다 — 소유자를 두면 다른 프로세스의 재개 공지가 막힌다."""
    from tests.fixtures.topologies import make_mission

    manager = RunManager(store=store, lease=RunLease(owner="a"))
    handle = manager.acquire(make_mission())

    assert store.get(handle.run_id).owner == ""
    assert manager.lease.owner == "a"
    assert RunStatus(store.get(handle.run_id).status) is RunStatus.RUNNING
