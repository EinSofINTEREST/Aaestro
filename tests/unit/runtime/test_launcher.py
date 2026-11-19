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
from aaestro.core.manifest import AgentManifest, GroupManifest
from aaestro.runtime.docker.engine import DockerEngine
from aaestro.runtime.launcher import AgentLauncher
from aaestro.runtime.lifecycle import AgentState
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


# --- 기동 시 quota 재검증 (#474) ----------------------------------------------------


def grouped(name: str = "worker", *, cpu: str = "1.0", group: str = "research") -> AgentManifest:
    """그룹에 속한, 리소스만 바꿔 쓰는 manifest."""
    from tests.fixtures.builders import make_manifest

    return make_manifest(
        metadata={"name": name, "version": "0.1.0", "group": group},
        spec={
            "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
            "promptset": {"ref": "promptsets/test@0.1.0"},
            "runtime": {"resources": {"cpu": cpu, "memory": "1Gi"}},
        },
    )


def quota_group(**quotas: object) -> GroupManifest:
    return GroupManifest.model_validate(
        {
            "apiVersion": "aaestro/v1",
            "kind": "Group",
            "metadata": {"name": "research", "version": "0.1.0"},
            "spec": {"quotas": quotas},
        }
    )


async def test_a_replica_past_the_group_quota_is_refused_before_its_container():
    """배포 검증은 선언을 센다 — 실제로 떠 있는 레플리카를 더하면 넘는 기동은 RT_006 이다."""
    client = FakeDockerClient()
    agents = launcher(client)
    group = quota_group(max_agents=1)
    await agents.start(grouped(), group=group)

    with pytest.raises(AaestroError) as excinfo:
        await agents.start(grouped(), replica=1, group=group)

    assert excinfo.value.code == ErrorCode.RT_006
    assert len(client.created) == 1, "거부한 레플리카의 컨테이너가 만들어졌다"
    assert ("worker", 1) not in agents.launched
    await agents.stop_all()


async def test_a_stopped_replica_frees_its_share_of_the_quota():
    client = FakeDockerClient()
    agents = launcher(client)
    group = quota_group(cpu="1.0")
    await agents.start(grouped(), group=group)
    await agents.stop("worker")

    await agents.start(grouped(), group=group)

    assert len(client.created) == 2
    await agents.stop_all()


async def test_a_replica_whose_container_is_gone_holds_no_quota():
    """교체가 옛 컨테이너를 지우고 넘어지면 레플리카는 자리에 남지만 자원은 쥐지 않는다."""
    agents = launcher()
    group = quota_group(cpu="1.0")
    first = await agents.start(grouped(), group=group)
    first.stopped = True

    await agents.start(grouped("second"), group=group)

    assert ("second", 0) in agents.launched
    first.stopped = False
    await agents.stop_all()


async def test_members_of_other_groups_do_not_count_against_the_group():
    agents = launcher()
    await agents.start(grouped("outsider", group="other"), group=None)

    await agents.start(grouped(), group=quota_group(cpu="1.0"))

    assert ("worker", 0) in agents.launched
    await agents.stop_all()


async def test_concurrent_launches_cannot_both_slip_under_the_quota():
    """검사 뒤 컨테이너를 만드는 사이에 두 기동이 서로를 못 보면 둘 다 통과한다."""
    import asyncio

    client = FakeDockerClient()
    agents = launcher(client)
    group = quota_group(max_agents=1)

    results = await asyncio.gather(
        agents.start(grouped(), replica=0, group=group),
        agents.start(grouped(), replica=1, group=group),
        return_exceptions=True,
    )

    refused = [r for r in results if isinstance(r, AaestroError)]
    assert [r.code for r in refused] == [ErrorCode.RT_006]
    assert len(client.created) == 1
    await agents.stop_all()


async def test_a_second_launch_of_a_starting_replica_keeps_the_first_reservation(monkeypatch):
    """같은 자리의 두 번째 기동이 예약을 덮어쓰거나 지우면 다른 에이전트가 그 자리를 못 센다."""
    import asyncio

    agents = launcher()
    release = asyncio.Event()
    original = agents.engine.start

    async def held_start(spec):
        await release.wait()
        return await original(spec)

    monkeypatch.setattr(agents.engine, "start", held_start)
    group = quota_group(max_agents=1)
    first = asyncio.create_task(agents.start(grouped(), group=group))
    await asyncio.sleep(0)
    # 거부는 await 전에 난다 — 통과해 버리면 막힌 엔진에서 기다리므로 태스크로 띄워 본다
    duplicate = asyncio.create_task(agents.start(grouped(), group=group))
    await asyncio.sleep(0)
    try:
        assert duplicate.done(), "같은 레플리카의 두 번째 기동이 거부되지 않았다"
        assert isinstance(duplicate.exception(), AaestroError)
        assert duplicate.exception().code == ErrorCode.RT_010
        with pytest.raises(AaestroError) as other:
            await agents.start(grouped("other"), group=group)
        assert other.value.code == ErrorCode.RT_006, "첫 기동의 예약이 사라졌다"
    finally:
        release.set()
        await asyncio.gather(first, duplicate, return_exceptions=True)
        await agents.stop_all()


async def test_the_host_capacity_counts_every_live_container():
    agents = AgentLauncher(engine=DockerEngine(client=FakeDockerClient()), host_cpu_cores=1.5)
    await agents.start(grouped("first"))

    with pytest.raises(AaestroError) as excinfo:
        await agents.start(grouped("second"))

    assert excinfo.value.code == ErrorCode.RT_006
    await agents.stop_all()


async def test_a_restart_replaces_its_own_share_instead_of_adding_to_it():
    """교체는 같은 자리를 다시 세운다 — 옛 것까지 세면 quota 경계의 에이전트가 못 선다."""
    client = FakeDockerClient()
    agents = AgentLauncher(engine=DockerEngine(client=client), restart_sleep=_no_wait)
    launched = await agents.start(grouped(), group=quota_group(cpu="1.0"))
    launched.lifecycle.record_health(healthy=False, threshold=1)

    await agents._restart(launched)

    assert len(client.created) == 2
    assert agents.launched["worker", 0].lifecycle.state is AgentState.STARTING
    await agents.stop_all()


async def test_a_restart_refused_by_the_quota_fails_permanently():
    """quota 초과는 같은 선언으로 다시 세워도 같다 — backoff 로 반복하지 않고 Failed 로 간다."""
    client = FakeDockerClient()
    agents = AgentLauncher(engine=DockerEngine(client=client), restart_sleep=_no_wait)
    launched = await agents.start(grouped(), group=quota_group(cpu="1.5"))
    # 그 사이 같은 그룹의 다른 에이전트가 quota 를 통과해 기동 중이다
    agents._admitted["other", 0] = grouped("other")
    launched.lifecycle.record_health(healthy=False, threshold=1)

    await agents._restart(launched)

    assert len(client.created) == 1
    assert launched.lifecycle.state is AgentState.FAILED
    await agents.stop_all()


async def _no_wait(_delay: float) -> None:
    return None
