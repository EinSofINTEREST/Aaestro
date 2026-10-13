"""Deploying a graph as containers — validated, rolled back on failure, re-attached (#243)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from aaestro.access.registry import ACCESS_CREDENTIAL_ENV, AccessRegistry
from aaestro.authoring import Author
from aaestro.catalog import Catalog
from aaestro.core.agent import HealthState, HealthStatus
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import ModelOverride
from aaestro.runtime.control import ControlClient
from aaestro.runtime.deployments import (
    DeploymentManager,
    DeploymentRecord,
    DeploymentStatus,
    InMemoryDeploymentStore,
    SqliteDeploymentStore,
)
from aaestro.runtime.docker.engine import DockerEngine
from aaestro.runtime.images import BuildRecord, BuildStatus
from aaestro.runtime.launcher import AgentLauncher
from aaestro.runtime.lifecycle import AgentState
from aaestro.runtime.ports import A2APortAllocator
from aaestro.runtime.spec import (
    A2A_EDGES_ENV,
    A2A_PEERS_ENV,
    A2A_SECRET_ENV,
    DEFAULT_BASE_IMAGE,
)
from tests.fixtures.fake_docker import FakeDockerClient
from tests.fixtures.waiting import until

REPO_ROOT = Path(__file__).resolve().parents[3]


def write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def agent_doc(name: str) -> dict:
    return {
        "apiVersion": "aaestro/v1",
        "kind": "Agent",
        "metadata": {"name": name, "version": "0.1.0"},
        "spec": {
            "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
            "promptset": {"ref": "promptsets/solo@0.1.0"},
            "runtime": {"env_allowlist": ["ANTHROPIC_API_KEY"]},
        },
    }


def graph_doc(name: str, agents: list[str]) -> dict:
    nodes = [{"id": f"n{i}", "agent": f"agents/{a}@0.1.0"} for i, a in enumerate(agents)]
    edges = (
        [{"from": "START", "to": "n0"}]
        + [{"from": f"n{i}", "to": f"n{i + 1}"} for i in range(len(agents) - 1)]
        + [{"from": f"n{len(agents) - 1}", "to": "END"}]
    )
    return {
        "apiVersion": "aaestro/v1",
        "kind": "Graph",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": {
            "mode": "mission",
            "goal": "t",
            "state": {"schema": "aaestro.graphs.schemas:ResearchState"},
            "nodes": nodes,
            "edges": edges,
        },
    }


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    write(
        tmp_path / "modules" / "promptsets" / "solo" / "0.1.0" / "promptset.yaml",
        {
            "apiVersion": "aaestro/v1",
            "kind": "Promptset",
            "metadata": {"name": "solo", "version": "0.1.0"},
            "spec": {
                "engine": "jinja2",
                "templates": {
                    "default": {"file": "t.j2"},
                    "n0": {"file": "a.j2"},
                    "n1": {"file": "b.j2"},
                },
            },
        },
    )
    for name in ("alpha", "beta"):
        write(tmp_path / "agents" / name / "manifest.yaml", agent_doc(name))
    write(
        tmp_path / "groups" / "global.yaml",
        yaml.safe_load((REPO_ROOT / "groups" / "global.yaml").read_text(encoding="utf-8")),
    )
    write(tmp_path / "graphs" / "two.yaml", graph_doc("two", ["alpha", "beta"]))
    write(tmp_path / "graphs" / "twice.yaml", graph_doc("twice", ["alpha", "alpha"]))
    return tmp_path


def running(docker: FakeDockerClient) -> list[str]:
    """뜬 것 중 멈추거나 지워지지 않은 컨테이너 — 유령 컨테이너 검사의 기준."""
    gone = {cid for cid, _ in docker.stopped} | set(docker.removed)
    return [cid for cid in docker.started if cid not in gone]


class TrackingDocker(FakeDockerClient):
    """지워진 컨테이너는 inspect 에서 Running=False — 재시작 뒤 대조에 필요하다."""

    def inspect(self, container_id: str) -> dict:
        if container_id in self.removed:
            return {"Running": False, "ExitCode": 0, "OOMKilled": False}
        return super().inspect(container_id)


class NoSleep:
    async def __call__(self, _delay: float) -> None:
        await asyncio.sleep(0)


class Tick:
    """health 루프용 — 즉시 통과시키면 루프가 hot-spin 해 to_thread 결과가 밀린다."""

    async def __call__(self, _delay: float) -> None:
        await asyncio.sleep(0.01)


@pytest.fixture
def healthy(monkeypatch):
    """모든 Control 클라이언트가 건강하다 — 감시 루프가 Ready 로 올린다."""

    async def well(self: ControlClient) -> HealthStatus:
        return HealthStatus(status=HealthState.HEALTHY)

    async def drained(self: ControlClient, *, timeout_s: float | None = None) -> None:
        return None

    monkeypatch.setattr(ControlClient, "health", well)
    # Ready 인 에이전트의 정지는 drain 을 먼저 친다 — 대역이 없으면 실제 HTTP 재시도로 느려진다
    monkeypatch.setattr(ControlClient, "drain", drained)


@pytest.fixture
def docker() -> TrackingDocker:
    return TrackingDocker()


@pytest.fixture
def manager(workspace, docker, healthy):
    catalog = Catalog.under(workspace)
    launcher = AgentLauncher(
        engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=Tick()
    )
    return DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog),
        launcher=launcher,
        store=InMemoryDeploymentStore(),
        secrets_env={"ANTHROPIC_API_KEY": "k"},
        ready_timeout_s=5.0,
        ready_poll_s=0.0,
        sleep=NoSleep(),
    )


# --- 배포 ------------------------------------------------------------------------


async def test_deploy_launches_every_agent_and_becomes_ready(manager, docker):
    record = await manager.deploy("two")

    assert record.status == DeploymentStatus.READY
    assert sorted(a.name for a in record.agents) == ["alpha", "beta"]
    assert len(docker.created) == 2
    assert all(a.container_id and a.token for a in record.agents)
    await manager.launcher.stop_all()


async def test_the_same_agent_on_two_nodes_is_launched_once(manager, docker):
    record = await manager.deploy("twice")

    assert [a.name for a in record.agents] == ["alpha"]
    assert len(docker.created) == 1
    await manager.launcher.stop_all()


async def test_a_graph_that_fails_validation_launches_nothing(manager, docker, workspace):
    write(workspace / "graphs" / "bad.yaml", graph_doc("bad", ["nobody"]))

    with pytest.raises(AaestroError) as excinfo:
        await manager.deploy("bad")

    assert excinfo.value.code == ErrorCode.VAL_001
    assert docker.created == []
    assert manager.deployments() == []


async def test_an_unknown_graph_is_not_found(manager):
    with pytest.raises(AaestroError) as excinfo:
        await manager.deploy("nope")

    assert excinfo.value.code == ErrorCode.NF_001


# --- 되감기 -----------------------------------------------------------------------


async def test_a_launch_failure_rolls_back_what_was_started(manager, docker, monkeypatch):
    """유령 컨테이너는 05 Consistency 3 위반 — 하나가 못 뜨면 앞서 뜬 것을 내린다."""
    original = docker.create
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("daemon hiccup")
        return original(**kwargs)

    monkeypatch.setattr(docker, "create", flaky)

    with pytest.raises(AaestroError):
        await manager.deploy("two")

    record = manager.deployments()[0]
    assert record.status == DeploymentStatus.FAILED
    assert record.error
    assert running(docker) == [], "먼저 뜬 컨테이너가 남았다"


async def test_agents_that_never_get_healthy_time_out_and_roll_back(workspace, docker, monkeypatch):
    async def sick(self: ControlClient) -> HealthStatus:
        return HealthStatus(status=HealthState.UNHEALTHY)

    monkeypatch.setattr(ControlClient, "health", sick)
    catalog = Catalog.under(workspace)
    ticks = iter(range(0, 100, 10))
    manager = DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog),
        launcher=AgentLauncher(
            engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=Tick()
        ),
        store=InMemoryDeploymentStore(),
        secrets_env={"ANTHROPIC_API_KEY": "k"},
        ready_timeout_s=30.0,
        ready_poll_s=0.0,
        sleep=NoSleep(),
        clock=lambda: next(ticks),
    )

    with pytest.raises(AaestroError) as excinfo:
        await manager.deploy("two")

    assert excinfo.value.code == ErrorCode.RT_002
    assert running(docker) == []
    assert manager.deployments()[0].status == DeploymentStatus.FAILED


# --- 해체 / in_use ---------------------------------------------------------------


async def test_teardown_stops_every_agent_and_keeps_the_record(manager, docker):
    record = await manager.deploy("two")

    stopped = await manager.teardown(record.deployment_id)

    assert stopped.status == DeploymentStatus.STOPPED
    assert running(docker) == []
    assert manager.get(record.deployment_id).status == DeploymentStatus.STOPPED


async def test_teardown_of_an_unknown_deployment_is_not_found(manager):
    with pytest.raises(AaestroError) as excinfo:
        await manager.teardown("dep-nope")

    assert excinfo.value.code == ErrorCode.NF_001


async def test_in_use_follows_the_deployment(manager):
    assert not manager.in_use("agent", "alpha")
    record = await manager.deploy("two")

    assert manager.in_use("agent", "alpha") and manager.in_use("graph", "two")
    assert not manager.in_use("agent", "gamma")

    await manager.teardown(record.deployment_id)
    assert not manager.in_use("agent", "alpha")


async def test_authoring_refuses_to_touch_a_deployed_agent(manager, workspace):
    """#242 의 in_use 훅이 실제로 이것으로 채워진다."""
    await manager.deploy("two")
    author = Author(catalog=Catalog.under(workspace), in_use=manager.in_use)

    with pytest.raises(AaestroError) as excinfo:
        author.delete_graph("two")

    assert "deployed" in excinfo.value.message
    await manager.launcher.stop_all()


# --- 재시작 --------------------------------------------------------------------------


async def test_a_restarted_control_plane_reattaches_running_containers(workspace, docker, healthy):
    catalog = Catalog.under(workspace)
    store = InMemoryDeploymentStore()
    first = DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog),
        store=store,
        secrets_env={"ANTHROPIC_API_KEY": "k"},
        launcher=AgentLauncher(
            engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=Tick()
        ),
        ready_poll_s=0.0,
        sleep=NoSleep(),
    )
    record = await first.deploy("two")
    assert record.status == DeploymentStatus.READY

    # 재시작 — 같은 저장소, 같은 Docker, 새 launcher
    second = DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog),
        store=store,
        secrets_env={"ANTHROPIC_API_KEY": "k"},
        launcher=AgentLauncher(
            engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=Tick()
        ),
        ready_poll_s=0.0,
        sleep=NoSleep(),
    )
    touched = await second.reattach()

    assert [r.status for r in touched] == [DeploymentStatus.READY]
    assert sorted(second.launcher.launched) == [("alpha", 0), ("beta", 0)]
    adopted = second.launcher.launched[("alpha", 0)]
    # 살아 있다고 Ready 는 아니다 — 첫 health 성공이 올린다 (02 Rule 2)
    await until(lambda: adopted.lifecycle.accepts_tasks)
    assert second.launcher.route("alpha").agent == "alpha"
    assert second.in_use("agent", "alpha")
    await second.launcher.stop_all()


async def test_missing_containers_mark_the_deployment_lost(workspace, docker, healthy):
    catalog = Catalog.under(workspace)
    store = InMemoryDeploymentStore()
    first = DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog),
        store=store,
        secrets_env={"ANTHROPIC_API_KEY": "k"},
        launcher=AgentLauncher(
            engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=Tick()
        ),
        ready_poll_s=0.0,
        sleep=NoSleep(),
    )
    record = await first.deploy("two")
    docker.remove(record.agents[0].container_id)  # 누가 밖에서 지웠다

    second = DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog),
        store=store,
        secrets_env={"ANTHROPIC_API_KEY": "k"},
        launcher=AgentLauncher(
            engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=Tick()
        ),
        ready_poll_s=0.0,
        sleep=NoSleep(),
    )
    touched = await second.reattach()

    assert touched[0].status == DeploymentStatus.LOST
    assert "alpha" in (touched[0].error or "")
    # 붙은 쪽(beta)은 그대로 감시한다 — 그 컨테이너가 아직 선언으로 돌고 있으므로
    # 선언도 계속 보호한다 (리뷰: LOST 를 in_use 에서 빼면 덮어쓸 수 있다)
    assert list(second.launcher.launched) == [("beta", 0)]
    assert second.in_use("agent", "beta") and second.in_use("agent", "alpha")
    assert second.in_use("graph", "two")

    stopped = await second.teardown(record.deployment_id)

    assert stopped.status == DeploymentStatus.STOPPED
    assert second.launcher.launched == {}
    assert not second.in_use("agent", "beta")


# --- 저장소 ----------------------------------------------------------------------


async def test_the_sqlite_store_survives_a_reopen(tmp_path, manager):
    manager.store = SqliteDeploymentStore(path=str(tmp_path / "dep.db"))
    record = await manager.deploy("two")

    reopened = SqliteDeploymentStore(path=str(tmp_path / "dep.db"))

    assert reopened.get(record.deployment_id) == record
    assert [r.deployment_id for r in reopened.list()] == [record.deployment_id]
    await manager.launcher.stop_all()


# --- agent_env 는 secret 을 우회하지 못한다 ------------------------------------------


@pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "DB_PASSWORD", "MY_SECRET", "X_TOKEN"])
def test_agent_env_refuses_secret_looking_keys(workspace, docker, key):
    catalog = Catalog.under(workspace)
    with pytest.raises(AaestroError) as excinfo:
        DeploymentManager(
            catalog=catalog,
            author=Author(catalog=catalog),
            store=InMemoryDeploymentStore(),
            launcher=AgentLauncher(engine=DockerEngine(client=docker)),
            agent_env={key: "x"},
        )
    assert excinfo.value.code == ErrorCode.CFG_002


async def test_agent_env_reaches_the_container_alongside_secrets(manager, docker):
    manager.agent_env = {"ANTHROPIC_BASE_URL": "http://fake:8000"}

    await manager.deploy("two")

    env = docker.created[0]["environment"]
    assert env["ANTHROPIC_BASE_URL"] == "http://fake:8000"
    assert env["ANTHROPIC_API_KEY"] == "k"
    await manager.launcher.stop_all()


# --- 선언 마운트와 A2A 배선 (#243) -------------------------------------------------


def wired_workspace(workspace: Path) -> None:
    """beta → alpha 를 선언한 그래프. 두 에이전트 모두 A2A 를 켠다."""
    for name in ("alpha", "beta"):
        doc = agent_doc(name)
        doc["spec"]["a2a"] = {"enabled": True}
        write(workspace / "agents" / name / "manifest.yaml", doc)
    graph = graph_doc("wired", ["alpha", "beta"])
    graph["spec"]["connections"] = [{"caller": "n1", "callee": "n0"}]
    write(workspace / "graphs" / "wired.yaml", graph)


def wired_manager(workspace: Path, docker: TrackingDocker, store=None) -> DeploymentManager:
    catalog = Catalog.under(workspace)
    return DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog),
        launcher=AgentLauncher(
            engine=DockerEngine(client=docker),
            ports=A2APortAllocator(port_range=(9100, 9110)),
            health_interval_s=10.0,
            health_sleep=Tick(),
        ),
        store=store or InMemoryDeploymentStore(),
        secrets_env={"ANTHROPIC_API_KEY": "k"},
        ready_poll_s=0.0,
        sleep=NoSleep(),
    )


def env_of(docker: TrackingDocker, agent: str) -> dict[str, str]:
    for created in docker.created:
        if created["name"] == f"aaestro-{agent}-0":
            return created["environment"]
    raise AssertionError(f"{agent} was not created")


async def test_the_graph_connections_become_a2a_env_in_the_containers(workspace, docker, healthy):
    """03 Discovery — compose 가 손으로 적던 EDGES/SECRET/PEERS 를 runtime 이 준다."""
    wired_workspace(workspace)
    manager = wired_manager(workspace, docker)

    record = await manager.deploy("wired")

    alpha, beta = env_of(docker, "alpha"), env_of(docker, "beta")
    assert alpha[A2A_EDGES_ENV] == beta[A2A_EDGES_ENV] == "beta>alpha"
    assert alpha[A2A_SECRET_ENV] == beta[A2A_SECRET_ENV] == record.a2a_secret
    assert len(record.a2a_secret) >= 32
    # caller 만 peer 주소를 받는다 — 상대의 컨테이너 이름과 **상대의** A2A 포트
    alpha_port = next(a.a2a_port for a in record.agents if a.name == "alpha")
    assert beta[A2A_PEERS_ENV] == f"alpha=aaestro-alpha-0:{alpha_port}"
    assert A2A_PEERS_ENV not in alpha
    await manager.launcher.stop_all()


async def test_declarations_are_mounted_read_only_into_the_base_image(workspace, docker, healthy):
    """02 Rule 2 — declarative agent 는 이미지를 굽지 않으므로 runtime 이 선언을 싣는다."""
    wired_workspace(workspace)
    manager = wired_manager(workspace, docker)

    await manager.deploy("wired")

    volumes = next(c for c in docker.created if c["name"] == "aaestro-alpha-0")["volumes"]
    declaration = str((workspace / "agents" / "alpha").resolve())
    promptsets = str((workspace / "modules" / "promptsets").resolve())
    assert volumes[declaration] == {"bind": "/app/declaration", "mode": "ro"}
    assert volumes[promptsets] == {"bind": "/app/modules/promptsets", "mode": "ro"}
    # 없는 모듈 루트는 걸지 않는다 — Docker 가 root 소유 디렉토리를 만들어 버린다
    assert not any(v["bind"].endswith("/memorysets") for v in volumes.values())
    assert all(v["mode"] == "ro" for v in volumes.values())
    # 파일 하나를 바인드하지 않는다 — 원자적 교체가 떠 있는 컨테이너에 반영되지 않는다 (#275)
    assert not any(v["bind"].endswith(".yaml") for v in volumes.values())
    created = next(c for c in docker.created if c["name"] == "aaestro-alpha-0")
    assert created["environment"]["AAESTRO_MANIFEST"] == "/app/declaration/manifest.yaml"
    await manager.launcher.stop_all()


def test_runtime_a2a_env_names_match_agentd():
    """runtime 이 agentd 를 import 하지 않으므로 상수를 두 벌 둔다 — 드리프트 방지."""
    from aaestro.agentd import a2a_server

    assert (A2A_EDGES_ENV, A2A_SECRET_ENV, A2A_PEERS_ENV) == (
        a2a_server.EDGES_ENV,
        a2a_server.SECRET_ENV,
        a2a_server.PEERS_ENV,
    )


async def test_a_failed_deploy_returns_the_preallocated_ports(
    workspace, docker, healthy, monkeypatch
):
    wired_workspace(workspace)
    manager = wired_manager(workspace, docker)
    original = DockerEngine.start

    async def second_fails(self, spec):
        if spec.name == "aaestro-beta-0":
            raise AaestroError(category=ErrorCategory.RUNTIME, code=ErrorCode.RT_001, message="x")
        return await original(self, spec)

    monkeypatch.setattr(DockerEngine, "start", second_fails)

    with pytest.raises(AaestroError):
        await manager.deploy("wired")

    assert manager.launcher.ports is not None
    assert manager.launcher.ports.assigned == {}
    assert running(docker) == []


async def test_an_isolated_control_plane_reattaches_by_network_address(workspace, healthy):
    """내부 네트워크에는 게시 포트가 없다 — 재부착도 네트워크 안 주소로 붙어야 한다 (#280)."""
    docker = TrackingDocker(address="172.30.0.9")
    catalog = Catalog.under(workspace)
    store = InMemoryDeploymentStore()

    def manager_on_internal_network() -> DeploymentManager:
        return DeploymentManager(
            catalog=catalog,
            author=Author(catalog=catalog),
            store=store,
            secrets_env={"ANTHROPIC_API_KEY": "k"},
            launcher=AgentLauncher(
                engine=DockerEngine(client=docker, network="agents", internal=True),
                health_interval_s=10.0,
                health_sleep=Tick(),
            ),
            ready_poll_s=0.0,
            sleep=NoSleep(),
        )

    first = manager_on_internal_network()
    await first.deploy("two")
    second = manager_on_internal_network()
    touched = await second.reattach()

    assert [r.status for r in touched] == [DeploymentStatus.READY]
    for launched in second.launcher.launched.values():
        # 핸들만이 아니라 실제로 부르는 클라이언트가 그 주소를 써야 한다
        assert launched.client.base_url == launched.handle.control_url == "http://172.30.0.9:8080"
    await first.launcher.stop_all()
    await second.launcher.stop_all()


@pytest.mark.parametrize(
    ("network_error", "attached"),
    [
        # 프록시를 켜기 전 일반 네트워크로 뜬 배포
        ("not internal", None),
        # 내부 네트워크에 있지만 누가 외부 네트워크를 더 붙인 컨테이너
        (None, ("agents", "bridge")),
    ],
)
async def test_reattach_refuses_containers_that_are_not_isolated(
    workspace, healthy, network_error, attached
):
    """재부착은 기동을 거치지 않는다 — 외부 경로가 남은 컨테이너를 격리된 것으로 붙이면 안 된다."""
    from aaestro.runtime.docker.errors import NetworkIsolationError

    catalog = Catalog.under(workspace)
    store = InMemoryDeploymentStore()
    first_docker = TrackingDocker()
    first = DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog),
        store=store,
        secrets_env={"ANTHROPIC_API_KEY": "k"},
        launcher=AgentLauncher(
            engine=DockerEngine(client=first_docker, network="agents"),
            health_interval_s=10.0,
            health_sleep=Tick(),
        ),
        ready_poll_s=0.0,
        sleep=NoSleep(),
    )
    registry = with_access(first)
    record = await first.deploy("two")
    await first.launcher.stop_all()

    later = TrackingDocker(
        network_error=(
            NetworkIsolationError("agents", expected=True, actual=False) if network_error else None
        ),
        attached=attached,
    )
    later.created = list(first_docker.created)
    second = DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog),
        store=store,
        secrets_env={"ANTHROPIC_API_KEY": "k"},
        launcher=AgentLauncher(
            engine=DockerEngine(client=later, network="agents", internal=True),
            health_interval_s=10.0,
            health_sleep=Tick(),
        ),
        ready_poll_s=0.0,
        sleep=NoSleep(),
    )
    second.access = registry

    touched = await second.reattach()

    assert [(r.status, r.error) for r in touched] == [
        (DeploymentStatus.LOST, "network isolation mismatch: alpha")
    ]
    assert second.launcher.launched == {}, "격리되지 않은 컨테이너를 붙였다"
    for agent in record.agents:
        with pytest.raises(AaestroError):
            registry.identify(agent.access_credential)  # 외부 경로가 남은 컨테이너의 신원은 회수


class VanishingDocker(TrackingDocker):
    """찾은 뒤 조회하는 사이 컨테이너가 사라진다 — SDK 는 NotFound 같은 자기 예외를 던진다."""

    def __init__(self, vanishing: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.vanishing = vanishing

    def networks_of(self, container_id: str) -> tuple[str, ...]:
        if container_id == self.find(self.vanishing):
            raise RuntimeError("404 container not found")
        return super().networks_of(container_id)


async def test_a_docker_failure_during_the_isolation_check_fails_closed_and_moves_on(
    workspace, healthy
):
    """확인하지 못한 배포는 lost + 신원 회수, 뒤의 배포는 계속 재부착한다 (#296 리뷰)."""
    catalog = Catalog.under(workspace)
    store = InMemoryDeploymentStore()
    first_docker = TrackingDocker()

    def manager_with(client, *, internal: bool) -> DeploymentManager:
        return DeploymentManager(
            catalog=catalog,
            author=Author(catalog=catalog),
            store=store,
            secrets_env={"ANTHROPIC_API_KEY": "k"},
            launcher=AgentLauncher(
                engine=DockerEngine(client=client, network="agents", internal=internal),
                health_interval_s=10.0,
                health_sleep=Tick(),
            ),
            ready_poll_s=0.0,
            sleep=NoSleep(),
        )

    first = manager_with(first_docker, internal=True)
    registry = with_access(first)
    broken = await first.deploy("two")
    await first.launcher.stop_all()
    write(workspace / "agents" / "gamma" / "manifest.yaml", agent_doc("gamma"))
    write(workspace / "graphs" / "other.yaml", graph_doc("other", ["gamma"]))
    healthy_record = await manager_with(first_docker, internal=True).deploy("other")

    later = VanishingDocker("aaestro-alpha-0", attached=("agents",))
    later.created = list(first_docker.created)
    second = manager_with(later, internal=True)
    second.access = registry

    touched = {r.deployment_id: r for r in await second.reattach()}

    assert touched[broken.deployment_id].status == DeploymentStatus.LOST
    assert touched[broken.deployment_id].error == "network isolation unverifiable: alpha"
    with pytest.raises(AaestroError):
        registry.identify(broken.agents[0].access_credential)
    assert touched[healthy_record.deployment_id].status == DeploymentStatus.READY
    assert sorted(second.launcher.launched) == [("gamma", 0)]
    await second.launcher.stop_all()


async def test_reattach_hands_the_launcher_what_a_restart_needs(workspace, docker, healthy):
    wired_workspace(workspace)
    store = InMemoryDeploymentStore()
    first = wired_manager(workspace, docker, store)
    record = await first.deploy("wired")

    second = wired_manager(workspace, docker, store)
    await second.reattach()

    adopted = second.launcher.launched[("beta", 0)]
    args = adopted.restart_args
    assert args["manifest"].name == "beta"
    assert args["secrets"][A2A_SECRET_ENV] == record.a2a_secret
    assert args["secrets"][A2A_PEERS_ENV].endswith(f":{record.agents[0].a2a_port}")
    assert args["mounts"][0]["mount_path"] == "/app/declaration"
    assert args["secrets"]["AAESTRO_MANIFEST"] == "/app/declaration/manifest.yaml"
    # 기록된 포트를 그대로 잡는다 — 컨테이너 안의 env 는 이미 그 포트로 굳어 있다
    assert adopted.a2a_port == next(a.a2a_port for a in record.agents if a.name == "beta")
    await second.launcher.stop_all()


async def test_reattach_without_the_secrets_marks_the_deployment_lost(workspace, docker, healthy):
    """조용히 붙이면 첫 재시작에서 넘어진다 — 붙지 않고 이유를 남긴다."""
    wired_workspace(workspace)
    store = InMemoryDeploymentStore()
    await wired_manager(workspace, docker, store).deploy("wired")
    second = wired_manager(workspace, docker, store)
    second.secrets_env = {}

    touched = await second.reattach()

    assert [r.status for r in touched] == [DeploymentStatus.LOST]
    assert "ANTHROPIC_API_KEY" in (touched[0].error or "")
    assert second.launcher.launched == {}


async def test_the_sqlite_store_keeps_the_a2a_secret(tmp_path, workspace, docker, healthy):
    wired_workspace(workspace)
    store = SqliteDeploymentStore(path=tmp_path / "deployments.db")
    record = await wired_manager(workspace, docker, store).deploy("wired")

    reopened = SqliteDeploymentStore(path=tmp_path / "deployments.db")

    assert reopened.get(record.deployment_id).a2a_secret == record.a2a_secret


# --- 재시작이 바꾼 컨테이너를 기록이 따라간다 (#243) ----------------------------------


async def test_get_reflects_a_container_the_launcher_replaced(manager, docker):
    """health 재시작은 id 와 control 포트를 바꾼다 — 기록이 옛 컨테이너를 가리키면
    안 된다."""
    record = await manager.deploy("two")
    before = next(a for a in record.agents if a.name == "alpha")
    launched = manager.launcher.launched[("alpha", 0)]
    launched.lifecycle.transition(AgentState.UNHEALTHY)  # 재시작은 Unhealthy 에서 온다

    await manager.launcher._replace(launched)  # noqa: SLF001 — 재시작 경로를 직접 태운다

    after = next(a for a in manager.get(record.deployment_id).agents if a.name == "alpha")
    assert after.container_id != before.container_id
    assert after.container_id == manager.launcher.launched[("alpha", 0)].handle.container_id
    assert before.container_id in docker.removed
    await manager.launcher.stop_all()


async def test_reattach_finds_the_replaced_container_by_name(workspace, docker, healthy):
    """control plane 이 죽어 있는 동안 기록은 옛 id 를 들고 있다 — 이름으로 찾는다."""
    catalog = Catalog.under(workspace)
    store = InMemoryDeploymentStore()

    def fresh() -> DeploymentManager:
        return DeploymentManager(
            catalog=catalog,
            author=Author(catalog=catalog),
            store=store,
            secrets_env={"ANTHROPIC_API_KEY": "k"},
            launcher=AgentLauncher(
                engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=Tick()
            ),
            ready_poll_s=0.0,
            sleep=NoSleep(),
        )

    first = fresh()
    record = await first.deploy("two")
    replaced = first.launcher.launched[("alpha", 0)]
    replaced.lifecycle.transition(AgentState.UNHEALTHY)
    await first.launcher._replace(replaced)  # noqa: SLF001
    live_id = first.launcher.launched[("alpha", 0)].handle.container_id
    stale = store.get(record.deployment_id)
    assert next(a for a in stale.agents if a.name == "alpha").container_id != live_id

    second = fresh()
    touched = await second.reattach()

    assert [r.status for r in touched] == [DeploymentStatus.READY]
    assert next(a for a in touched[0].agents if a.name == "alpha").container_id == live_id
    assert second.launcher.launched[("alpha", 0)].handle.container_id == live_id
    await first.launcher.stop_all()
    await second.launcher.stop_all()


# --- 리뷰(#250) 가 짚은 경계 -------------------------------------------------------


async def test_declarations_are_protected_while_agents_are_still_starting(
    workspace, docker, healthy, monkeypatch
):
    """`agents` 는 Ready 뒤에야 채워진다 — 기동 중에 in_use 가 False 면 authoring 이
    그 선언을 덮어쓴다."""
    wired_workspace(workspace)
    manager = wired_manager(workspace, docker)
    gate = asyncio.Event()
    original = AgentLauncher.start

    async def slow_start(self, manifest, **kwargs):
        await gate.wait()
        return await original(self, manifest, **kwargs)

    monkeypatch.setattr(AgentLauncher, "start", slow_start)
    deploying = asyncio.create_task(manager.deploy("wired"))
    await asyncio.sleep(0)

    assert manager.in_use("agent", "alpha") and manager.in_use("agent", "beta")
    assert manager.in_use("graph", "wired")
    assert manager.deployments()[0].status == DeploymentStatus.STARTING

    gate.set()
    await deploying
    await manager.launcher.stop_all()


async def test_port_exhaustion_fails_the_deployment_and_frees_what_it_took(
    workspace, docker, healthy
):
    """준비 단계의 실패도 실패 경계 안이다 — 기록은 FAILED, 잡은 포트는 돌려준다."""
    wired_workspace(workspace)
    manager = wired_manager(workspace, docker)
    assert manager.launcher.ports is not None
    manager.launcher.ports.port_range = (9100, 9100)  # 두 에이전트, 포트 하나

    with pytest.raises(AaestroError) as exc_info:
        await manager.deploy("wired")

    assert exc_info.value.code == ErrorCode.RT_001
    assert manager.launcher.ports.assigned == {}
    assert manager.deployments()[0].status == DeploymentStatus.FAILED
    assert docker.created == []


# --- 저장소 스키마 마이그레이션 (#254) ---------------------------------------------


def test_the_sqlite_store_opens_a_database_from_before_the_new_columns(tmp_path):
    """실 스택에서 기동이 죽었다 — 옛 DB 는 `a2a_secret`/`declared` 컬럼이 없다."""
    import sqlite3

    path = tmp_path / "deployments.db"
    with sqlite3.connect(path) as old:
        old.execute(
            "CREATE TABLE deployments (deployment_id TEXT PRIMARY KEY, graph TEXT NOT NULL, "
            "version TEXT NOT NULL, status TEXT NOT NULL, agents TEXT NOT NULL, error TEXT, "
            "updated_at TEXT NOT NULL)"
        )
        old.execute(
            "INSERT INTO deployments VALUES (?,?,?,?,?,?,?)",
            ("dep-old", "two", "1.0.0", "ready", "[]", None, "2026-09-06T00:00:00+00:00"),
        )

    store = SqliteDeploymentStore(path=path)
    record = store.get("dep-old")

    assert record is not None and record.status == DeploymentStatus.READY
    assert record.a2a_secret == "" and record.declared == ()
    assert record.model_overrides == {}
    store.upsert(DeploymentRecord(**{**record.__dict__, "declared": ("alpha",), "a2a_secret": "s"}))
    assert store.get("dep-old").declared == ("alpha",)
    assert [r.deployment_id for r in store.list()] == ["dep-old"]


def test_the_sqlite_store_adds_model_overrides_to_a_database_without_it(tmp_path):
    """#358 이전 DB — `a2a_secret`/`declared` 는 있고 `model_overrides` 만 없다."""
    import sqlite3

    path = tmp_path / "deployments.db"
    with sqlite3.connect(path) as old:
        old.execute(
            "CREATE TABLE deployments (deployment_id TEXT PRIMARY KEY, graph TEXT NOT NULL, "
            "version TEXT NOT NULL, status TEXT NOT NULL, agents TEXT NOT NULL, error TEXT, "
            "updated_at TEXT NOT NULL, a2a_secret TEXT NOT NULL DEFAULT '', "
            "declared TEXT NOT NULL DEFAULT '[]')"
        )
        old.execute(
            "INSERT INTO deployments VALUES (?,?,?,?,?,?,?,?,?)",
            ("dep-old", "two", "1.0.0", "ready", "[]", None, "t", "s", '["alpha"]'),
        )

    store = SqliteDeploymentStore(path=path)
    record = store.get("dep-old")

    assert record is not None and record.model_overrides == {}
    store.upsert(DeploymentRecord(**{**record.__dict__, "model_overrides": {"n0": {"name": "m"}}}))
    assert store.get("dep-old").model_overrides == {"n0": {"name": "m"}}


# --- 배포의 모델 override (#358) ----------------------------------------------------


async def test_deploy_records_the_model_overrides_as_declared(tmp_path, manager):
    manager.store = SqliteDeploymentStore(path=str(tmp_path / "dep.db"))

    record = await manager.deploy(
        "two",
        model_overrides={
            "n0": ModelOverride(name="claude-haiku-4-5"),
            "n1": ModelOverride.model_validate({"effort": None}),
        },
    )

    # 명시하지 않은 키는 싣지 않는다 — 다시 입힐 때 아래 계층을 지우면 안 된다
    expected = {"n0": {"name": "claude-haiku-4-5"}, "n1": {"effort": None}}
    assert record.model_overrides == expected
    reopened = SqliteDeploymentStore(path=str(tmp_path / "dep.db"))
    assert reopened.get(record.deployment_id).model_overrides == expected
    await manager.launcher.stop_all()


async def test_deploy_without_overrides_records_an_empty_map(manager):
    record = await manager.deploy("two")

    assert record.model_overrides == {}
    await manager.launcher.stop_all()


async def test_a_model_override_for_an_unknown_node_launches_nothing(manager, docker):
    with pytest.raises(AaestroError) as excinfo:
        await manager.deploy("two", model_overrides={"ghost": ModelOverride(name="m")})

    assert excinfo.value.code == ErrorCode.VAL_002
    assert docker.created == []
    assert manager.deployments() == []


async def test_validation_sees_the_deployment_overrides(manager, docker):
    """override 를 검증 **전에** 입히지 않으면 manifest 밖 provider 로의 교체가 통과한다."""
    override = ModelOverride(provider="openai-compat", name="gpt-5")

    with pytest.raises(AaestroError) as excinfo:
        await manager.deploy("two", model_overrides={"n0": override})

    assert excinfo.value.code == ErrorCode.VAL_001
    assert any("provider" in f for f in excinfo.value.details["findings"])
    assert docker.created == []


# --- 배포 게이트: 굽지 않은 커스텀 에이전트는 기동하지 않는다 (#266) ----------------------


class FakeImages:
    """빌드 단계의 두 질문만 답한다 — 조립과 굽기는 `test_images.py` 소관."""

    def __init__(self, custom: set[str], records: dict[str, BuildRecord] | None = None) -> None:
        self.custom = custom
        self.records = records or {}

    def needs_build(self, agent: str, version: str) -> bool:
        return agent in self.custom

    def record_of(self, agent: str, version: str) -> BuildRecord | None:
        return self.records.get(agent)


def built(agent: str, status: BuildStatus = BuildStatus.BUILT) -> BuildRecord:
    return BuildRecord(
        agent=agent,
        version="0.1.0",
        status=status,
        image=f"aaestro/agent-{agent}:0.1.0",
        error="COPY failed" if status is BuildStatus.FAILED else None,
    )


def image_of(docker: TrackingDocker, agent: str) -> str:
    for created in docker.created:
        if created["name"] == f"aaestro-{agent}-0":
            return created["image"]
    raise AssertionError(f"{agent} was not created")


@pytest.mark.parametrize(
    ("records", "status"),
    [
        ({}, None),
        ({"alpha": built("alpha", BuildStatus.FAILED)}, BuildStatus.FAILED),
        ({"alpha": built("alpha", BuildStatus.BUILDING)}, BuildStatus.BUILDING),
    ],
    ids=["never-built", "failed", "still-building"],
)
async def test_an_unbuilt_custom_agent_is_refused_before_anything_starts(
    manager, docker, records, status
):
    """배포가 대신 굽지 않는다 (02 Lifecycle 1) — 굽힌 적 없거나 실패했거나 굽는 중이면 거절."""
    manager.images = FakeImages({"alpha"}, records)

    with pytest.raises(AaestroError) as excinfo:
        await manager.deploy("two")

    assert excinfo.value.code == ErrorCode.RT_012
    assert excinfo.value.agent == "alpha"
    assert excinfo.value.details["build_status"] == status
    assert excinfo.value.details["image"] == "aaestro/agent-alpha:0.1.0"
    assert docker.created == [], "거절된 배포가 컨테이너를 띄웠다"
    assert manager.deployments() == [], "거절은 실패한 배포가 아니라 시작되지 않은 배포다"


async def test_a_built_record_under_another_tag_is_refused_before_anything_starts(manager, docker):
    """상태가 `built` 여도 기록의 태그가 띄울 태그와 다르면 기동 전에 거절한다 (#383)."""
    stale = BuildRecord(
        agent="alpha",
        version="0.1.0",
        status=BuildStatus.BUILT,
        image="old-prefix/agent-alpha:0.1.0",
    )
    manager.images = FakeImages({"alpha"}, {"alpha": stale})

    with pytest.raises(AaestroError) as excinfo:
        await manager.deploy("two")

    assert excinfo.value.code == ErrorCode.RT_012
    assert excinfo.value.agent == "alpha"
    assert excinfo.value.details["image"] == "aaestro/agent-alpha:0.1.0"
    assert excinfo.value.details["recorded_image"] == "old-prefix/agent-alpha:0.1.0"
    assert excinfo.value.details["build_status"] == BuildStatus.BUILT
    assert docker.created == [], "거절된 배포가 컨테이너를 띄웠다"
    assert manager.deployments() == [], "거절은 실패한 배포가 아니라 시작되지 않은 배포다"


async def test_a_built_custom_agent_runs_its_baked_image(manager, docker):
    """빌드 후에는 배포되고, 매니페스트가 아니라 **구운 태그**로 돈다."""
    manager.images = FakeImages({"alpha"}, {"alpha": built("alpha")})

    record = await manager.deploy("two")

    assert record.status == DeploymentStatus.READY
    assert image_of(docker, "alpha") == "aaestro/agent-alpha:0.1.0"
    await manager.launcher.stop_all()


async def test_a_declarative_agent_needs_no_build(manager, docker):
    """재료가 없으면 base 이미지 + 선언 마운트 그대로다 (#243) — 빌드를 요구하지 않는다."""
    manager.images = FakeImages({"alpha"}, {"alpha": built("alpha")})

    await manager.deploy("two")

    assert image_of(docker, "beta") == DEFAULT_BASE_IMAGE
    await manager.launcher.stop_all()


async def test_a_manifest_naming_another_image_is_refused(manager, docker, workspace):
    """두 곳이 다른 이미지를 가리키면 무엇이 도는지 알 수 없다."""
    doc = agent_doc("alpha")
    doc["spec"]["runtime"]["image"] = "aaestro/agent-something-else:0.1.0"
    write(workspace / "agents" / "alpha" / "manifest.yaml", doc)
    manager.images = FakeImages({"alpha"}, {"alpha": built("alpha")})

    with pytest.raises(AaestroError) as excinfo:
        await manager.deploy("two")

    assert excinfo.value.code == ErrorCode.VAL_002
    assert excinfo.value.details == {
        "declared": "aaestro/agent-something-else:0.1.0",
        "built": "aaestro/agent-alpha:0.1.0",
    }
    assert docker.created == []


async def test_a_manifest_naming_the_built_tag_is_accepted(manager, docker, workspace):
    """같은 태그를 적어 둔 기존 매니페스트(claude-code)는 그대로 통과한다."""
    doc = agent_doc("alpha")
    doc["spec"]["runtime"]["image"] = "aaestro/agent-alpha:0.1.0"
    write(workspace / "agents" / "alpha" / "manifest.yaml", doc)
    manager.images = FakeImages({"alpha"}, {"alpha": built("alpha")})

    record = await manager.deploy("two")

    assert record.status == DeploymentStatus.READY
    await manager.launcher.stop_all()


async def test_a_restart_after_reattach_keeps_the_baked_image(workspace, docker, healthy):
    """재부착한 컨테이너가 다시 세워질 때 base 이미지로 떨어지면 커스텀 실행기가 사라진다."""
    wired_workspace(workspace)
    store = InMemoryDeploymentStore()
    images = FakeImages({"alpha"}, {"alpha": built("alpha")})
    first = wired_manager(workspace, docker, store)
    first.images = images
    await first.deploy("wired")

    second = wired_manager(workspace, docker, store)
    second.images = images
    await second.reattach()

    assert second.launcher.launched[("alpha", 0)].restart_args["image"] == (
        "aaestro/agent-alpha:0.1.0"
    )
    assert second.launcher.launched[("beta", 0)].restart_args["image"] == DEFAULT_BASE_IMAGE
    await second.launcher.stop_all()


async def test_reattach_restarts_with_the_image_that_was_deployed(workspace, docker, healthy):
    """재부착은 **실제로 배포된** 이미지로 다시 세운다 — 지금 스토어에서 다시 유도하지 않는다.

    재시작한 control plane 이 재료 스토어 없이 떴다면 유도 결과가 None 이 되고, 다음 health
    재시작이 base 이미지로 떨어져 커스텀 실행기가 조용히 사라진다.
    """
    wired_workspace(workspace)
    store = InMemoryDeploymentStore()
    first = wired_manager(workspace, docker, store)
    first.images = FakeImages({"alpha"}, {"alpha": built("alpha")})
    await first.deploy("wired")

    second = wired_manager(workspace, docker, store)
    second.images = None  # 재료·빌드 스토어가 설정되지 않은 채 재시작했다
    await second.reattach()

    assert second.launcher.launched[("alpha", 0)].restart_args["image"] == (
        "aaestro/agent-alpha:0.1.0"
    )
    await second.launcher.stop_all()


# --- 에이전트 신원 (#277) ------------------------------------------------------------


def with_access(manager: DeploymentManager) -> AccessRegistry:
    from aaestro.access.store import InMemoryAccessStore

    registry = AccessRegistry(store=InMemoryAccessStore(), catalog=manager.catalog)
    manager.access = registry
    return registry


async def test_each_deployed_agent_carries_its_own_identity(manager, docker):
    registry = with_access(manager)

    record = await manager.deploy("two")

    credentials = {a.name: a.access_credential for a in record.agents}
    for name, credential in credentials.items():
        created = next(c for c in docker.created if c["name"] == f"aaestro-{name}-0")
        assert created["environment"][ACCESS_CREDENTIAL_ENV] == credential
        assert registry.identify(credential) == name
    assert len(set(credentials.values())) == 2, "에이전트끼리 신원을 공유하면 서로를 사칭한다"
    await manager.launcher.stop_all()


async def test_tearing_down_revokes_the_identities(manager):
    registry = with_access(manager)
    record = await manager.deploy("two")

    await manager.teardown(record.deployment_id)

    for agent in record.agents:
        with pytest.raises(AaestroError) as exc_info:
            registry.identify(agent.access_credential)
        assert exc_info.value.code == ErrorCode.ACC_001


async def test_a_rolled_back_deployment_leaves_no_live_identity(workspace, healthy):
    """되감긴 배포의 신원이 살아 있으면 없는 컨테이너 이름으로 강제 지점을 통과한다."""
    failing = TrackingDocker(start_error=RuntimeError("no room"))
    catalog = Catalog.under(workspace)
    manager = DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog),
        launcher=AgentLauncher(engine=DockerEngine(client=failing)),
        store=InMemoryDeploymentStore(),
        secrets_env={"ANTHROPIC_API_KEY": "k"},
        ready_poll_s=0.0,
        sleep=NoSleep(),
    )
    registry = with_access(manager)

    with pytest.raises(AaestroError):
        await manager.deploy("two")

    identities = list(registry.store._identities.values())  # noqa: SLF001
    assert identities, "신원이 발급되지 않았다 — 되감기 확인이 공허하다"
    assert all(identity.revoked_at is not None for identity in identities)


async def test_reattach_reinjects_the_recorded_identity(workspace, docker, healthy):
    wired_workspace(workspace)
    store = InMemoryDeploymentStore()
    first = wired_manager(workspace, docker, store)
    with_access(first)
    record = await first.deploy("wired")

    second = wired_manager(workspace, docker, store)
    second.access = first.access
    await second.reattach()

    beta = next(a for a in record.agents if a.name == "beta")
    assert second.launcher.launched[("beta", 0)].restart_args["secrets"][ACCESS_CREDENTIAL_ENV] == (
        beta.access_credential
    )
    await second.launcher.stop_all()


async def test_without_a_registry_no_identity_is_injected(manager, docker):
    record = await manager.deploy("two")

    assert all(ACCESS_CREDENTIAL_ENV not in c["environment"] for c in docker.created)
    assert all(a.access_credential == "" for a in record.agents)
    await manager.launcher.stop_all()


# --- 리뷰 반영 (#288) --------------------------------------------------------------


async def test_a_replaced_container_keeps_its_identity_in_the_record(manager):
    """조회가 기록을 새 컨테이너로 맞출 때 신원을 비우면, 다음 재시작은 신원 없이 선다."""
    with_access(manager)
    record = await manager.deploy("two")
    before = next(a for a in record.agents if a.name == "alpha")
    launched = manager.launcher.launched[("alpha", 0)]
    launched.lifecycle.transition(AgentState.UNHEALTHY)
    await manager.launcher._replace(launched)  # noqa: SLF001 — 재시작 경로를 직접 태운다

    after = next(a for a in manager.get(record.deployment_id).agents if a.name == "alpha")

    assert after.container_id != before.container_id, "교체가 일어나지 않아 확인이 공허하다"
    assert after.access_credential == before.access_credential != ""
    assert after.token == before.token
    await manager.launcher.stop_all()


async def test_a_failing_stop_still_revokes_the_identities(manager, monkeypatch):
    registry = with_access(manager)
    record = await manager.deploy("two")

    async def refuse(name: str) -> None:
        raise RuntimeError("docker went away")

    monkeypatch.setattr(manager.launcher, "stop", refuse)
    with pytest.raises(RuntimeError):
        await manager.teardown(record.deployment_id)

    for agent in record.agents:
        with pytest.raises(AaestroError) as exc_info:
            registry.identify(agent.access_credential)
        assert exc_info.value.code == ErrorCode.ACC_001


async def test_reattach_revokes_identities_of_a_deployment_interrupted_while_starting(
    workspace, docker, healthy
):
    """기동 도중 control plane 이 죽으면 기록은 STARTING 에 남고 신원은 어디에도 붙지 않는다."""
    store = InMemoryDeploymentStore()
    first = wired_manager(workspace, docker, store)
    registry = with_access(first)
    credential = registry.issue_identity("alpha", "dep-interrupted")
    store.upsert(
        DeploymentRecord(
            deployment_id="dep-interrupted",
            graph="two",
            version="1.0.0",
            status=DeploymentStatus.STARTING,
        )
    )

    second = wired_manager(workspace, docker, store)
    second.access = registry
    await second.reattach()

    with pytest.raises(AaestroError) as exc_info:
        registry.identify(credential)
    assert exc_info.value.code == ErrorCode.ACC_001


def test_the_manifest_env_names_what_agentd_reads():
    """runtime 이 주입하는 env 이름과 agentd 가 읽는 이름이 어긋나면 base 기본값으로 떨어진다."""
    from aaestro.agentd.__main__ import MANIFEST_ENV as AGENTD_MANIFEST_ENV
    from aaestro.runtime.deployments import MANIFEST_ENV

    assert MANIFEST_ENV == AGENTD_MANIFEST_ENV


# --- 메모리 신원 (#278) --------------------------------------------------------------


async def test_with_a_registry_the_memory_token_is_the_agent_identity(workspace, docker, healthy):
    """정적 메모리 토큰으로 되돌아가면 레지스트리 회수가 닿지 않는 뒷문이 된다."""
    from aaestro.memory.http import MEMORY_TOKEN_ENV

    wired_workspace(workspace)
    store = InMemoryDeploymentStore()
    manager = wired_manager(workspace, docker, store)
    manager.memory_url = "http://memory:8090"
    manager.memory_tokens = {"alpha": "static", "beta": "static"}
    with_access(manager)

    record = await manager.deploy("wired")

    for agent in record.agents:
        assert env_of(docker, agent.name)[MEMORY_TOKEN_ENV] == agent.access_credential != "static"
    again = wired_manager(workspace, docker, store)
    again.memory_url, again.memory_tokens, again.access = (
        manager.memory_url,
        manager.memory_tokens,
        manager.access,
    )
    await again.reattach()
    beta = next(a for a in record.agents if a.name == "beta")
    assert (
        again.launcher.launched[("beta", 0)].restart_args["memory"].token == beta.access_credential
    )
    await manager.launcher.stop_all()
    await again.launcher.stop_all()


async def test_each_identity_records_the_graph_it_was_deployed_in(manager):
    """A2A 선언 판정은 호출자가 지금 배포된 그래프의 connections 를 본다 (#281)."""
    registry = with_access(manager)

    await manager.deploy("two")

    assert registry.store.live_graphs("alpha") == frozenset({"two"})
    await manager.launcher.stop_all()


async def test_registry_mode_gives_agents_the_registry_address_and_no_shared_secret(
    workspace, docker, healthy
):
    """공유 서명 키는 그래프의 모든 에이전트가 쥐어 누구든 다른 에이전트 행세를 한다 (#281)."""
    from aaestro.access.client import ACCESS_URL_ENV

    wired_workspace(workspace)
    manager = wired_manager(workspace, docker)
    with_access(manager)
    manager.access_url = "http://control-plane:8700"

    record = await manager.deploy("wired")

    for agent in ("alpha", "beta"):
        env = env_of(docker, agent)
        assert A2A_SECRET_ENV not in env, "레지스트리 모드에 공유 서명 키가 들어갔다"
        assert env[ACCESS_URL_ENV] == "http://control-plane:8700"
        assert env[A2A_EDGES_ENV] == "beta>alpha", "호출자 쪽 편의 검사는 그대로 쓴다"
    assert record.status == "ready"
    await manager.launcher.stop_all()


# --- 권한 에이전트 (#279) ------------------------------------------------------------------


async def test_a_running_permission_agent_becomes_a_peer_of_every_later_deployment(
    workspace, docker, healthy
):
    """권한 에이전트는 다른 배포에 있다 — 떠 있으면 레지스트리 모드의 모두가 요청할 수 있다."""
    from aaestro.access.client import ACCESS_URL_ENV

    wired_workspace(workspace)
    steward_doc = agent_doc("permission-agent")
    steward_doc["spec"]["a2a"] = {"enabled": True}
    write(workspace / "agents" / "permission-agent" / "manifest.yaml", steward_doc)
    write(workspace / "graphs" / "permissions.yaml", graph_doc("permissions", ["permission-agent"]))

    manager = wired_manager(workspace, docker)
    registry = with_access(manager)
    registry.stewards = frozenset({"permission-agent"})
    manager.access_url = "http://control-plane:8700"

    await manager.deploy("permissions")
    port = manager.launcher.launched[("permission-agent", 0)].a2a_port
    await manager.deploy("wired")

    for agent in ("alpha", "beta"):
        env = env_of(docker, agent)
        assert f"{agent}>permission-agent" in env[A2A_EDGES_ENV].split(",")
        assert f"permission-agent=aaestro-permission-agent-0:{port}" in env[A2A_PEERS_ENV].split(
            ","
        )
        assert ACCESS_URL_ENV in env
    steward_env = env_of(docker, "permission-agent")
    assert "permission-agent>permission-agent" not in steward_env.get(A2A_EDGES_ENV, ""), (
        "자기 자신을 peer 로"
    )
    await manager.launcher.stop_all()


async def test_without_the_registry_address_no_permission_agent_is_wired(
    workspace, docker, healthy
):
    """표 없이 부를 수 없다 — 레지스트리 모드가 아니면 권한 에이전트를 잇지 않는다."""
    wired_workspace(workspace)
    steward_doc = agent_doc("permission-agent")
    steward_doc["spec"]["a2a"] = {"enabled": True}
    write(workspace / "agents" / "permission-agent" / "manifest.yaml", steward_doc)
    write(workspace / "graphs" / "permissions.yaml", graph_doc("permissions", ["permission-agent"]))
    manager = wired_manager(workspace, docker)
    registry = with_access(manager)
    registry.stewards = frozenset({"permission-agent"})

    await manager.deploy("permissions")
    await manager.deploy("wired")

    assert "permission-agent" not in env_of(docker, "alpha").get(A2A_EDGES_ENV, "")
    await manager.launcher.stop_all()


# --- 이그레스 프록시 (#293) ------------------------------------------------------------


async def test_with_an_egress_proxy_agents_get_no_model_key_and_go_out_through_the_proxy(
    manager, docker
):
    """프록시가 키를 주입한다 — 에이전트 env 에 키가 있으면 회수가 재배포 없이는 안 된다."""
    from urllib.parse import urlsplit

    from aaestro.runtime.deployments import EgressEndpoints

    with_access(manager)
    manager.egress = EgressEndpoints(
        connect_url="http://aaestro-egress:8080", providers_url="http://aaestro-egress:8081"
    )
    manager.agent_env = {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}

    record = await manager.deploy("two")

    for agent in record.agents:
        env = env_of(docker, agent.name)
        assert "k" not in env.values(), "모델 API 키가 컨테이너에 들어갔다"
        assert env["ANTHROPIC_API_KEY"] == agent.access_credential, "키 자리에는 신원을 싣는다"
        assert env["ANTHROPIC_BASE_URL"] == "http://aaestro-egress:8081/anthropic"
        proxy = urlsplit(env["HTTPS_PROXY"])
        assert (proxy.hostname, proxy.port, proxy.username) == ("aaestro-egress", 8080, agent.name)
        assert proxy.password == agent.access_credential
        assert env["https_proxy"] == env["HTTPS_PROXY"]
        assert "HTTP_PROXY" not in env, "사설 네트워크의 http 서비스까지 프록시로 보내면 안 된다"
    await manager.launcher.stop_all()


async def test_without_an_egress_proxy_the_model_key_is_injected_as_before(manager, docker):
    record = await manager.deploy("two")

    assert all(env_of(docker, a.name)["ANTHROPIC_API_KEY"] == "k" for a in record.agents)
    await manager.launcher.stop_all()


def test_a_proxy_terminated_key_never_leaves_even_without_an_identity_override(manager):
    """배선이 신원으로 덮어쓰지 않아도 프록시가 종단하는 키는 에이전트 env 로 나가지 않는다."""
    from aaestro.runtime.deployments import EgressEndpoints, Provision

    manager.egress = EgressEndpoints(
        connect_url="http://aaestro-egress:8080", providers_url="http://aaestro-egress:8081"
    )
    manifest = manager.catalog.agent("alpha")

    env = manager._env_for(manifest, Provision(env={}, mounts=(), a2a_port=None, image=None))  # noqa: SLF001

    assert "ANTHROPIC_API_KEY" not in env


def test_the_openai_compat_key_never_reaches_the_container_through_the_allowlist(manager):
    """실 키는 프록시만 쥔다 — manifest 가 allowlist 에 올려도 컨테이너로 가지 않는다 (#361)."""
    from aaestro.core.manifest import AgentManifest
    from aaestro.runtime.deployments import EgressEndpoints, Provision

    doc = agent_doc("alpha")
    doc["spec"]["runtime"] = {"env_allowlist": ["ANTHROPIC_API_KEY", "OPENAI_COMPAT_API_KEY"]}
    manifest = AgentManifest.model_validate(doc)
    manager.secrets_env = {"ANTHROPIC_API_KEY": "k", "OPENAI_COMPAT_API_KEY": "sk-real-compat"}
    provision = Provision(env={}, mounts=(), a2a_port=None, image=None)

    plain = manager._env_for(manifest, provision)  # noqa: SLF001
    manager.egress = EgressEndpoints(
        connect_url="http://aaestro-egress:8080", providers_url="http://aaestro-egress:8081"
    )
    proxied = manager._env_for(manifest, provision)  # noqa: SLF001

    assert plain["OPENAI_COMPAT_API_KEY"] == "sk-real-compat", "프록시가 없으면 직접 붙는다"
    assert "OPENAI_COMPAT_API_KEY" not in proxied
    assert "sk-real-compat" not in proxied.values()


def test_the_proxy_wiring_puts_the_identity_in_the_openai_compat_key_slot():
    """anthropic 과 같은 방식 — 주소는 프록시 종단 창구, 키 자리는 에이전트 신원."""
    from aaestro.runtime.deployments import EgressEndpoints

    env = EgressEndpoints(
        connect_url="http://aaestro-egress:8080", providers_url="http://aaestro-egress:8081/"
    ).env_for("alpha", "cred")

    assert env["OPENAI_COMPAT_BASE_URL"] == "http://aaestro-egress:8081/openai-compat"
    assert env["OPENAI_COMPAT_API_KEY"] == "cred" == env["ANTHROPIC_API_KEY"]


def test_a_remote_mcp_credential_stays_with_the_proxy(manager):
    """원격 MCP 서버 자격은 프록시가 붙인다 — 에이전트 env 에 있으면 회수가 재배포 없이 안 된다."""
    from aaestro.core.manifest import AgentManifest
    from aaestro.runtime.deployments import EgressEndpoints, Provision

    doc = agent_doc("alpha")
    doc["spec"]["runtime"] = {"env_allowlist": ["ANTHROPIC_API_KEY", "CORP_TOKEN", "LOCAL_TOKEN"]}
    doc["spec"]["mcp"] = {
        "servers": [
            {
                "name": "corp",
                "transport": "streamable-http",
                "url": "https://mcp.example.com/mcp",
                "auth": {"type": "bearer", "token_env": "CORP_TOKEN"},
            },
            {
                "name": "local",
                "transport": "stdio",
                "command": ["mcp-local"],
                "env_allowlist": ["LOCAL_TOKEN"],
            },
        ]
    }
    manifest = AgentManifest.model_validate(doc)
    manager.secrets_env = {"ANTHROPIC_API_KEY": "k", "CORP_TOKEN": "corp", "LOCAL_TOKEN": "local"}
    provision = Provision(env={}, mounts=(), a2a_port=None, image=None)

    plain = manager._env_for(manifest, provision)  # noqa: SLF001
    manager.egress = EgressEndpoints(
        connect_url="http://aaestro-egress:8080", providers_url="http://aaestro-egress:8081"
    )
    proxied = manager._env_for(manifest, provision)  # noqa: SLF001

    assert plain["CORP_TOKEN"] == "corp", "프록시가 없으면 에이전트가 직접 붙는다"
    assert "CORP_TOKEN" not in proxied
    assert proxied["LOCAL_TOKEN"] == "local", "stdio 서버의 자격은 컨테이너 안에서 쓴다"


def test_the_proxy_wiring_points_remote_mcp_at_the_termination():
    from aaestro.protocols.mcp.transport import MCP_PROXY_URL_ENV
    from aaestro.runtime.deployments import EgressEndpoints

    env = EgressEndpoints(
        connect_url="http://aaestro-egress:8080", providers_url="http://aaestro-egress:8081/"
    ).env_for("alpha", "cred")

    assert env[MCP_PROXY_URL_ENV] == "http://aaestro-egress:8081/mcp"


def test_an_ipv6_proxy_address_keeps_its_brackets():
    """hostname 은 IPv6 괄호를 벗긴다 — `@::1:8080` 은 프록시 주소가 아니다 (#294 리뷰)."""
    from urllib.parse import urlsplit

    from aaestro.runtime.deployments import EgressEndpoints

    env = EgressEndpoints(
        connect_url="http://[fd00::5]:8080", providers_url="http://[fd00::5]:8081"
    ).env_for("alpha", "cred")

    parsed = urlsplit(env["HTTPS_PROXY"])
    assert (parsed.hostname, parsed.port, parsed.password) == ("fd00::5", 8080, "cred")


# --- MCP 사이드카 (#304) -------------------------------------------------------------

SIDECAR = "aaestro-alpha--mcp-search"
SIDECAR_NET = "aaestro-alpha--mcp"


def with_sidecar(workspace: Path, agent: str = "alpha", image: str = "mcp/search:0.3.0") -> None:
    doc = agent_doc(agent)
    doc["spec"]["mcp"] = {
        "servers": [{"name": "search", "transport": "streamable-http", "sidecar": {"image": image}}]
    }
    write(workspace / "agents" / agent / "manifest.yaml", doc)


def sidecar_manager(workspace: Path, docker: FakeDockerClient, store=None) -> DeploymentManager:
    catalog = Catalog.under(workspace)
    return DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog),
        launcher=AgentLauncher(
            engine=DockerEngine(client=docker), health_interval_s=10.0, health_sleep=Tick()
        ),
        store=store or InMemoryDeploymentStore(),
        secrets_env={"ANTHROPIC_API_KEY": "k"},
        ready_timeout_s=5.0,
        ready_poll_s=0.0,
        sleep=NoSleep(),
    )


def behind_the_proxy(manager: DeploymentManager) -> None:
    from aaestro.runtime.deployments import EgressEndpoints
    from aaestro.runtime.sidecars import ProxyAttachment

    with_access(manager)
    manager.launcher.engine.internal = True
    manager.egress = EgressEndpoints(
        connect_url="http://aaestro-egress:8080", providers_url="http://aaestro-egress:8081"
    )
    manager.sidecar_proxy = ProxyAttachment(container="aaestro-egress-1", alias="aaestro-egress")


def created(docker: FakeDockerClient, name: str) -> dict:
    [found] = [c for c in docker.created if c["name"] == name]
    return found


async def test_a_sidecar_starts_before_its_agent_which_joins_its_network(
    manager, docker, workspace
):
    with_sidecar(workspace)

    record = await manager.deploy("two")

    names = [c["name"] for c in docker.created]
    assert names.index(SIDECAR) < names.index("aaestro-alpha-0")
    alpha = next(a for a in record.agents if a.name == "alpha")
    # 프록시가 없는 배포 — 에이전트만 자기 사이드카 네트워크에 붙는다. beta 는 붙지 않는다
    assert docker.connected == [(SIDECAR_NET, alpha.container_id, ())]

    await manager.teardown(record.deployment_id)

    assert docker.find(SIDECAR) is None, "해체가 사이드카를 남겼다"
    assert docker.removed_networks == [SIDECAR_NET, "aaestro-beta--mcp"]
    assert running(docker) == []


async def test_behind_the_proxy_only_the_proxy_reaches_the_sidecar(manager, docker, workspace):
    with_sidecar(workspace)
    behind_the_proxy(manager)

    record = await manager.deploy("two")

    assert docker.connected == [(SIDECAR_NET, "aaestro-egress-1", ("aaestro-egress",))]
    agent_env, sidecar_env = env_of(docker, "alpha"), created(docker, SIDECAR)["environment"]
    # 바깥 호출은 소유 에이전트의 신원으로 같은 판정을 받는다 — 그 밖의 에이전트 env 는 없다
    assert sidecar_env["HTTPS_PROXY"] == agent_env["HTTPS_PROXY"]
    assert ACCESS_CREDENTIAL_ENV not in sidecar_env and "ANTHROPIC_API_KEY" not in sidecar_env
    alpha = next(a for a in record.agents if a.name == "alpha")
    await manager.launcher.stop_all()
    await manager.teardown(record.deployment_id)
    assert (SIDECAR_NET, "aaestro-egress-1") in docker.disconnected
    assert alpha.container_id not in {c for _, c, _ in docker.connected}


async def test_behind_the_proxy_without_its_container_sidecars_are_refused(
    manager, docker, workspace
):
    """프록시를 붙일 수 없으면 에이전트를 사이드카에 직접 이을 수밖에 없다 — 띄우지 않는다."""
    with_sidecar(workspace)
    behind_the_proxy(manager)
    manager.sidecar_proxy = None

    with pytest.raises(AaestroError) as caught:
        await manager.deploy("two")

    assert caught.value.code == ErrorCode.CFG_001
    assert caught.value.details["agents"] == ["alpha"]
    assert docker.created == [] and manager.deployments() == []


async def test_a_graph_without_sidecars_deploys_behind_the_proxy_without_its_container(
    manager, docker
):
    behind_the_proxy(manager)
    manager.sidecar_proxy = None

    record = await manager.deploy("two")

    assert record.status == DeploymentStatus.READY
    await manager.launcher.stop_all()


async def test_a_failed_deploy_removes_the_sidecars_it_started(workspace, healthy):
    """beta 의 사이드카를 세우지 못하면 alpha 와 그 사이드카까지 되감긴다."""
    with_sidecar(workspace)
    with_sidecar(workspace, "beta", image="mcp/broken:0.1.0")

    class NoBroken(TrackingDocker):
        def ensure_image(self, image: str) -> None:
            if image == "mcp/broken:0.1.0":
                raise RuntimeError("pull denied")
            super().ensure_image(image)

    docker = NoBroken()
    manager = sidecar_manager(workspace, docker)

    with pytest.raises(AaestroError) as caught:
        await manager.deploy("two")

    assert caught.value.code == ErrorCode.RT_004
    assert docker.find(SIDECAR) is None
    assert running(docker) == []
    assert set(docker.removed_networks) >= {SIDECAR_NET, "aaestro-beta--mcp"}
    assert [r.status for r in manager.deployments()] == [DeploymentStatus.FAILED]


async def test_reattach_brings_back_a_sidecar_that_disappeared(workspace, docker, healthy):
    with_sidecar(workspace)
    store = InMemoryDeploymentStore()
    record = await sidecar_manager(workspace, docker, store).deploy("two")
    lost = docker.find(SIDECAR)
    docker.remove(lost)

    second = sidecar_manager(workspace, docker, store)
    touched = await second.reattach()

    assert [r.status for r in touched] == [DeploymentStatus.READY]
    assert docker.find(SIDECAR) not in (None, lost)
    await second.launcher.stop_all()
    await second.teardown(record.deployment_id)


async def test_reattach_keeps_a_running_sidecar(workspace, docker, healthy):
    with_sidecar(workspace)
    store = InMemoryDeploymentStore()
    await sidecar_manager(workspace, docker, store).deploy("two")
    alive = docker.find(SIDECAR)

    second = sidecar_manager(workspace, docker, store)
    await second.reattach()

    assert docker.find(SIDECAR) == alive and alive not in docker.removed
    await second.launcher.stop_all()


async def test_reattach_marks_lost_when_a_sidecar_cannot_be_restored(workspace, docker, healthy):
    with_sidecar(workspace)
    store = InMemoryDeploymentStore()
    await sidecar_manager(workspace, docker, store).deploy("two")
    docker.remove(docker.find(SIDECAR))

    class NoPull(TrackingDocker):
        def ensure_image(self, image: str) -> None:
            raise RuntimeError("registry down")

    broken = NoPull()
    broken.created, broken.removed, broken.started = docker.created, docker.removed, docker.started
    touched = await sidecar_manager(workspace, broken, store).reattach()

    assert [r.status for r in touched] == [DeploymentStatus.LOST]
    assert "mcp sidecars not restored: alpha" in (touched[0].error or "")
