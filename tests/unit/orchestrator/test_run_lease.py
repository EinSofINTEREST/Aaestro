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
    assert taker.manager.claim("svc") is not None

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


def test_mission_records_carry_the_same_lease(store):
    """mission 도 service 와 같은 소유자 · 임대를 쓴다 — 죽은 소유자의 mission 재개 근거 (#723)."""
    from tests.fixtures.topologies import make_mission

    manager = RunManager(store=store, lease=RunLease(owner="a", clock=Clock()))
    handle = manager.acquire(make_mission())

    found = store.get(handle.run_id)
    assert (found.owner, found.lease_until) == ("a", T0 + DEFAULT_RUN_LEASE_S)
    assert RunStatus(found.status) is RunStatus.RUNNING


# --- 시간 여유 · 조용한 거절 · 임대 놓기 · 옛 DB 읽기 (qa 피드백) ----------------------------


class RenewFails(SqliteRunStore):
    """살아 있지만 임대를 미루지 못하는 소유자의 저장소 — 같은 파일을 다른 프로세스도 쓴다."""

    def renew(self, run_id: str, owner: str, lease_until: float) -> bool:
        raise AaestroError(category=ErrorCategory.STORAGE, code=ErrorCode.STOR_003, message="x")


async def test_a_live_owner_that_cannot_renew_stops_before_anyone_can_claim(path, monkeypatch):
    """옛 소유자가 살아 있는데 갱신만 못 하면 — 만료 margin 전에 멈춰 같은 회차를 두 벌 돌지 않는다.

    회차 2 의 첫 checkpoint 를 쓰기 전에 멈춘 소유자가, 남이 가져간 뒤 풀려나 같은 thread 에
    쓰던 결함 (qa R3) 의 재현이다.
    """
    from aaestro.orchestrator.builder import build_graph
    from aaestro.orchestrator.run import ServiceRunner, iteration_thread

    checkpointer = build_checkpointer("memory")
    entered, original = asyncio.Event(), ServiceRunner._iterate

    async def stalls_before_checkpoint(self, handle, state):
        if handle.iteration == 2 and self._lease.owner == "a":
            entered.set()
            await asyncio.Event().wait()  # 경계 검사는 지났고 첫 checkpoint 는 아직이다
        return await original(self, handle, state)

    monkeypatch.setattr(ServiceRunner, "_iterate", stalls_before_checkpoint)
    clock, gate = Clock(), Gate()
    stalled = process(
        path,
        Watcher(),
        checkpointer,
        owner="a",
        clock=clock,
        sleep=gate,
        store=RenewFails(path=path),
    )
    await stalled.start_service(topology(), {"query": "q"}, run_id="svc", sleep=no_sleep)
    await entered.wait()

    # 갱신 주기 두 번이 실패한 시각 — 만료 (T0+30) 보다 margin 만큼 이르다
    clock.now = T0 + DEFAULT_RUN_LEASE_S - DEFAULT_RUN_LEASE_S / 3
    gate.opened.set()
    handle = await asyncio.wait_for(stalled.services["svc"], timeout=5)
    assert handle.lease_lost

    successor = process(path, Watcher(), checkpointer, owner="b", clock=Clock(AFTER_EXPIRY))
    resumed = await successor.resume_service(topology(), "svc", max_iterations=3, sleep=no_sleep)
    await successor.services["svc"]

    graph = build_graph(topology(), Watcher(), checkpointer=checkpointer)
    config = {"configurable": {"thread_id": iteration_thread("svc", 2)}}
    inputs = [
        h async for h in graph.aget_state_history(config) if h.metadata.get("source") == "input"
    ]
    assert resumed.iteration == 3
    assert len(inputs) == 1  # 회차 2 를 쓴 것은 이어받은 쪽 하나뿐이다


@pytest.mark.parametrize("mode", ["mission", "service"])
def test_an_ownerless_record_takes_its_first_owner(store, mode):
    """소유자 없는 기록 (옛 기록 · 제출 공지) 은 처음 쓰는 소유자를 받는다 — 두 모드 공통 (#723)."""
    store.upsert(RunRecord(run_id="r", graph="g", mode=mode, status="running"))

    first = RunRecord(run_id="r", graph="g", mode=mode, status="running", owner="a")
    assert store.upsert(RunRecord(**{**first.__dict__, "lease_until": T0 + 30})) is True

    assert (store.get("r").owner, store.get("r").lease_until) == ("a", T0 + 30)
    # 그 뒤 다른 소유자 · 소유자 없는 쓰기는 받지 않는다
    assert store.upsert(RunRecord(**{**first.__dict__, "owner": "b"})) is False
    assert store.upsert(RunRecord(run_id="r", graph="g", mode=mode, status="failed")) is False


@pytest.mark.parametrize("mode", ["mission", "service"])
async def test_a_new_submission_does_not_take_over_an_existing_ownerless_record(path, mode):
    """남의 mission · #662 이전 바이너리가 돌리는 run 을 새 제출이 빼앗지 않는다 (qa repro4 B)."""
    SqliteRunStore(path=path).upsert(
        RunRecord(run_id="m1", graph="other", mode=mode, status="running")
    )
    submitter = process(path, Watcher(), build_checkpointer("memory"), owner="b", clock=Clock())

    with pytest.raises(AaestroError) as excinfo:
        await submitter.start_service(topology(), {"query": "q"}, run_id="m1", sleep=no_sleep)

    assert excinfo.value.code == ErrorCode.GRAPH_006
    assert submitter.services == {}
    found = SqliteRunStore(path=path).get("m1")
    assert (found.owner, found.mode, found.graph) == ("", mode, "other")


async def test_starting_over_a_run_another_process_owns_is_refused(path):
    """남이 끝낸 run id 로 시작하면 202 뒤 조용히 멈추지 않고 곧바로 GRAPH_006 (qa repro2)."""
    first = process(path, Watcher(), build_checkpointer("memory"), owner="one", clock=Clock())
    await first.start_service(
        topology(), {"query": "q"}, run_id="svc", sleep=no_sleep, max_iterations=1
    )
    await first.services["svc"]
    second = process(path, Watcher(), build_checkpointer("memory"), owner="two", clock=Clock())

    with pytest.raises(AaestroError) as excinfo:
        await second.start_service(topology(), {"query": "q"}, run_id="svc", sleep=no_sleep)

    assert excinfo.value.code == ErrorCode.GRAPH_006
    assert second.services == {}
    assert second.manager.runs == {}
    assert second.manager.store.get("svc").owner == "one"


async def test_a_failed_resume_lets_go_of_the_lease(path):
    """가져온 뒤 재개가 실패하면 임대를 놓는다 — 다음 재개가 임대 기간 내내 409 를 받지 않는다."""
    SqliteRunStore(path=path).upsert(owned(lease_until=T0))  # checkpoint 없는 죽은 run
    first = process(
        path, Watcher(), build_checkpointer("memory"), owner="b", clock=Clock(AFTER_EXPIRY)
    )

    with pytest.raises(AaestroError) as excinfo:
        await first.resume_service(topology(), "svc", max_iterations=1, sleep=no_sleep)
    assert excinfo.value.code == ErrorCode.STOR_002
    assert first.manager.store.get("svc").lease_until == 0

    retry = process(
        path, Watcher(), build_checkpointer("memory"), owner="c", clock=Clock(AFTER_EXPIRY)
    )
    with pytest.raises(AaestroError) as excinfo:
        await retry.resume_service(topology(), "svc", max_iterations=1, sleep=no_sleep)
    assert excinfo.value.code == ErrorCode.STOR_002  # 409 가 아니라 다시 가져가 같은 원인으로 실패


def test_a_read_only_store_reads_a_database_from_before_the_added_columns(tmp_path):
    """읽기 전용 저장소는 마이그레이션하지 않는다 — 없는 컬럼은 기본값으로 읽는다."""
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

    store = SqliteRunStore(path=path, read_only=True)
    try:
        found = store.get("svc")
        listed = store.list()
    finally:
        store.close()

    assert (found.owner, found.lease_until, found.transitions, found.agents) == ("", 0, 0, ())
    assert found.model_overrides == {}
    assert [r.run_id for r in listed] == ["svc"]


@pytest.mark.parametrize(
    ("first_delay", "later_delay"),
    [
        (4.9, 5.0),  # qa repro3 — 잠금 대기가 주기마다 여유를 조금씩 먹는다
        (0.0, 15.0),  # 정지 시각 전에 시작한 호출이 정지 시각을 넘겨 끝난다 — 끝나자마자 멈춘다
    ],
)
async def test_a_lease_holder_stops_before_expiry_even_when_renewal_is_slow(
    first_delay, later_delay
):
    """저장소 호출의 지연이 여유를 먹는다 — 호출이 끝난 뒤의 시각으로 판정해 만료 전에 멈춘다.

    잠긴 sqlite 는 갱신 한 번에 최대 5초를 쓴다 (qa repro3: 만료 4.9초 뒤에 멈췄다).
    """
    from aaestro.core.topology import GraphMode
    from aaestro.orchestrator.run import RunHandle, hold_lease

    clock = Clock()

    async def sleep(delay: float) -> None:
        clock.now += delay

    class Locked(InMemoryRunStore):
        calls = 0

        def renew(self, run_id: str, owner: str, lease_until: float) -> bool:
            self.calls += 1
            clock.now += first_delay if self.calls == 1 else later_delay  # 잠금 대기
            if self.calls == 1:
                return super().renew(run_id, owner, lease_until)
            raise AaestroError(
                category=ErrorCategory.STORAGE, code=ErrorCode.STOR_003, message="locked"
            )

    store = Locked()
    store.upsert(owned(lease_until=T0 + DEFAULT_RUN_LEASE_S))
    handle = RunHandle(
        run_id="svc",
        graph="feed-monitor",
        mode=GraphMode.SERVICE,
        lease_until=T0 + DEFAULT_RUN_LEASE_S,
    )
    stopped: list[float] = []

    await hold_lease(
        store,
        RunLease(owner="a", clock=clock, sleep=sleep),
        handle,
        lambda: stopped.append(clock.now),
    )

    written = store.get("svc").lease_until
    assert handle.lease_lost
    assert stopped and stopped[0] < written  # 남이 가져갈 수 있게 되기 전에 멈췄다
    assert not store.get("svc").orphaned(stopped[0])


async def test_a_failing_let_go_does_not_hide_why_the_resume_failed(path):
    """임대를 놓지 못한 것은 WARN 이고, 재개가 실패한 원래 이유가 올라간다."""

    class CannotRelease(SqliteRunStore):
        def release(self, run_id: str, owner: str) -> None:
            raise AaestroError(
                category=ErrorCategory.STORAGE, code=ErrorCode.STOR_003, message="locked"
            )

    SqliteRunStore(path=path).upsert(owned(lease_until=T0))  # checkpoint 없는 죽은 run
    resumer = process(
        path,
        Watcher(),
        build_checkpointer("memory"),
        owner="b",
        clock=Clock(AFTER_EXPIRY),
        store=CannotRelease(path=path),
    )

    with pytest.raises(AaestroError) as excinfo:
        await resumer.resume_service(topology(), "svc", max_iterations=1, sleep=no_sleep)

    assert excinfo.value.code == ErrorCode.STOR_002


def test_a_record_of_another_mode_is_not_overwritten(store):
    """검사와 기록 사이에 남의 mission 이 같은 id 를 먼저 쓰면 service 기록은 거절된다 (qa 3차)."""
    mission = RunRecord(run_id="svc", graph="other", mode="mission", status="running")
    store.upsert(mission)

    assert store.upsert(owned()) is False
    found = store.get("svc")
    assert (found.mode, found.graph, found.owner) == ("mission", "other", "")


class Recovers(SqliteRunStore):
    """잠깐 갱신을 못 하다 회복하는 저장소."""

    failing = False

    def renew(self, run_id: str, owner: str, lease_until: float) -> bool:
        if self.failing:
            raise AaestroError(
                category=ErrorCategory.STORAGE, code=ErrorCode.STOR_003, message="locked"
            )
        return super().renew(run_id, owner, lease_until)


async def test_the_process_that_lost_the_lease_can_resume_once_it_expires(path):
    """임대를 잃고 멈춘 프로세스의 로컬 핸들 (STOPPED) 이 재개를 막지 않는다 — 기록을 읽는다."""
    clock, gate, store = Clock(), Gate(), Recovers(path=path)
    runtime = Watcher(block_at=3)
    owner = process(
        path, runtime, build_checkpointer("memory"), owner="a", clock=clock, sleep=gate, store=store
    )
    await owner.start_service(topology(), {"query": "q"}, run_id="svc", sleep=no_sleep)
    await runtime.blocked.wait()

    store.failing = True
    clock.now = T0 + DEFAULT_RUN_LEASE_S - DEFAULT_RUN_LEASE_S / 3
    gate.opened.set()
    stopped = await asyncio.wait_for(owner.services["svc"], timeout=5)
    assert stopped.lease_lost
    assert owner.manager.runs["svc"].status is RunStatus.STOPPED
    assert store.get("svc").status == "running"

    store.failing = False
    clock.now = AFTER_EXPIRY
    resumed = await owner.resume_service(topology(), "svc", max_iterations=4, sleep=no_sleep)
    await owner.services["svc"]

    assert resumed.run_id == "svc"
    assert resumed.iteration == 4
    assert store.get("svc").owner == "a"


async def test_an_orphan_taken_over_and_killed_again_still_resumes(path):
    """`:resumed` 처럼 0 이 아닌 회차에서 시작한 run 을 가져온 직후 다시 죽어도 다음 재개가 잇는다.

    가져올 때 회차 0 으로 먼저 기록하면 다음 재개가 없는 회차 0 thread 를 찾아 STOR_002 였다.
    """
    from tests.unit.orchestrator.test_cross_process_resume import (
        PlansThenFails,
        accumulating_service,
    )

    graph = accumulating_service()
    checkpointer = build_checkpointer("memory")
    first = process(path, PlansThenFails(succeed=2), checkpointer, owner="p1", clock=Clock())
    await first.start_service(graph, {"query": "q"}, run_id="svc", sleep=no_sleep)
    halted = await first.services["svc"]
    assert halted.status is RunStatus.HALTED

    dead: list[RunSubmitter] = []
    resumed_id = "svc:resumed"
    for step, (run_id, now) in enumerate(
        [("svc", T0), (resumed_id, AFTER_EXPIRY), (resumed_id, AFTER_EXPIRY + 31)]
    ):
        runtime = Watcher(block_at=1)
        dying = process(path, runtime, checkpointer, owner=f"d{step}", clock=Clock(now))
        handle = await dying.resume_service(graph, run_id, sleep=no_sleep)
        await runtime.blocked.wait()  # 첫 회차가 끝나기 전에 죽는다
        assert handle.run_id == resumed_id
        assert dying.manager.store.get(resumed_id).iteration >= halted.iteration
        dead.append(dying)

    for dying in dead:
        await bury(dying)
