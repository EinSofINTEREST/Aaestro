"""A deploy that overlaps a running one is refused before it touches anything (#473).

겹치는 배포 (이미 떠 있는 에이전트를 포함한 배포) 는 사이드카 · 포트 · 신원 · 기록을 건드리기
전에 거부된다 — 거절된 배포의 되감기가 떠 있는 배포의 자원을 치우면 안 된다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import structlog.testing

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.runtime.control import ControlClient
from aaestro.runtime.deployments import (
    DeploymentManager,
    DeploymentRecord,
    DeploymentStatus,
    InMemoryDeploymentStore,
)
from aaestro.runtime.docker.engine import AGENT_LABEL, DockerEngine
from aaestro.runtime.lifecycle import AgentState
from aaestro.runtime.spec import container_name
from tests.fixtures.waiting import until
from tests.unit.runtime.test_deployments import (  # noqa: F401 — fixture
    SIDECAR,
    SIDECAR_NET,
    agent_doc,
    docker,
    graph_doc,
    healthy,
    running,
    wired_manager,
    with_access,
    workspace,
    write,
)

OVERLAP = "overlap"
"""beta 를 먼저, 떠 있는 alpha 를 나중에 띄우는 그래프 — 되감기가 beta 까지 걸린다."""


def overlap_workspace(root: Path) -> None:
    """alpha 는 A2A 포트와 MCP 사이드카를 갖는다. solo = [alpha], overlap = [beta, alpha]."""
    alpha = agent_doc("alpha")
    alpha["spec"]["a2a"] = {"enabled": True}
    alpha["spec"]["mcp"] = {
        "servers": [
            {
                "name": "search",
                "transport": "streamable-http",
                "sidecar": {"image": "mcp/search:0.3.0"},
            }
        ]
    }
    write(root / "agents" / "alpha" / "manifest.yaml", alpha)
    beta = agent_doc("beta")
    beta["spec"]["a2a"] = {"enabled": True}
    write(root / "agents" / "beta" / "manifest.yaml", beta)
    write(root / "graphs" / "solo.yaml", graph_doc("solo", ["alpha"]))
    write(root / "graphs" / f"{OVERLAP}.yaml", graph_doc(OVERLAP, ["beta", "alpha"]))


@pytest.fixture
def overlap(workspace, docker, healthy):  # noqa: F811
    overlap_workspace(workspace)
    return wired_manager(workspace, docker), docker


@pytest.fixture
def planes(workspace, docker, healthy):  # noqa: F811
    """같은 Docker · 같은 저장소 위의 control plane 을 여럿 만든다 — 재시작을 흉내 낸다."""
    overlap_workspace(workspace)
    return (lambda store: wired_manager(workspace, docker, store)), docker


def ports_of(manager: DeploymentManager) -> dict:
    assert manager.launcher.ports is not None
    return dict(manager.launcher.ports.assigned)


async def test_an_overlapping_deploy_leaves_the_running_deployment_untouched(overlap):
    manager, fake = overlap
    running_one = await manager.deploy("solo")
    sidecar, ports, created = fake.find(SIDECAR), ports_of(manager), len(fake.created)
    assert sidecar is not None, "사이드카가 서지 않았다 — 재현이 공허하다"

    with pytest.raises(AaestroError) as caught:
        await manager.deploy(OVERLAP)

    assert caught.value.code == ErrorCode.RT_010
    assert caught.value.details["agents"] == ["alpha"]
    # 떠 있는 배포의 사이드카 · 네트워크 · 포트 기록이 그대로다
    assert fake.find(SIDECAR) == sidecar and sidecar not in fake.removed
    assert SIDECAR_NET not in fake.removed_networks
    assert ports_of(manager) == ports
    # 거절된 배포는 아무것도 세우지 않았고 기록도 남기지 않는다 — 시작되지 않은 배포다
    assert len(fake.created) == created
    assert [r.deployment_id for r in manager.deployments()] == [running_one.deployment_id]
    assert manager.get(running_one.deployment_id).status == DeploymentStatus.READY
    await manager.launcher.stop_all()


async def test_an_overlapping_deploy_issues_no_identity(overlap):
    manager, fake = overlap
    registry = with_access(manager)
    running_one = await manager.deploy("solo")
    issued = len(registry.store._identities)  # noqa: SLF001

    with pytest.raises(AaestroError) as caught:
        await manager.deploy(OVERLAP)

    assert caught.value.code == ErrorCode.RT_010
    assert len(registry.store._identities) == issued  # noqa: SLF001
    [alpha] = running_one.agents
    assert registry.identify(alpha.access_credential) == "alpha"
    await manager.launcher.stop_all()


async def test_concurrent_overlapping_deploys_let_only_one_through(overlap):
    """둘 다 아직 기동 전이면 launcher 는 겹침을 모른다 — 기동 중인 배포도 에이전트를 쥔다."""
    manager, fake = overlap

    first, second = await asyncio.gather(
        manager.deploy("solo"), manager.deploy(OVERLAP), return_exceptions=True
    )

    assert not isinstance(first, BaseException) and first.status == DeploymentStatus.READY
    assert isinstance(second, AaestroError) and second.code == ErrorCode.RT_010
    assert second.details["deployments"] == [first.deployment_id]
    assert fake.find(SIDECAR) is not None and SIDECAR_NET not in fake.removed_networks
    assert len(ports_of(manager)) == 1, "거절된 배포가 포트를 남겼다"
    assert [r.deployment_id for r in manager.deployments()] == [first.deployment_id]
    await manager.launcher.stop_all()


async def test_the_agents_are_free_again_once_the_running_deployment_is_torn_down(overlap):
    manager, fake = overlap
    running_one = await manager.deploy("solo")
    await manager.teardown(running_one.deployment_id)

    record = await manager.deploy(OVERLAP)

    assert record.status == DeploymentStatus.READY
    await manager.launcher.stop_all()


async def test_a_failed_deploy_frees_its_agents_for_the_next_attempt(overlap, monkeypatch):
    manager, fake = overlap
    original = DockerEngine.start
    failures = iter([True])

    async def alpha_fails_once(self, spec):
        if spec.name == "aaestro-alpha-0" and next(failures, False):
            raise AaestroError(category=ErrorCategory.RUNTIME, code=ErrorCode.RT_001, message="x")
        return await original(self, spec)

    monkeypatch.setattr(DockerEngine, "start", alpha_fails_once)
    with pytest.raises(AaestroError):
        await manager.deploy(OVERLAP)
    assert running(fake) == []

    record = await manager.deploy(OVERLAP)

    assert record.status == DeploymentStatus.READY
    await manager.launcher.stop_all()


# --- 소유권은 배포 기록이 쥔다 — launcher 메모리가 빈 구간 ---------------------------


async def refused(manager: DeploymentManager) -> AaestroError:
    with pytest.raises(AaestroError) as caught:
        await manager.deploy(OVERLAP)
    assert caught.value.code == ErrorCode.RT_010
    return caught.value


async def test_an_overlap_while_a_restart_waits_for_its_container_is_refused(overlap, monkeypatch):
    """재시작은 launcher 항목을 지운 뒤 새 컨테이너를 기다린다 — 그 사이 launcher 는 비어 있다."""
    manager, fake = overlap
    running_one = await manager.deploy("solo")
    sidecar, ports = fake.find(SIDECAR), ports_of(manager)
    launched = manager.launcher.launched[("alpha", 0)]
    parked, gate = asyncio.Event(), asyncio.Event()
    original = DockerEngine.start

    async def slow_restart(self, spec):
        if spec.name == "aaestro-alpha-0" and asyncio.current_task() is restart:
            parked.set()
            await gate.wait()
        return await original(self, spec)

    monkeypatch.setattr(DockerEngine, "start", slow_restart)
    launched.lifecycle.transition(AgentState.UNHEALTHY)
    restart = asyncio.create_task(manager.launcher._replace(launched))  # noqa: SLF001
    await asyncio.wait_for(parked.wait(), 3)
    assert not manager.launcher.replicas_of("alpha"), "재시작 창에 들어가지 못했다 — 공허하다"

    error = await refused(manager)

    gate.set()
    await restart
    assert error.details["deployments"] == [running_one.deployment_id]
    assert fake.find(SIDECAR) == sidecar and SIDECAR_NET not in fake.removed_networks
    assert ports_of(manager) == ports
    await manager.launcher.stop_all()


async def test_an_overlap_after_a_failed_restart_is_refused(overlap, monkeypatch):
    """재시작이 실패해도 배포는 그 에이전트를 쥔다 — 항목은 정지 상태로 남는다 (#491)."""
    manager, fake = overlap
    running_one = await manager.deploy("solo")
    sidecar, ports = fake.find(SIDECAR), ports_of(manager)
    launched = manager.launcher.launched[("alpha", 0)]
    original = DockerEngine.start
    failures = iter([True])

    async def fails_once(self, spec):
        if spec.name == "aaestro-alpha-0" and next(failures, False):
            raise AaestroError(category=ErrorCategory.RUNTIME, code=ErrorCode.RT_001, message="x")
        return await original(self, spec)

    monkeypatch.setattr(DockerEngine, "start", fails_once)
    launched.lifecycle.transition(AgentState.UNHEALTHY)
    with pytest.raises(AaestroError):
        await manager.launcher._replace(launched)  # noqa: SLF001
    assert [r.stopped for r in manager.launcher.replicas_of("alpha")] == [True]

    error = await refused(manager)

    assert error.details["deployments"] == [running_one.deployment_id]
    assert fake.find(SIDECAR) == sidecar and SIDECAR_NET not in fake.removed_networks
    assert ports_of(manager) == ports
    await manager.launcher.stop_all()


async def test_a_lost_deployment_holds_its_agents_until_it_is_torn_down(planes):
    """LOST 배포의 사이드카는 launcher 밖에 남는다 — 해체가 풀기 전까지는 그 배포의 것이다."""
    make, fake = planes
    store = InMemoryDeploymentStore()
    record = await make(store).deploy("solo")
    sidecar = fake.find(SIDECAR)
    fake.remove(record.agents[0].container_id)  # 누가 밖에서 지웠다
    restarted = make(store)
    [lost] = await restarted.reattach()
    assert lost.status == DeploymentStatus.LOST
    assert not restarted.launcher.replicas_of("alpha")

    error = await refused(restarted)

    assert error.details["deployments"] == [record.deployment_id]
    assert fake.find(SIDECAR) == sidecar and SIDECAR_NET not in fake.removed_networks
    assert (await restarted.teardown(record.deployment_id)).status == DeploymentStatus.STOPPED
    assert (await restarted.deploy(OVERLAP)).status == DeploymentStatus.READY
    await restarted.launcher.stop_all()


async def test_a_deployment_interrupted_while_starting_holds_its_agents(planes):
    """기동 도중 죽은 배포가 무엇을 세웠는지 알 수 없다 — 운영자가 해체할 때까지 막는다."""
    make, fake = planes
    store = InMemoryDeploymentStore()
    store.upsert(
        DeploymentRecord(
            deployment_id="dep-interrupted",
            graph="solo",
            version="1.0.0",
            status=DeploymentStatus.STARTING,
            updated_at="2026-10-14T00:00:00+00:00",
            a2a_secret="s",
            declared=("alpha",),
        )
    )
    manager = make(store)

    error = await refused(manager)

    assert error.details["deployments"] == ["dep-interrupted"]
    assert fake.created == []
    assert (await manager.teardown("dep-interrupted")).status == DeploymentStatus.STOPPED
    assert (await manager.deploy(OVERLAP)).status == DeploymentStatus.READY
    await manager.launcher.stop_all()


def interrupted_record() -> DeploymentRecord:
    """기동 도중 죽은 배포 — agents 가 비었고 선언된 이름만 있다."""
    return DeploymentRecord(
        deployment_id="dep-interrupted",
        graph="solo",
        version="1.0.0",
        status=DeploymentStatus.STARTING,
        updated_at="2026-10-14T00:00:00+00:00",
        a2a_secret="s",
        declared=("alpha",),
    )


def track_revocations(manager: DeploymentManager) -> list[str]:
    registry = with_access(manager)
    revoked: list[str] = []
    original = registry.revoke_deployment

    def recording(deployment_id: str) -> int:
        revoked.append(deployment_id)
        return original(deployment_id)

    registry.revoke_deployment = recording  # type: ignore[method-assign]
    return revoked


async def test_teardown_of_an_interrupted_deployment_removes_its_orphan_containers(planes):
    """기동 도중 죽은 배포는 agents 가 비어 있다 — 선언된 이름으로 남은 컨테이너를 치운다."""
    make, fake = planes
    first = make(InMemoryDeploymentStore())
    await first.deploy("solo")
    agent_container = fake.find(container_name("alpha", 0))
    assert agent_container is not None
    orphans = set(fake.labeled({AGENT_LABEL: "alpha"}))
    assert agent_container in orphans
    # 새 control plane — launcher 는 이 컨테이너를 모르고, 기록은 STARTING 에 agents 가 비었다
    store = InMemoryDeploymentStore()
    store.upsert(interrupted_record())
    manager = make(store)
    revoked = track_revocations(manager)

    assert (await manager.teardown("dep-interrupted")).status == DeploymentStatus.STOPPED

    assert agent_container in fake.removed
    assert orphans <= set(fake.removed)
    assert fake.labeled({AGENT_LABEL: "alpha"}) == ()
    assert fake.find(SIDECAR) is None and SIDECAR_NET in fake.removed_networks
    assert revoked == ["dep-interrupted"]
    assert "alpha" not in manager._claimed  # noqa: SLF001
    await first.launcher.stop_all()


async def test_teardown_of_a_lost_deployment_removes_containers_the_launcher_never_adopted(planes):
    """#485 — LOST 배포의 컨테이너는 launcher 밖에 있다 — 라벨로 찾아 사이드카 · 네트워크도 치운다.

    기록은 기동한 쪽의 것을 LOST 로 바꿔 새 control plane 에 준다.
    """
    make, fake = planes
    store = InMemoryDeploymentStore()
    first = make(store)
    record = await first.deploy("solo")
    agent_container = fake.find(container_name("alpha", 0))
    assert agent_container is not None and fake.find(SIDECAR) is not None
    # 새 control plane 이 이 배포를 붙이지 못했다 — 컨테이너는 살아 있고 launcher 는 모른다
    store.upsert(DeploymentRecord(**{**record.__dict__, "status": DeploymentStatus.LOST}))
    restarted = make(store)
    assert not restarted.launcher.replicas_of("alpha")

    stopped = await restarted.teardown(record.deployment_id)

    assert stopped.status == DeploymentStatus.STOPPED
    assert agent_container in fake.removed
    assert fake.labeled({AGENT_LABEL: "alpha"}) == ()
    assert fake.find(SIDECAR) is None and SIDECAR_NET in fake.removed_networks
    await first.launcher.stop_all()


async def test_teardown_survives_a_failing_orphan_lookup(planes, monkeypatch):
    """조회 실패가 신원 회수와 STOPPED 기록을 막으면 해체한 배포가 READY 로 남는다."""
    make, fake = planes
    store = InMemoryDeploymentStore()
    store.upsert(interrupted_record())
    manager = make(store)
    revoked = track_revocations(manager)

    def unreachable(labels):
        raise ConnectionError("daemon unreachable")

    monkeypatch.setattr(fake, "labeled", unreachable)

    with structlog.testing.capture_logs() as logs:
        record = await manager.teardown("dep-interrupted")

    assert record.status == DeploymentStatus.STOPPED
    assert revoked == ["dep-interrupted"]
    assert any(e["event"] == "orphan containers could not be listed" for e in logs)


async def test_an_overlap_with_a_reattached_deployment_is_refused(planes):
    make, fake = planes
    store = InMemoryDeploymentStore()
    record = await make(store).deploy("solo")
    sidecar = fake.find(SIDECAR)
    restarted = make(store)
    [ready] = await restarted.reattach()
    assert ready.status == DeploymentStatus.READY

    error = await refused(restarted)

    assert error.details["deployments"] == [record.deployment_id]
    assert fake.find(SIDECAR) == sidecar and sidecar not in fake.removed
    await restarted.launcher.stop_all()


# --- 해체 · 취소 -------------------------------------------------------------------


async def deploy_pair(manager: DeploymentManager) -> DeploymentRecord:
    """alpha 를 먼저, beta 를 나중에 멈추는 배포 — beta 를 멈추는 동안 alpha 는 이미 없다."""
    pair_doc = graph_doc("pair", ["alpha", "beta"])
    write(Path(manager.catalog.roots.for_type("graphs")) / "pair.yaml", pair_doc)
    return await manager.deploy("pair")


async def spin() -> None:
    """다른 task 가 갈 수 있는 데까지 가게 한다 — 가지 않았음을 보는 쪽에서 쓴다."""
    for _ in range(20):
        await asyncio.sleep(0.01)


async def test_a_deploy_during_a_teardown_is_refused_until_it_ends(overlap, monkeypatch):
    """해체는 에이전트를 하나씩 멈춘다 — 그 사이 선 배포의 사이드카를 해체의 정리가 지운다."""
    manager, fake = overlap
    pair = await deploy_pair(manager)
    entered, gate = asyncio.Event(), asyncio.Event()

    async def slow_drain(self, *, timeout_s=None):
        if self.agent == "beta":
            entered.set()
            await gate.wait()

    monkeypatch.setattr(ControlClient, "drain", slow_drain)
    down = asyncio.create_task(manager.teardown(pair.deployment_id))
    await asyncio.wait_for(entered.wait(), 3)
    assert not manager.launcher.replicas_of("alpha"), "alpha 가 먼저 멈추지 않았다 — 공허하다"

    with pytest.raises(AaestroError) as caught:
        await manager.deploy("solo")

    assert caught.value.code == ErrorCode.RT_010
    assert caught.value.details["deployments"] == [pair.deployment_id]
    gate.set()
    await asyncio.wait_for(down, 3)
    record = await manager.deploy("solo")
    assert record.status == DeploymentStatus.READY and fake.find(SIDECAR) is not None
    await manager.launcher.stop_all()


async def test_a_cancelled_deploy_frees_its_agents(overlap, monkeypatch):
    manager, fake = overlap
    entered, gate = asyncio.Event(), asyncio.Event()
    original = DockerEngine.start

    async def parks_alpha(self, spec):
        if spec.name == "aaestro-alpha-0" and not gate.is_set():
            entered.set()
            await gate.wait()
        return await original(self, spec)

    monkeypatch.setattr(DockerEngine, "start", parks_alpha)
    attempt = asyncio.create_task(manager.deploy(OVERLAP))
    await asyncio.wait_for(entered.wait(), 3)
    attempt.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attempt
    gate.set()
    assert running(fake) == []

    record = await manager.deploy(OVERLAP)

    assert record.status == DeploymentStatus.READY
    await manager.launcher.stop_all()


# --- 같은 배포의 해체는 하나에 합류한다 ----------------------------------------------


async def test_a_repeated_teardown_that_would_outlive_the_first_spares_a_new_deployment(
    overlap, monkeypatch
):
    """두 해체가 따로 돌면 먼저 끝난 쪽이 점유를 놓고, 늦은 쪽이 새 배포의 사이드카를 지운다."""
    manager, fake = overlap
    pair = await deploy_pair(manager)
    in_drain, go_first, in_stop, go_second = (asyncio.Event() for _ in range(4))
    beta_stops: list[object] = []
    original_stop = DockerEngine.stop

    async def drain(self, *, timeout_s=None):
        if self.agent == "beta" and not in_drain.is_set():
            in_drain.set()
            await go_first.wait()

    async def stop(self, handle, *, remove=True):
        if handle.agent == "beta":
            beta_stops.append(handle)
            if not go_first.is_set():  # 첫 해체가 drain 에 멈춘 사이 다른 해체가 beta 를 멈춘다
                in_stop.set()
                await go_second.wait()
        return await original_stop(self, handle, remove=remove)

    monkeypatch.setattr(ControlClient, "drain", drain)
    monkeypatch.setattr(DockerEngine, "stop", stop)
    first = asyncio.create_task(manager.teardown(pair.deployment_id))
    await asyncio.wait_for(in_drain.wait(), 3)
    second = asyncio.create_task(manager.teardown(pair.deployment_id))
    await spin()
    go_first.set()
    assert (await asyncio.wait_for(first, 3)).status == DeploymentStatus.STOPPED

    record = await manager.deploy("solo")
    sidecar, nets = fake.find(SIDECAR), fake.removed_networks.count(SIDECAR_NET)

    go_second.set()
    assert (await asyncio.wait_for(second, 3)).status == DeploymentStatus.STOPPED
    assert record.status == DeploymentStatus.READY
    assert fake.find(SIDECAR) == sidecar and sidecar not in fake.removed
    assert fake.removed_networks.count(SIDECAR_NET) == nets, (
        "새 배포의 사이드카 네트워크가 지워졌다"
    )
    assert len(beta_stops) == 1, "같은 컨테이너를 두 번 멈췄다"
    await manager.launcher.stop_all()


async def test_a_repeated_teardown_that_ends_first_keeps_the_agents_held(overlap, monkeypatch):
    """늦게 끝나는 해체가 아직 beta 를 멈추는 중이다 — 그동안 새 배포는 서지 못한다."""
    manager, fake = overlap
    pair = await deploy_pair(manager)
    in_drain, gate = asyncio.Event(), asyncio.Event()

    async def drain(self, *, timeout_s=None):
        if self.agent == "beta" and not in_drain.is_set():
            in_drain.set()
            await gate.wait()

    monkeypatch.setattr(ControlClient, "drain", drain)
    first = asyncio.create_task(manager.teardown(pair.deployment_id))
    await asyncio.wait_for(in_drain.wait(), 3)
    second = asyncio.create_task(manager.teardown(pair.deployment_id))
    await spin()

    with pytest.raises(AaestroError) as caught:
        await manager.deploy("solo")

    assert caught.value.code == ErrorCode.RT_010
    assert caught.value.details["deployments"] == [pair.deployment_id]
    gate.set()
    done = await asyncio.wait_for(asyncio.gather(first, second), 3)
    assert [r.status for r in done] == [DeploymentStatus.STOPPED] * 2
    assert (await manager.deploy("solo")).status == DeploymentStatus.READY
    assert fake.find(SIDECAR) is not None
    await manager.launcher.stop_all()


async def test_a_cancelled_teardown_caller_does_not_stop_the_teardown(overlap, monkeypatch):
    """반쯤 멈춘 배포를 남기지 않는다 — 부른 쪽이 떠나도 해체는 끝까지 간다."""
    manager, fake = overlap
    pair = await deploy_pair(manager)
    in_drain, gate = asyncio.Event(), asyncio.Event()

    async def drain(self, *, timeout_s=None):
        if self.agent == "beta":
            in_drain.set()
            await gate.wait()

    monkeypatch.setattr(ControlClient, "drain", drain)
    caller = asyncio.create_task(manager.teardown(pair.deployment_id))
    await asyncio.wait_for(in_drain.wait(), 3)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    gate.set()

    await until(lambda: manager.get(pair.deployment_id).status == DeploymentStatus.STOPPED)
    assert running(fake) == []
    assert fake.find(SIDECAR) is None
