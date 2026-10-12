"""Launching and routing across an agent's replicas.

01 Scalability 는 동일 manifest 의 N replica 기동과 그 사이의 round-robin
라우팅을 규정하는데, **둘 다 불가능했다** (#212):

- `AgentLauncher` 가 이름만으로 핸들을 잡아 두 번째 레플리카를 거부했다
- 라우터 클래스를 프로덕션에서 만드는 곳이 없었다 — 라우팅은 `AgentLauncher.route` 가 맡고,
  쓰이지 않던 클래스는 #319 에서 지웠다

health-aware 라우팅은 `AgentLifecycle` 이 돌아야 성립한다 (#213) — 여기서는
기동된 레플리카 위에서 분산한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.core.manifest import AgentManifest
from aaestro.runtime.docker.engine import DockerEngine
from aaestro.runtime.launcher import AgentLauncher
from aaestro.runtime.ports import A2APortAllocator
from aaestro.runtime.spec import A2A_PORT_ENV
from tests.fixtures.fake_docker import FakeDockerClient

REPO_ROOT = Path(__file__).resolve().parents[3]


def manifest(*, a2a: bool = False) -> AgentManifest:
    raw = yaml.safe_load((REPO_ROOT / "agents" / "echo" / "manifest.yaml").read_text("utf-8"))
    raw["spec"]["a2a"]["enabled"] = a2a
    return AgentManifest.model_validate(raw)


def launcher(client: FakeDockerClient, *, ports: bool = False) -> AgentLauncher:
    return AgentLauncher(
        engine=DockerEngine(client=client),
        ports=A2APortAllocator(port_range=(9100, 9199)) if ports else None,
    )


async def launch_replicas(agents: AgentLauncher, count: int) -> None:
    for replica in range(count):
        await agents.start(manifest(), replica=replica)


# --- 기동 -------------------------------------------------------------------


async def test_several_replicas_of_one_agent_launch():
    """#212 — 이름 하나로 잡아 두 번째가 거부됐다."""
    client = FakeDockerClient()
    agents = launcher(client)

    await launch_replicas(agents, 3)

    assert len(agents.replicas_of("echo")) == 3
    assert len(client.created) == 3


async def test_replicas_get_distinct_container_names():
    """이름이 겹치면 두 번째 기동이 Docker 쪽에서 막힌다."""
    client = FakeDockerClient()
    agents = launcher(client)

    await launch_replicas(agents, 2)

    names = [created["name"] for created in client.created]
    assert len(set(names)) == 2


async def test_the_same_replica_twice_is_still_rejected():
    """덮어쓰면 첫 컨테이너가 미아가 된다 — 그 방어는 유지된다."""
    client = FakeDockerClient()
    agents = launcher(client)
    await agents.start(manifest())

    with pytest.raises(AaestroError) as excinfo:
        await agents.start(manifest(), replica=0)

    assert excinfo.value.code == ErrorCode.RT_010
    assert len(client.created) == 1


async def test_replicas_get_distinct_a2a_ports():
    """같은 포트를 두 컨테이너가 열면 한쪽이 조용히 기동에 실패한다."""
    client = FakeDockerClient()
    agents = launcher(client, ports=True)

    for replica in range(2):
        await agents.start(manifest(a2a=True), replica=replica)

    ports = [created["environment"][A2A_PORT_ENV] for created in client.created]
    assert len(set(ports)) == 2


# --- 라우팅 -----------------------------------------------------------------


async def test_routing_spreads_across_replicas():
    """01 — runtime 이 replica 간 round-robin 으로 라우팅한다."""
    agents = launcher(FakeDockerClient())
    await launch_replicas(agents, 3)

    picked = [agents.route("echo") for _ in range(6)]

    # 세 레플리카가 각각 두 번씩 — 한쪽으로 쏠리면 분산이 아니다
    assert len({id(client) for client in picked}) == 3
    assert picked[0] is picked[3]


async def test_routing_a_single_replica_always_returns_it():
    agents = launcher(FakeDockerClient())
    await agents.start(manifest())

    assert agents.route("echo") is agents.route("echo")


async def test_routing_an_unlaunched_agent_fails_loudly():
    """부를 대상이 없으면 그것이 드러나야 한다 — 조용히 None 을 주면 안 된다."""
    agents = launcher(FakeDockerClient())

    with pytest.raises(AaestroError) as excinfo:
        agents.route("absent")

    assert excinfo.value.code == ErrorCode.RT_009
    assert excinfo.value.retryable


# --- 정지 -------------------------------------------------------------------


async def test_stopping_one_replica_leaves_the_others():
    """레플리카 하나의 교체가 나머지를 죽이면 무중단이 아니다."""
    agents = launcher(FakeDockerClient())
    await launch_replicas(agents, 3)

    await agents.stop("echo", replica=1)

    assert [item.replica for item in agents.replicas_of("echo")] == [0, 2]


async def test_stopping_one_replica_keeps_the_token():
    """토큰은 에이전트 단위다 — 하나 멈췄다고 버리면 나머지가 401 이 된다."""
    agents = launcher(FakeDockerClient())
    await launch_replicas(agents, 2)

    await agents.stop("echo", replica=0)

    assert agents.issuer.known("echo")


async def test_stopping_the_agent_stops_every_replica():
    """02 의 drain 은 에이전트 단위다 — 기본은 전부다."""
    agents = launcher(FakeDockerClient())
    await launch_replicas(agents, 3)

    await agents.stop("echo")

    assert not agents.replicas_of("echo")
    assert not agents.issuer.known("echo")


async def test_a_stopped_replica_returns_its_port():
    """회수하지 않으면 재기동을 반복하는 운영에서 범위가 조용히 마른다."""
    client = FakeDockerClient()
    allocator = A2APortAllocator(port_range=(9100, 9199))
    agents = AgentLauncher(engine=DockerEngine(client=client), ports=allocator)
    for replica in range(2):
        await agents.start(manifest(a2a=True), replica=replica)

    await agents.stop("echo", replica=1)

    assert set(allocator.assigned) == {"echo/0"}


async def test_routing_skips_a_stopped_replica():
    """정지한 레플리카로 계속 보내면 그 태스크가 전부 실패한다."""
    agents = launcher(FakeDockerClient())
    await launch_replicas(agents, 2)
    survivor = agents.replicas_of("echo")[0].client
    await agents.stop("echo", replica=1)

    assert [agents.route("echo") for _ in range(3)] == [survivor] * 3
