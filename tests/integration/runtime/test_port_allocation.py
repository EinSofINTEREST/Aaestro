"""A2A port allocation against real containers.

03 Port Assignment 는 포트를 **runtime 이** 준다고 규정한다. #173 이 그
할당기를 만들었지만, E2E 는 compose 가 **고정 포트**를 주입해 통과했다 —
할당 경로가 한 번도 돌지 않았다 (#170).

compose 는 정적이라 동적 할당과 맞지 않는다: runtime 이 컨테이너를 직접 띄우는
경로(`AgentLauncher`)를 태워야 한다.
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
from tests.integration.runtime.test_docker_lifecycle import (
    CliDockerClient,
    docker,
    echo_image,  # noqa: F401 — fixture
    requires_docker,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
TEST_NETWORK = "aaestro-net-ports"
PORT_RANGE = (9300, 9399)


def a2a_manifest(name: str) -> AgentManifest:
    """A2A 를 켠 manifest — 포트를 받아야 하는 조건이다."""
    raw = yaml.safe_load((REPO_ROOT / "agents" / "echo" / "manifest.yaml").read_text("utf-8"))
    raw["metadata"]["name"] = name
    raw["spec"]["a2a"]["enabled"] = True
    return AgentManifest.model_validate(raw)


@pytest.fixture
async def launcher(echo_image: str):  # noqa: F811
    """실제 컨테이너를 띄우는 launcher — finalizer 가 반드시 정리한다."""
    client = CliDockerClient()
    agents = AgentLauncher(
        engine=DockerEngine(client=client, network=TEST_NETWORK),
        ports=A2APortAllocator(port_range=PORT_RANGE),
    )
    try:
        yield agents
    finally:
        await agents.stop_all()
        for container in client.containers:
            docker("rm", "-f", container, check=False)
        docker("network", "rm", TEST_NETWORK, check=False)


def injected_port(container_id: str) -> int:
    """컨테이너에 실제로 들어간 A2A 포트 — 스펙이 아니라 실행 중인 값을 본다."""
    raw = docker("inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", container_id)
    for line in raw.splitlines():
        key, _, value = line.partition("=")
        if key == A2A_PORT_ENV:
            return int(value)
    raise AssertionError(f"{A2A_PORT_ENV} was never injected")


@requires_docker
async def test_the_runtime_allocates_from_its_range(launcher):
    """#170 — compose 의 고정 포트로는 이 경로가 한 번도 돌지 않았다."""
    launched = await launcher.start(a2a_manifest("ports-one"))

    port = injected_port(launched.handle.container_id)

    assert PORT_RANGE[0] <= port <= PORT_RANGE[1]


@requires_docker
async def test_two_agents_do_not_share_a_port(launcher):
    """같은 포트를 두 컨테이너가 열면 한쪽이 조용히 기동에 실패한다."""
    first = await launcher.start(a2a_manifest("ports-a"))
    second = await launcher.start(a2a_manifest("ports-b"))

    assert injected_port(first.handle.container_id) != injected_port(second.handle.container_id)


@requires_docker
async def test_a_stopped_agent_returns_its_port(launcher):
    """회수하지 않으면 재기동을 반복하는 운영에서 범위가 조용히 마른다."""
    launched = await launcher.start(a2a_manifest("ports-recycle"))
    taken = injected_port(launched.handle.container_id)
    await launcher.stop("ports-recycle")

    again = await launcher.start(a2a_manifest("ports-recycle"))

    assert injected_port(again.handle.container_id) == taken


@requires_docker
async def test_an_exhausted_range_fails_loudly(echo_image):  # noqa: F811
    """조용히 겹쳐 주면 원인이 포트라는 것이 드러나지 않는다."""
    client = CliDockerClient()
    agents = AgentLauncher(
        engine=DockerEngine(client=client, network=TEST_NETWORK),
        # 한 자리뿐인 범위 — 두 번째 기동이 막혀야 한다
        ports=A2APortAllocator(port_range=(9400, 9400)),
    )
    try:
        await agents.start(a2a_manifest("ports-full-a"))

        with pytest.raises(AaestroError) as excinfo:
            await agents.start(a2a_manifest("ports-full-b"))

        assert excinfo.value.code == ErrorCode.RT_001
    finally:
        await agents.stop_all()
        for container in client.containers:
            docker("rm", "-f", container, check=False)


@requires_docker
async def test_an_agent_without_a2a_consumes_no_port(launcher):
    """A2A 를 안 쓰는 에이전트가 범위를 소비하면 수용량 검증이 어긋난다."""
    raw = yaml.safe_load((REPO_ROOT / "agents" / "echo" / "manifest.yaml").read_text("utf-8"))
    raw["metadata"]["name"] = "ports-quiet"
    manifest = AgentManifest.model_validate(raw)

    launched = await launcher.start(manifest)

    raw_env = docker(
        "inspect",
        "--format",
        "{{range .Config.Env}}{{println .}}{{end}}",
        launched.handle.container_id,
    )
    assert A2A_PORT_ENV not in raw_env
