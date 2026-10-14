"""Unit tests for agent launch wiring.

핵심 계약: **주입한 토큰과 호출에 싣는 토큰이 같다.** 어긋나면 컨테이너는
떴는데 모든 호출이 401 이 된다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aaestro.core.agent import AGENT_TOKEN_ENV
from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.core.manifest import AgentManifest
from aaestro.runtime.docker.engine import DockerEngine
from aaestro.runtime.launcher import AgentLauncher
from tests.fixtures.fake_docker import FakeDockerClient

REPO_ROOT = Path(__file__).resolve().parents[3]


def manifest() -> AgentManifest:
    return AgentManifest.model_validate(
        yaml.safe_load((REPO_ROOT / "agents" / "echo" / "manifest.yaml").read_text("utf-8"))
    )


def launcher(client: FakeDockerClient | None = None) -> AgentLauncher:
    return AgentLauncher(engine=DockerEngine(client=client or FakeDockerClient()))


async def test_injected_and_carried_tokens_match():
    """발급처와 사용처가 어긋나면 컨테이너는 떴는데 호출이 전부 막힌다."""
    client = FakeDockerClient()
    launched = await launcher(client).start(manifest())

    injected = client.created[0]["environment"][AGENT_TOKEN_ENV]

    assert injected == launched.client._token


async def test_declared_secrets_survive_launch():
    client = FakeDockerClient()

    await launcher(client).start(manifest(), secrets={"ANTHROPIC_API_KEY": "sk-x"})

    assert client.created[0]["environment"]["ANTHROPIC_API_KEY"] == "sk-x"


async def test_client_points_at_the_mapped_port():
    """runtime 이 할당한 포트로 붙어야 한다 — 하드코딩하면 replica 가 깨진다."""
    client = FakeDockerClient(host_port=51000)

    launched = await launcher(client).start(manifest())

    assert "51000" in launched.client._base_url


async def test_each_agent_gets_its_own_token():
    """한 에이전트의 토큰으로 다른 에이전트를 열 수 없어야 한다."""
    launch = launcher()
    first = await launch.start(manifest())
    second = await launch.start(
        manifest().model_copy(
            update={"metadata": manifest().metadata.model_copy(update={"name": "other"})}
        )
    )

    assert first.client._token != second.client._token


async def test_stop_forgets_the_token():
    """죽은 에이전트의 토큰을 들고 있지 않는다."""
    launch = launcher()
    await launch.start(manifest())

    await launch.stop("echo")

    assert launch.issuer.known("echo") is None
    assert launch.launched == {}


async def test_stop_unknown_agent_is_a_noop():
    launch = launcher()

    await launch.stop("never-started")

    assert launch.launched == {}


async def test_stop_all_clears_every_agent():
    launch = launcher()
    await launch.start(manifest())

    await launch.stop_all()

    assert launch.launched == {}


async def test_clients_mapping_feeds_the_node_runtime():
    """ControlNodeRuntime 이 이 매핑을 그대로 받는다."""
    launch = launcher()
    await launch.start(manifest())

    clients = launch.clients()

    assert set(clients) == {"echo"}


async def test_stopped_container_is_removed():
    client = FakeDockerClient()
    launch = launcher(client)
    launched = await launch.start(manifest())

    await launch.stop("echo")

    assert launched.handle.container_id in client.removed


@pytest.mark.parametrize("replica", [0, 2])
async def test_replica_index_reaches_the_container_name(replica):
    client = FakeDockerClient()

    await launcher(client).start(manifest(), replica=replica)

    assert client.created[0]["name"].endswith(str(replica))


# --- lifecycle 상태의 무결성 -------------------------------------------------


async def test_launching_the_same_replica_twice_is_rejected():
    """덮어쓰면 첫 컨테이너가 미아가 된다 — **같은 레플리카**일 때만이다."""
    client = FakeDockerClient()
    launch = launcher(client)
    await launch.start(manifest())

    with pytest.raises(AaestroError) as exc_info:
        await launch.start(manifest(), replica=0)

    assert exc_info.value.code == ErrorCode.RT_010
    # 거부는 기동 전에 — 컨테이너를 만들어놓고 버리면 안 된다
    assert len(client.created) == 1


async def test_failed_stop_keeps_the_handle_for_retry():
    """유일한 재시도 수단을 먼저 버리면 미아 컨테이너를 정리할 방법이 없다.

    DockerEngine.stop 은 실패를 삼키므로(유령 컨테이너 방지) 실제로 터질 수
    있는 지점은 클라이언트 정리다.
    """
    client = FakeDockerClient()
    launch = launcher(client)
    launched = await launch.start(manifest())

    async def failing_aclose() -> None:
        raise RuntimeError("connection pool busy")

    launched.client.aclose = failing_aclose  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await launch.stop("echo")

    # 내부 키가 아니라 동작으로 본다: 핸들이 남아야 다시 정리할 수 있다
    assert launch.replicas_of("echo")
    assert launch.issuer.known("echo")


async def test_successful_stop_forgets_the_token():
    client = FakeDockerClient()
    launch = launcher(client)
    await launch.start(manifest())

    await launch.stop("echo")

    assert not launch.replicas_of("echo")
    assert not launch.issuer.known("echo")


async def test_stop_all_cleans_every_agent_before_reporting_failure():
    """하나가 실패해도 나머지를 정리해야 한다 — docstring 이 약속한 동작이다."""
    client = FakeDockerClient()
    launch = launcher(client)
    broken = await launch.start(manifest())

    async def failing_aclose() -> None:
        raise RuntimeError("connection pool busy")

    broken.client.aclose = failing_aclose  # type: ignore[method-assign]

    healthy = await launch.start(
        manifest().model_copy(
            update={"metadata": manifest().metadata.model_copy(update={"name": "other"})}
        )
    )

    with pytest.raises(AaestroError) as exc_info:
        await launch.stop_all()

    assert exc_info.value.code == ErrorCode.RT_005
    # 실패한 쪽은 재시도용으로 남고, 멀쩡한 쪽은 끝까지 정리된다
    assert launch.replicas_of("echo")
    assert not launch.replicas_of("other")
    assert healthy.handle.container_id in client.removed


# --- memory 접속 정보 주입 ---------------------------------------------------


async def test_memory_endpoint_reaches_the_container():
    """주소와 불투명 토큰만 넣는다 — DB 자격증명은 컨테이너에 들어가지 않는다."""
    from aaestro.memory.client import MEMORY_TOKEN_ENV, MEMORY_URL_ENV
    from aaestro.runtime.launcher import MemoryEndpoint

    client = FakeDockerClient()

    await launcher(client).start(
        manifest(), memory=MemoryEndpoint(url="http://memory:8080", token="opaque")
    )

    env = client.created[0]["environment"]
    assert env[MEMORY_URL_ENV] == "http://memory:8080"
    assert env[MEMORY_TOKEN_ENV] == "opaque"


async def test_no_memory_endpoint_leaves_the_environment_alone():
    """메모리를 쓰지 않는 에이전트에게 빈 값을 넣으면 기동이 헷갈린다."""
    from aaestro.memory.client import MEMORY_TOKEN_ENV, MEMORY_URL_ENV

    client = FakeDockerClient()

    await launcher(client).start(manifest())

    env = client.created[0]["environment"]
    assert MEMORY_URL_ENV not in env
    assert MEMORY_TOKEN_ENV not in env


async def test_storage_credentials_never_reach_the_container():
    """09 Access Enforcement 1 — 저장소 자격증명은 서비스만 보유한다."""
    from aaestro.runtime.launcher import MemoryEndpoint

    client = FakeDockerClient()

    await launcher(client).start(
        manifest(), memory=MemoryEndpoint(url="http://memory:8080", token="opaque")
    )

    env = client.created[0]["environment"]
    leaked = [key for key in env if "DSN" in key.upper() or "DATABASE" in key.upper()]
    assert leaked == []
