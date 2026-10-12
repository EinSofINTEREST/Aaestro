"""Unit tests for A2A port allocation.

핵심 계약: **포트는 runtime 이 준다** (03 Rule 2). 파라미터만 있고 채우는
곳이 없으면 A2A 포트는 영원히 열리지 않는다 — 그것이 #173 이었다.
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
from aaestro.runtime.spec import DEFAULT_CONTROL_PORT
from tests.fixtures.fake_docker import FakeDockerClient

REPO_ROOT = Path(__file__).resolve().parents[3]


def manifest(name: str) -> AgentManifest:
    return AgentManifest.model_validate(
        yaml.safe_load((REPO_ROOT / "agents" / name / "manifest.yaml").read_text("utf-8"))
    )


def launcher(client: FakeDockerClient, allocator: A2APortAllocator) -> AgentLauncher:
    return AgentLauncher(engine=DockerEngine(client=client), ports=allocator)


def a2a_ports(created: dict) -> list[int]:
    """컨테이너가 실제로 연 A2A 포트 — 스펙이 아니라 기동 인자를 본다."""
    opened = [int(str(port).split("/")[0]) for port in created["ports"]]
    return [port for port in opened if port != DEFAULT_CONTROL_PORT]


# --- 할당기 단독 -----------------------------------------------------------


def test_allocate_hands_out_distinct_ports_within_range():
    allocator = A2APortAllocator(port_range=(9100, 9199))

    first = allocator.allocate("planner")
    second = allocator.allocate("researcher")

    assert first != second
    assert {first, second} <= set(range(9100, 9200))


def test_allocate_is_stable_for_the_same_replica():
    """재기동이 범위를 갉아먹으면 오래 도는 배포에서 포트가 마른다."""
    allocator = A2APortAllocator(port_range=(9100, 9199))

    assert allocator.allocate("planner") == allocator.allocate("planner")
    assert len(allocator.assigned) == 1


def test_replicas_of_one_agent_get_distinct_ports():
    """02 는 동일 manifest 의 N replica 를 허용한다 — 이름만으로 잡으면 둘 다 깨진다."""
    allocator = A2APortAllocator(port_range=(9100, 9199))

    assert allocator.allocate("planner", replica=0) != allocator.allocate("planner", replica=1)


def test_released_port_returns_to_the_range():
    allocator = A2APortAllocator(port_range=(9100, 9101))
    first = allocator.allocate("planner")
    allocator.allocate("researcher")

    allocator.release("planner")

    # 회수하지 않으면 범위가 소진되어 아래가 실패한다
    assert allocator.allocate("writer") == first


def test_exhausted_range_fails_loudly():
    """조용히 겹쳐 주면 두 컨테이너가 같은 포트를 열고 원인이 드러나지 않는다."""
    allocator = A2APortAllocator(port_range=(9100, 9101))
    allocator.allocate("a")
    allocator.allocate("b")

    with pytest.raises(AaestroError) as excinfo:
        allocator.allocate("c")

    assert excinfo.value.code == ErrorCode.RT_001
    assert excinfo.value.details["port_range"] == [9100, 9101]


# --- 배선: launcher 를 통과하는 경로 ---------------------------------------
# 할당기만 테스트하면 #173 자체(아무도 부르지 않음)를 놓친다


async def test_launcher_assigns_a_port_to_an_a2a_agent():
    """#173 — 이 배선이 없어서 A2A 포트가 한 번도 열리지 않았다."""
    client = FakeDockerClient()
    allocator = A2APortAllocator(port_range=(9100, 9199))

    await launcher(client, allocator).start(manifest("planner"))

    assert a2a_ports(client.created[0]) == [allocator.assigned["planner/0"]]


async def test_launcher_leaves_non_a2a_agents_without_a_port():
    """A2A 를 안 쓰는 에이전트가 범위를 소비하면 수용량 검증이 어긋난다."""
    client = FakeDockerClient()
    allocator = A2APortAllocator(port_range=(9100, 9199))

    await launcher(client, allocator).start(manifest("echo"))

    assert a2a_ports(client.created[0]) == []
    assert allocator.assigned == {}


async def test_launcher_respects_an_explicit_port():
    """밖에서 정해 넘기는 경우(E2E compose)를 할당기가 덮어쓰면 안 된다."""
    client = FakeDockerClient()
    allocator = A2APortAllocator(port_range=(9100, 9199))

    await launcher(client, allocator).start(manifest("planner"), a2a_port=19102)

    assert a2a_ports(client.created[0]) == [19102]


async def test_stopping_an_agent_returns_its_port():
    """정지가 회수하지 않으면 재기동을 반복하는 운영에서 범위가 조용히 마른다."""
    client = FakeDockerClient()
    allocator = A2APortAllocator(port_range=(9100, 9199))
    agents = launcher(client, allocator)

    await agents.start(manifest("planner"))
    await agents.stop("planner")

    assert allocator.assigned == {}


async def test_two_a2a_agents_do_not_share_a_port():
    """같은 포트를 두 컨테이너가 열면 한쪽이 조용히 기동에 실패한다."""
    client = FakeDockerClient()
    allocator = A2APortAllocator(port_range=(9100, 9199))
    agents = launcher(client, allocator)

    await agents.start(manifest("planner"))
    await agents.start(manifest("researcher"))

    assert a2a_ports(client.created[0]) != a2a_ports(client.created[1])
