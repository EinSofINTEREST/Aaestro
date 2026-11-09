"""배포 응답이 레플리카의 Unhealthy · Failed 를 드러낸다 — 저장된 상태는 그대로 (#487).

lifecycle 상태는 대개 감시 루프 대신 테스트가 직접 옮긴다: 감시는 첫 health 뒤에 멈춰 두고
(`Park`), 관측이 그 순간의 lifecycle 을 읽는지만 본다. 실제 감시가 상태를 옮기는 경로와 묶는
배선 테스트는 하나 (`test_supervision_marking_a_replica_unhealthy_degrades_the_view`) 이고,
감시 · 재시작 자체는 `test_supervision` · `test_restart` 소관이다.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from aaestro.authoring import Author
from aaestro.catalog import Catalog
from aaestro.core.agent import HealthState, HealthStatus
from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.orchestrator.control import create_app
from aaestro.orchestrator.runs import RunService
from aaestro.orchestrator.runstore import InMemoryRunStore
from aaestro.runtime.control import ControlClient
from aaestro.runtime.deployments import (
    _OBSERVED,
    DEGRADED,
    DeploymentManager,
    DeploymentRecord,
    DeploymentStatus,
    InMemoryDeploymentStore,
    ReplicaState,
)
from aaestro.runtime.docker.engine import DockerEngine
from aaestro.runtime.launcher import AgentLauncher
from aaestro.runtime.lifecycle import AgentState
from tests.fixtures.waiting import until
from tests.unit.orchestrator.test_runs import FakeSubmitter
from tests.unit.runtime import test_deployments as shared
from tests.unit.runtime.test_deployments import NoSleep, TrackingDocker, running

workspace = shared.workspace  # 같은 선언 작업 공간 (그래프 two · twice)


class Park:
    """첫 health 뒤 감시를 세워 둔다 — 테스트가 옮긴 lifecycle 을 감시가 되돌리지 않게."""

    async def __call__(self, _delay: float) -> None:
        await asyncio.Event().wait()


class Pulse:
    """`go` 가 열려 있는 동안만 감시 루프를 돌린다 — 실제 sleep 없이 health 를 몇 번이든 친다."""

    def __init__(self) -> None:
        self.go = asyncio.Event()

    async def __call__(self, _delay: float) -> None:
        await self.go.wait()
        # 열린 Event 의 wait 는 양보하지 않는다 — 감시 루프가 이벤트 루프를 붙잡지 않게
        await asyncio.sleep(0)


class CountingStore(InMemoryDeploymentStore):
    def __init__(self) -> None:
        super().__init__()
        self.written: list[str] = []

    def upsert(self, record: DeploymentRecord) -> None:
        self.written.append(str(record.status))
        super().upsert(record)


@pytest.fixture
def gates(monkeypatch) -> dict[str, asyncio.Event]:
    """health 와 drain 을 테스트가 연다 — 기본은 둘 다 열림. ``sick`` 을 켜면 health 가 실패한다."""
    health, drain, sick = asyncio.Event(), asyncio.Event(), asyncio.Event()
    health.set()
    drain.set()

    async def well(self: ControlClient) -> HealthStatus:
        await health.wait()
        status = HealthState.UNHEALTHY if sick.is_set() else HealthState.HEALTHY
        return HealthStatus(status=status)

    async def drained(self: ControlClient, *, timeout_s: float | None = None) -> None:
        await drain.wait()

    monkeypatch.setattr(ControlClient, "health", well)
    monkeypatch.setattr(ControlClient, "drain", drained)
    return {"health": health, "drain": drain, "sick": sick}


def manager_for(
    workspace,
    docker,
    store,
    *,
    health_interval_s: float | None = 10.0,
    health_sleep: Park | Pulse | None = None,
) -> DeploymentManager:
    catalog = Catalog.under(workspace)
    return DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog),
        launcher=AgentLauncher(
            engine=DockerEngine(client=docker),
            health_interval_s=health_interval_s,
            health_sleep=health_sleep or Park(),
            # 감시가 건 재시작은 backoff 에서 세워 둔다 — 교체가 상태를 바꾸지 않게
            restart_sleep=Park(),
        ),
        store=store,
        secrets_env={"ANTHROPIC_API_KEY": "k"},
        ready_timeout_s=5.0,
        ready_poll_s=0.0,
        sleep=NoSleep(),
    )


@pytest.fixture
def docker() -> TrackingDocker:
    return TrackingDocker()


@pytest.fixture
def store() -> CountingStore:
    return CountingStore()


@pytest.fixture
async def manager(workspace, docker, store, gates):
    manager = manager_for(workspace, docker, store)
    yield manager
    await manager.launcher.stop_all()


@pytest.fixture
async def api(manager):
    app = create_app(InMemoryRunStore(), deployments=manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        yield client


def lifecycle(manager: DeploymentManager, agent: str):
    return manager.launcher.launched[agent, 0].lifecycle


def states(body: dict) -> dict[str, str | None]:
    return {a["name"]: a["state"] for a in body["agents"]}


async def view(api, deployment_id: str) -> dict:
    response = await api.get(f"/v1/deployments/{deployment_id}")
    assert response.status_code == 200
    return response.json()


# --- 1 · 3 Unhealthy 와 회복 ------------------------------------------------------------


async def test_an_unhealthy_replica_degrades_the_view_but_not_the_record(api, manager, store):
    record = await manager.deploy("two")
    lifecycle(manager, "alpha").transition(AgentState.UNHEALTHY)

    body = await view(api, record.deployment_id)

    assert body["status"] == DEGRADED
    assert states(body) == {"alpha": ReplicaState.UNHEALTHY, "beta": ReplicaState.READY}
    assert store.get(record.deployment_id).status == DeploymentStatus.READY
    listed = (await api.get("/v1/deployments")).json()["items"]
    assert [d["status"] for d in listed] == [DEGRADED]


async def test_recovery_turns_the_view_back_to_ready_without_writing(api, manager, store):
    record = await manager.deploy("two")
    lifecycle(manager, "alpha").transition(AgentState.UNHEALTHY)
    await view(api, record.deployment_id)
    writes = len(store.written)

    lifecycle(manager, "alpha").transition(AgentState.READY)
    body = await view(api, record.deployment_id)

    assert body["status"] == DeploymentStatus.READY
    assert set(states(body).values()) == {ReplicaState.READY}
    assert len(store.written) == writes
    assert DEGRADED not in store.written


# --- 2 교체의 틈 -------------------------------------------------------------------------


async def test_a_replica_being_replaced_reads_absent_then_starting(api, manager):
    record = await manager.deploy("two")
    launched = manager.launcher.launched.pop(("alpha", 0))  # `_replace` 가 자리를 비운 틈
    try:
        body = await view(api, record.deployment_id)
        assert body["status"] == DEGRADED
        assert states(body)["alpha"] == ReplicaState.ABSENT
    finally:
        manager.launcher.launched["alpha", 0] = launched

    launched.lifecycle.transition(AgentState.UNHEALTHY)
    launched.lifecycle.transition(AgentState.STARTING)  # 교체 컨테이너가 뜨는 중
    body = await view(api, record.deployment_id)

    assert body["status"] == DEGRADED
    assert states(body)["alpha"] == ReplicaState.STARTING


# --- 4 재시작 상한 -----------------------------------------------------------------------


async def test_a_failed_replica_degrades_still_holds_its_agent_and_is_torn_down(
    api, manager, docker
):
    record = await manager.deploy("two")
    lifecycle(manager, "alpha").transition(AgentState.UNHEALTHY)
    lifecycle(manager, "alpha").transition(AgentState.FAILED)  # plan_restart 가 포기한 자리

    body = await view(api, record.deployment_id)
    assert body["status"] == DEGRADED
    assert states(body)["alpha"] == ReplicaState.FAILED

    with pytest.raises(AaestroError) as exc_info:
        await manager.deploy("twice")
    assert exc_info.value.code == ErrorCode.RT_010

    alpha = next(a.container_id for a in record.agents if a.name == "alpha")
    response = await api.delete(f"/v1/deployments/{record.deployment_id}")
    assert response.json()["status"] == DeploymentStatus.STOPPED
    # 저장된 상태가 ready 라 FAILED 단락을 타지 않는다 — 컨테이너가 실제로 멈춘다
    assert alpha in {cid for cid, _ in docker.stopped}
    assert running(docker) == []


# --- 5 재부착 ----------------------------------------------------------------------------


async def test_a_reattached_replica_reads_starting_until_its_first_health(workspace, docker, gates):
    store = CountingStore()
    first = manager_for(workspace, docker, store)
    record = await first.deploy("two")
    gates["health"].clear()  # 재부착한 감시의 첫 health 가 아직 돌아오지 않았다
    second = manager_for(workspace, docker, store)
    try:
        await second.reattach()

        observed = second.observe(second.get(record.deployment_id))
        assert observed.status == DEGRADED
        assert set(observed.replicas) == {ReplicaState.STARTING}

        gates["health"].set()
        await until(lambda: lifecycle(second, "alpha").accepts_tasks)
        await until(lambda: lifecycle(second, "beta").accepts_tasks)
        observed = second.observe(second.get(record.deployment_id))
        assert observed.status == DeploymentStatus.READY
        assert set(observed.replicas) == {ReplicaState.READY}
    finally:
        gates["health"].set()
        await second.launcher.stop_all()
        await first.launcher.stop_all()


async def test_a_lost_deployment_stays_lost_with_computed_replica_states(workspace, docker, gates):
    store = CountingStore()
    first = manager_for(workspace, docker, store)
    record = await first.deploy("two")
    alpha = next(a for a in record.agents if a.name == "alpha")
    docker.remove(alpha.container_id)
    gates["health"].clear()
    second = manager_for(workspace, docker, store)
    try:
        await second.reattach()

        observed = second.observe(second.get(record.deployment_id))
        assert observed.status == DeploymentStatus.LOST
        by_name = dict(zip((a.name for a in record.agents), observed.replicas, strict=True))
        # 붙은 beta 는 계산되고, 붙지 못한 alpha 는 쥔 레플리카가 없다 — 모른다가 아니다
        assert by_name == {"alpha": ReplicaState.ABSENT, "beta": ReplicaState.STARTING}
    finally:
        gates["health"].set()
        await second.launcher.stop_all()
        await first.launcher.stop_all()


# --- 6 감시가 꺼진 조립 -----------------------------------------------------------------


async def test_without_supervision_the_replica_state_is_unknown_and_the_status_ready(
    workspace, docker, gates
):
    store = CountingStore()
    first = manager_for(workspace, docker, store)
    record = await first.deploy("two")
    unwatched = manager_for(workspace, docker, store, health_interval_s=None)
    try:
        await unwatched.reattach()  # 감시가 없으니 lifecycle 은 STARTING 에서 움직이지 않는다

        observed = unwatched.observe(unwatched.get(record.deployment_id))
        assert observed.status == DeploymentStatus.READY
        assert observed.replicas == (None, None)
    finally:
        await unwatched.launcher.stop_all()
        await first.launcher.stop_all()


# --- 7 해체 중 ---------------------------------------------------------------------------


async def test_draining_during_teardown_is_not_shown_as_degraded(api, manager, gates):
    record = await manager.deploy("two")
    gates["drain"].clear()
    teardown = asyncio.create_task(manager.teardown(record.deployment_id))
    try:
        await until(lambda: lifecycle(manager, "alpha").state is AgentState.DRAINING)

        body = await view(api, record.deployment_id)

        assert body["status"] == DeploymentStatus.READY
        assert set(states(body).values()) == {None}
    finally:
        gates["drain"].set()
        stopped = await teardown
    assert stopped.status == DeploymentStatus.STOPPED


async def test_a_half_finished_teardown_shows_the_stopped_replica_absent(api, manager, monkeypatch):
    """두 번째 정지가 실패하면 기록은 ready 로 남고 해체 표는 비워진다 — 아무도 교체하지 않는다."""
    record = await manager.deploy("two")
    stop = manager.launcher.stop
    broken = {"beta": True}

    async def flaky(agent: str, *, replica: int | None = None) -> None:
        if broken.get(agent):
            raise RuntimeError("stop failed")
        await stop(agent, replica=replica)

    monkeypatch.setattr(manager.launcher, "stop", flaky)
    try:
        with pytest.raises(RuntimeError):
            await manager.teardown(record.deployment_id)
        await until(lambda: record.deployment_id not in manager._teardowns)

        body = await view(api, record.deployment_id)

        assert body["status"] == DEGRADED
        assert states(body)["alpha"] == ReplicaState.ABSENT
    finally:
        broken["beta"] = False


# --- 해체 밖의 정지 -----------------------------------------------------------------------


async def test_draining_outside_a_teardown_is_named_and_degrades(api, manager):
    record = await manager.deploy("two")
    lifecycle(manager, "alpha").transition(AgentState.DRAINING)  # launcher.stop 을 직접 부른 자리

    body = await view(api, record.deployment_id)

    assert body["status"] == DEGRADED
    assert states(body)["alpha"] == ReplicaState.DRAINING


async def test_a_stopped_replica_still_in_the_launcher_reads_absent(api, manager):
    record = await manager.deploy("two")
    lifecycle(manager, "alpha").transition(AgentState.STOPPED)

    body = await view(api, record.deployment_id)

    assert body["status"] == DEGRADED
    assert states(body)["alpha"] == ReplicaState.ABSENT


def test_every_state_a_launched_replica_can_be_in_has_a_name():
    """launcher 는 STARTING 으로 올린 뒤에 `launched` 에 넣는다 — 그 앞의 상태만 표에 없다."""
    assert set(_OBSERVED) == set(AgentState) - {AgentState.DECLARED, AgentState.BUILT}


# --- 감시 배선 -----------------------------------------------------------------------------


async def test_supervision_marking_a_replica_unhealthy_degrades_the_view(workspace, docker, gates):
    pulse = Pulse()
    manager = manager_for(workspace, docker, CountingStore(), health_sleep=pulse)
    app = create_app(InMemoryRunStore(), deployments=manager)
    try:
        record = await manager.deploy("two")
        gates["sick"].set()  # 이제부터 health 가 실패한다 — lifecycle 은 감시만 옮긴다
        pulse.go.set()
        await until(lambda: lifecycle(manager, "alpha").state is AgentState.UNHEALTHY)
        await until(lambda: lifecycle(manager, "beta").state is AgentState.UNHEALTHY)
        pulse.go.clear()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://cp"
        ) as api:
            body = await view(api, record.deployment_id)

        assert body["status"] == DEGRADED
        assert set(states(body).values()) == {ReplicaState.UNHEALTHY}
    finally:
        gates["sick"].clear()
        await manager.launcher.stop_all()


# --- 8 응답 형태 -------------------------------------------------------------------------


async def test_every_deployment_response_carries_replica_state_and_no_token(api, manager):
    created = (await api.post("/v1/deployments", json={"graph": "two"})).json()
    deployment_id = created["deployment_id"]
    tokens = [a.token for a in manager.get(deployment_id).agents]

    bodies = [
        created,
        await view(api, deployment_id),
        (await api.get("/v1/deployments")).json()["items"][0],
    ]
    for body in bodies:
        assert set(states(body).values()) == {ReplicaState.READY}
        assert not any(token in str(body) for token in tokens)
    deleted = (await api.delete(f"/v1/deployments/{deployment_id}")).json()
    assert set(states(deleted).values()) == {None}  # 멈춘 배포의 레플리카는 모른다


# --- 9 런 게이트 -------------------------------------------------------------------------


async def test_a_degraded_deployment_accepts_runs(manager):
    record = await manager.deploy("two")
    lifecycle(manager, "alpha").transition(AgentState.UNHEALTHY)
    assert manager.observe(manager.get(record.deployment_id)).status == DEGRADED
    run_store = InMemoryRunStore()
    submitter = FakeSubmitter(store=run_store)
    runs = RunService(
        catalog=manager.catalog, deployments=manager, submitter=submitter, store=run_store
    )

    submitted = await runs.submit(record.deployment_id, {"query": "q"})

    assert submitted.graph == "two"
    submitter.gate.set()
    await asyncio.gather(*runs.drivers.values())
