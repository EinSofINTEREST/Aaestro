"""mission run 재개와 임대 — 같은 run 은 하나만 구동하고, 죽은 소유자의 run 은 재개된다 (#723).

mission 도 service 와 같은 임대 (#662) 를 쓴다. 재개는 끝난 run 이거나 소유자의 임대가 만료된
run 을 한 번의 조건부 갱신 (``RunStore.reopen``) 으로 가져온다. 시간은 fake clock 이다 (06 —
실제 sleep 금지).
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from aaestro.core.agent import TaskResult
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.orchestrator.checkpoint import build_checkpointer
from aaestro.orchestrator.run import DEFAULT_RUN_LEASE_S, RunLease, RunManager
from aaestro.orchestrator.runs import RunService
from aaestro.orchestrator.runstore import InMemoryRunStore, RunRecord, RunStore, SqliteRunStore
from aaestro.orchestrator.submit import RunSubmitter
from tests.fixtures.topologies import make_mission
from tests.unit.orchestrator.test_runs import FakeCatalog, FakeDeployments, FakeSubmitter, deployed

MISSION = make_mission()
T0 = 1_000_000.0
STORES = [InMemoryRunStore, SqliteRunStore]


class Clock:
    """주입하는 시계 — 테스트가 시간을 옮긴다."""

    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class MeetsBeforeTheTransition(SqliteRunStore):
    """재개가 기록을 읽은 직후 (가져오기 직전) 두 재개를 만나게 한다.

    둘 다 "끝난 run" 을 본 뒤에 쓴다 — 확인과 쓰기가 갈라져 있으면 둘 다 구동한다.
    """

    barrier: threading.Barrier | None = None

    def get(self, run_id: str) -> RunRecord | None:
        found = super().get(run_id)
        barrier, self.barrier = self.barrier, None
        if barrier is not None:
            barrier.wait()
        return found


def _service(store: RunStore, owner: str = "me") -> tuple[RunService, FakeSubmitter]:
    submitter = FakeSubmitter(store=store)  # type: ignore[arg-type]
    submitter.manager = RunManager(store=store, lease=RunLease(owner=owner, clock=Clock()))
    runs = RunService(
        catalog=FakeCatalog({MISSION.metadata.name: MISSION}),  # type: ignore[arg-type]
        deployments=FakeDeployments({"dep-1": deployed(MISSION.metadata.name)}),  # type: ignore[arg-type]
        submitter=submitter,  # type: ignore[arg-type]
        store=store,
    )
    return runs, submitter


async def _resume(runs: RunService) -> AaestroError | None:
    try:
        await runs.resume("m-1")
    except AaestroError as err:
        return err
    await asyncio.gather(*runs.drivers.values())
    return None


def _mission(status: str = "failed", *, owner: str = "", lease_until: float = 0.0) -> RunRecord:
    return RunRecord(
        run_id="m-1",
        graph=MISSION.metadata.name,
        mode="mission",
        status=status,
        owner=owner,
        lease_until=lease_until,
    )


def _assert_one_drove(outcomes: list[AaestroError | None], submitters: list[FakeSubmitter]) -> None:
    refused = [err for err in outcomes if err is not None]
    assert len(refused) == 1
    assert refused[0].code == ErrorCode.GRAPH_006
    assert refused[0].category is ErrorCategory.GRAPH
    assert refused[0].details == {"run_id": "m-1", "graph": MISSION.metadata.name}
    assert sorted(len(s.calls) for s in submitters) == [0, 1]


def test_two_connections_resuming_the_same_mission_only_one_drives(tmp_path):
    path = str(tmp_path / "runs.db")
    SqliteRunStore(path=path).upsert(_mission(owner="dead"))
    barrier = threading.Barrier(2, timeout=10)
    stores = [MeetsBeforeTheTransition(path=path) for _ in range(2)]
    pairs = []
    for i, store in enumerate(stores):
        store.barrier = barrier
        pairs.append(_service(store, owner=f"p{i}"))
    outcomes: list[AaestroError | None] = [None, None]

    def resume(i: int) -> None:
        outcomes[i] = asyncio.run(_resume(pairs[i][0]))

    threads = [threading.Thread(target=resume, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads)
    _assert_one_drove(outcomes, [submitter for _, submitter in pairs])


async def test_two_services_on_one_memory_store_resuming_the_same_mission_only_one_drives():
    # 한 이벤트 루프 안의 저장소 — resume 은 가져오기까지 await 가 없어 두 재개가 차례로 시도한다
    store = InMemoryRunStore()
    store.upsert(_mission(owner="dead"))
    pairs = [_service(store, owner="p0"), _service(store, owner="p1")]

    outcomes = list(await asyncio.gather(*(_resume(runs) for runs, _ in pairs)))

    _assert_one_drove(outcomes, [submitter for _, submitter in pairs])


@pytest.mark.parametrize("status", ["running", "draining"])
@pytest.mark.parametrize("holder", ["other", "me"])
@pytest.mark.parametrize("make_store", STORES)
async def test_resuming_a_mission_under_a_live_lease_is_refused(make_store, holder, status):
    # 이 프로세스가 쥔 임대라도 살아 있으면 같다 — 같은 thread 를 두 번 이어 쓰게 된다
    store = make_store()
    store.upsert(_mission(status, owner=holder, lease_until=T0 + 1))
    runs, submitter = _service(store, owner="me")

    err = await _resume(runs)

    assert err is not None and err.code == ErrorCode.GRAPH_006
    assert submitter.calls == []
    found = store.get("m-1")
    assert (found.status, found.owner) == (status, holder)


@pytest.mark.parametrize(
    ("owner", "lease_until"),
    [("dead", T0), ("", 0.0)],
    ids=["expired-lease", "record-from-before-leases"],
)
@pytest.mark.parametrize("make_store", STORES)
async def test_a_running_mission_whose_owner_died_is_resumed(make_store, owner, lease_until):
    store = make_store()
    store.upsert(_mission("running", owner=owner, lease_until=lease_until))
    before = store.get("m-1").transitions
    runs, submitter = _service(store, owner="me")

    err = await _resume(runs)

    assert err is None
    assert submitter.calls == [("resume", "m-1")]
    found = store.get("m-1")
    assert (found.status, found.owner) == ("running", "me")
    assert found.lease_until == T0 + DEFAULT_RUN_LEASE_S
    assert found.transitions == before  # running → running 은 전이가 아니다


@pytest.mark.parametrize("make_store", STORES)
def test_reopen_of_an_ended_mission_counts_the_transition(make_store):
    store = make_store()
    store.upsert(_mission("failed", owner="a", lease_until=T0 + 100))
    before = store.get("m-1").transitions

    # 끝난 run 은 임대가 남아 있어도 가져온다 — 끝난 run 을 구동하는 소유자는 없다
    assert store.reopen("m-1", "b", T0 + 30, now=T0) is True
    assert store.reopen("m-1", "c", T0 + 30, now=T0) is False

    record = store.get("m-1")
    assert (record.status, record.owner, record.lease_until) == ("running", "b", T0 + 30)
    assert record.transitions == before + 1


@pytest.mark.parametrize("make_store", STORES)
def test_reopen_does_not_touch_a_service_run_or_an_unknown_one(make_store):
    # 소유자 없는 service running 을 만들지 않는다 — service 는 claim 과 새 id 재개가 맡는다
    store = make_store()
    store.upsert(RunRecord(run_id="s-1", graph="g", mode="service", status="halted"))

    assert store.reopen("s-1", "b", T0 + 30, now=T0) is False
    assert store.reopen("nope", "b", T0 + 30, now=T0) is False
    assert (store.get("s-1").status, store.get("s-1").owner) == ("halted", "")


def test_reopen_on_a_broken_connection_raises_stor_003(tmp_path):
    store = SqliteRunStore(path=str(tmp_path / "runs.db"))
    store.close()

    with pytest.raises(AaestroError) as excinfo:
        store.reopen("m-1", "b", T0 + 30, now=T0)

    assert excinfo.value.code == ErrorCode.STOR_003
    assert excinfo.value.details == {"run_id": "m-1"}


class Gate:
    """첫 갱신 주기만 테스트가 열어 준다 — 그 뒤 주기는 오지 않는다 (busy loop 방지)."""

    def __init__(self) -> None:
        self.opened = asyncio.Event()
        self.calls = 0

    async def __call__(self, _delay: float) -> None:
        self.calls += 1
        await (self.opened if self.calls == 1 else asyncio.Event()).wait()


class Blocking:
    """첫 노드에서 테스트가 풀어 줄 때까지 멈춘다 — 구동 중인 mission."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def invoke(self, node: Any, task: Any) -> TaskResult:
        self.started.set()
        await self.release.wait()
        return TaskResult.completed(task, output={})


@pytest.mark.parametrize("make_store", STORES)
async def test_a_driven_mission_renews_its_lease_and_lets_go_when_it_ends(make_store):
    store, clock, gate, runtime = make_store(), Clock(), Gate(), Blocking()
    submitter = RunSubmitter(
        runtime=runtime,
        manager=RunManager(store=store, lease=RunLease(owner="me", clock=clock, sleep=gate)),
        checkpointer=build_checkpointer("memory"),
    )
    driving = asyncio.create_task(submitter.submit(MISSION, {"query": "q"}, run_id="m-1"))
    await runtime.started.wait()
    assert store.get("m-1").lease_until == T0 + DEFAULT_RUN_LEASE_S

    clock.now = T0 + 10
    gate.opened.set()
    for _ in range(50):  # 갱신 태스크가 한 주기 돌 때까지 양보한다
        if store.get("m-1").lease_until != T0 + DEFAULT_RUN_LEASE_S:
            break
        await asyncio.sleep(0)
    assert store.get("m-1").lease_until == T0 + 10 + DEFAULT_RUN_LEASE_S

    runtime.release.set()
    result = await driving

    assert result.ok
    found = store.get("m-1")
    assert (found.status, found.owner, found.lease_until) == ("completed", "me", 0.0)


class TakenOver(InMemoryRunStore):
    """다른 프로세스가 임대를 가져간 저장소 — 이 소유자의 갱신은 거절된다."""

    def renew(self, run_id: str, owner: str, lease_until: float) -> bool:
        return False


async def test_a_mission_that_lost_its_lease_stops_without_touching_the_record():
    store, runtime = TakenOver(), Blocking()
    submitter = RunSubmitter(
        runtime=runtime,
        manager=RunManager(store=store, lease=RunLease(owner="me", clock=Clock(), sleep=Gate())),
        checkpointer=build_checkpointer("memory"),
    )

    # 갱신이 배선되지 않으면 노드가 영원히 멈춘다 — 매달리지 않고 실패하게
    async with asyncio.timeout(10):
        result = await submitter.submit(MISSION, {"query": "q"}, run_id="m-1")

    assert not result.ok
    assert result.error is not None and result.error.code == ErrorCode.GRAPH_006
    # 끝 기록을 쓰지 않는다 — running 으로 남아 새 소유자의 것이다
    assert store.get("m-1").status == "running"
