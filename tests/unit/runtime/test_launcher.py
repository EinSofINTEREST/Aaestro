"""Unit tests for agent launch wiring.

핵심 계약: **주입한 토큰과 호출에 싣는 토큰이 같다.** 어긋나면 컨테이너는
떴는데 모든 호출이 401 이 된다.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
import yaml

from aaestro.core.agent import AGENT_TOKEN_ENV
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import AgentManifest, GroupManifest
from aaestro.runtime.docker.engine import DockerEngine
from aaestro.runtime.launcher import AgentLauncher
from aaestro.runtime.lifecycle import AgentState
from aaestro.runtime.ports import A2APortAllocator
from aaestro.runtime.spec import container_name
from tests.fixtures.fake_docker import FakeDockerClient
from tests.fixtures.waiting import until

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


def quota_launcher(
    client: FakeDockerClient | None = None, **quotas: object
) -> tuple[AgentLauncher, dict[str, GroupManifest]]:
    """`research` 그룹 quota 를 지금 선언으로 찾는 launcher — 선언은 돌려준 표를 고쳐 바꾼다."""
    groups = {"research": quota_group(**quotas)}
    agents = AgentLauncher(
        engine=DockerEngine(client=client or FakeDockerClient()),
        groups=groups.get,
        restart_sleep=_no_wait,
    )
    return agents, groups


async def test_a_replica_past_the_group_quota_is_refused_before_its_container():
    """배포 검증은 선언을 센다 — 실제로 떠 있는 레플리카를 더하면 넘는 기동은 RT_006 이다."""
    client = FakeDockerClient()
    agents, _ = quota_launcher(client, max_agents=1)
    await agents.start(grouped())

    with pytest.raises(AaestroError) as excinfo:
        await agents.start(grouped(), replica=1)

    assert excinfo.value.code == ErrorCode.RT_006
    assert len(client.created) == 1, "거부한 레플리카의 컨테이너가 만들어졌다"
    assert ("worker", 1) not in agents.launched
    await agents.stop_all()


async def test_a_stopped_replica_frees_its_share_of_the_quota():
    client = FakeDockerClient()
    agents, _ = quota_launcher(client, cpu="1.0")
    await agents.start(grouped())
    await agents.stop("worker")

    await agents.start(grouped())

    assert len(client.created) == 2
    await agents.stop_all()


async def test_a_replica_whose_container_is_gone_holds_no_quota():
    """교체가 옛 컨테이너를 지우고 넘어지면 레플리카는 자리에 남지만 자원은 쥐지 않는다."""
    agents, _ = quota_launcher(cpu="1.0")
    first = await agents.start(grouped())
    first.stopped = True

    await agents.start(grouped("second"))

    assert ("second", 0) in agents.launched
    first.stopped = False
    await agents.stop_all()


async def test_members_of_other_groups_do_not_count_against_the_group():
    agents, _ = quota_launcher(cpu="1.0")
    await agents.start(grouped("outsider", group="other"))

    await agents.start(grouped())

    assert ("worker", 0) in agents.launched
    await agents.stop_all()


async def test_concurrent_launches_cannot_both_slip_under_the_quota():
    """검사 뒤 컨테이너를 만드는 사이에 두 기동이 서로를 못 보면 둘 다 통과한다."""
    import asyncio

    client = FakeDockerClient()
    agents, _ = quota_launcher(client, max_agents=1)

    results = await asyncio.gather(
        agents.start(grouped(), replica=0),
        agents.start(grouped(), replica=1),
        return_exceptions=True,
    )

    refused = [r for r in results if isinstance(r, AaestroError)]
    assert [r.code for r in refused] == [ErrorCode.RT_006]
    assert len(client.created) == 1
    await agents.stop_all()


async def test_a_second_launch_of_a_starting_replica_keeps_the_first_reservation(monkeypatch):
    """같은 자리의 두 번째 기동이 예약을 덮어쓰거나 지우면 다른 에이전트가 그 자리를 못 센다."""
    import asyncio

    agents, _ = quota_launcher(max_agents=1)
    release = asyncio.Event()
    original = agents.engine.start

    async def held_start(spec):
        await release.wait()
        return await original(spec)

    monkeypatch.setattr(agents.engine, "start", held_start)
    first = asyncio.create_task(agents.start(grouped()))
    await asyncio.sleep(0)
    # 거부는 await 전에 난다 — 통과해 버리면 막힌 엔진에서 기다리므로 태스크로 띄워 본다
    duplicate = asyncio.create_task(agents.start(grouped()))
    await asyncio.sleep(0)
    try:
        assert duplicate.done(), "같은 레플리카의 두 번째 기동이 거부되지 않았다"
        assert isinstance(duplicate.exception(), AaestroError)
        assert duplicate.exception().code == ErrorCode.RT_010
        with pytest.raises(AaestroError) as other:
            await agents.start(grouped("other"))
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
    agents, _ = quota_launcher(client, cpu="1.0")
    launched = await agents.start(grouped())
    launched.lifecycle.record_health(healthy=False, threshold=1)

    await agents._restart(launched)

    assert len(client.created) == 2
    assert agents.launched["worker", 0].lifecycle.state is AgentState.STARTING
    await agents.stop_all()


async def test_a_restart_refused_by_the_quota_fails_permanently():
    """quota 초과는 같은 선언으로 다시 세워도 같다 — backoff 로 반복하지 않고 Failed 로 간다."""
    client = FakeDockerClient()
    agents, _ = quota_launcher(client, cpu="1.5")
    launched = await agents.start(grouped())
    # 그 사이 같은 그룹의 다른 에이전트가 quota 를 통과해 기동 중이다
    agents._admitted["other", 0] = grouped("other")
    launched.lifecycle.record_health(healthy=False, threshold=1)

    await agents._restart(launched)

    assert len(client.created) == 1
    assert launched.lifecycle.state is AgentState.FAILED
    await agents.stop_all()


async def test_a_restart_sees_a_quota_raised_after_the_first_launch():
    """기동 때의 그룹 스냅샷으로 재시작하면 늘린 quota 로 들어온 이웃 때문에 Failed 로 끝난다."""
    client = FakeDockerClient()
    agents, groups = quota_launcher(client, cpu="1.0")
    alpha = await agents.start(grouped("alpha"))
    groups["research"] = quota_group(cpu="2.0")
    await agents.start(grouped("beta"))
    alpha.lifecycle.record_health(healthy=False, threshold=1)

    await agents._restart(alpha)

    assert agents.launched["alpha", 0].lifecycle.state is AgentState.STARTING
    assert len(client.created) == 3
    await agents.stop_all()


async def test_a_restart_after_the_quota_shrank_fails_permanently():
    """줄인 quota 는 같은 선언으로 다시 세워도 넘는다 — 재시도 없이 Failed."""
    client = FakeDockerClient()
    agents, groups = quota_launcher(client, cpu="2.0")
    alpha = await agents.start(grouped("alpha"))
    await agents.start(grouped("beta"))
    groups["research"] = quota_group(cpu="1.0")
    alpha.lifecycle.record_health(healthy=False, threshold=1)

    await agents._restart(alpha)

    assert alpha.lifecycle.state is AgentState.FAILED
    assert len(client.created) == 2
    await agents.stop_all()


async def _no_wait(_delay: float) -> None:
    return None


# --- 취소 경로의 quota 셈 (#648) ----------------------------------------------------


class HeldClient(FakeDockerClient):
    """고른 Docker 호출을 스레드 안에서 붙잡는다 — 취소가 스레드를 멈추지 못하는 자리를 재현한다.

    붙잡힌 호출마다 문이 하나다 (`held`) — 차례로 하나씩 풀어 순서를 정한다.
    """

    def __init__(self, *steps: str) -> None:
        super().__init__()
        self.steps = set(steps)
        self.held: list[threading.Event] = []
        self.opened = False

    def _hold(self, step: str) -> None:
        if step not in self.steps:
            return
        gate = threading.Event()
        self.held.append(gate)
        if self.opened:
            gate.set()
        gate.wait(5.0)

    def release(self) -> None:
        self.opened = True
        for gate in list(self.held):
            gate.set()

    def create(self, **kwargs):
        self._hold("create")
        return super().create(**kwargs)

    def remove(self, container_id: str) -> None:
        self._hold("remove")
        super().remove(container_id)


def held_launcher(client: HeldClient, **quotas: object) -> AgentLauncher:
    groups = {"research": quota_group(**quotas)}
    return AgentLauncher(
        engine=DockerEngine(client=client, cancel_wait_s=0.01),
        groups=groups.get,
        restart_sleep=_no_wait,
    )


def fresh_engine_log(monkeypatch) -> None:
    """앞선 테스트가 모듈 로거를 굳혀 두면 capture_logs 가 보지 못한다 (test_engine 과 같다)."""
    import structlog

    from aaestro.runtime.docker import engine as engine_module

    monkeypatch.setattr(engine_module, "log", structlog.get_logger("test"))


async def cancel_while_held(client: HeldClient, task: asyncio.Task, held: int) -> None:
    """`held` 번째 호출이 붙잡힐 때까지 기다려 태스크를 끊는다."""
    await until(lambda: len(client.held) >= held)
    task.cancel()
    await until(task.done, timeout_s=1.0)
    assert task.cancelled()


async def test_a_cancelled_start_handed_to_the_engine_holds_its_quota_until_cleaned_up():
    """#648 — 엔진이 정리를 넘겨받은 사이 컨테이너가 생기는 중인데 예약을 먼저 풀면 겹친다."""
    client = HeldClient("create")
    agents = held_launcher(client, max_agents=1)
    try:
        await cancel_while_held(client, asyncio.create_task(agents.start(grouped())), 1)

        with pytest.raises(AaestroError) as excinfo:
            await agents.start(grouped("other"))
        assert excinfo.value.code == ErrorCode.RT_006, "정리 중인 기동의 자리를 세지 않았다"
    finally:
        client.release()

    await agents.engine.settle_cleanups()
    assert client.removed, "넘겨받은 정리가 컨테이너를 지우지 않았다"
    assert not agents._settling

    await agents.start(grouped("other"))
    assert ("other", 0) in agents.launched
    await agents.stop_all()


async def test_two_cancelled_starts_of_one_replica_hold_the_quota_until_both_are_cleaned_up():
    """#648 — 같은 이름의 정리가 둘이면 먼저 끝난 하나가 다른 하나의 자리까지 풀었다."""
    client = HeldClient("create")
    agents = held_launcher(client, cpu="2.0")
    try:
        await cancel_while_held(client, asyncio.create_task(agents.start(grouped())), 1)
        await cancel_while_held(client, asyncio.create_task(agents.start(grouped())), 2)

        client.held[0].set()  # 첫 정리만 끝낸다
        await until(lambda: len(agents.engine._cleanups) == 1)

        with pytest.raises(AaestroError) as excinfo:
            await agents.start(grouped("other", cpu="2.0"))
        assert excinfo.value.code == ErrorCode.RT_006, "두 번째 정리가 도는 중에 자리가 풀렸다"
    finally:
        client.release()

    await agents.engine.settle_cleanups()
    assert not agents._settling
    await agents.stop_all()


async def test_shutdown_keeps_a_removal_its_cut_cleanup_left_running(monkeypatch):
    """#648 — 종료 상한에 끊긴 정리가 넘긴 제거를 놓치면 지우는 중인 컨테이너의 자리가 풀린다."""
    import structlog.testing

    fresh_engine_log(monkeypatch)

    client = HeldClient("create", "remove")
    agents = held_launcher(client, max_agents=1)
    try:
        await cancel_while_held(client, asyncio.create_task(agents.start(grouped())), 1)
        client.held[0].set()  # 생성은 끝나고 정리가 제거에서 붙잡힌다
        await until(lambda: len(client.held) == 2)

        with structlog.testing.capture_logs() as logs:
            await agents.engine.settle_cleanups(timeout_s=0.01)

        assert agents.engine._removals, "끊긴 정리가 넘긴 제거가 추적에서 빠졌다"
        assert [e for e in logs if e["event"].startswith("container removal still running")]
        with pytest.raises(AaestroError) as excinfo:
            await agents.start(grouped("other"))
        assert excinfo.value.code == ErrorCode.RT_006, "지우는 중인 컨테이너의 자리가 풀렸다"
    finally:
        client.release()

    await until(lambda: not agents.engine._cleanups)
    assert client.removed
    assert not agents._settling
    await agents.stop_all()


async def test_a_cancelled_stop_whose_removal_finished_frees_the_quota():
    """#648 — 취소된 정지가 끝낸 제거를 모르면 없는 컨테이너를 세어 다음 기동이 거짓 RT_006 이다."""
    client = HeldClient("remove")
    agents = held_launcher(client, max_agents=1)
    launched = await agents.start(grouped())
    await cancel_while_held(client, asyncio.create_task(agents.stop("worker")), 1)
    client.release()
    await agents.engine.settle_cleanups()

    assert client.removed
    assert launched.stopped, "제거가 끝났는데 남은 컨테이너로 센다"
    await agents.start(grouped("other"))
    assert ("other", 0) in agents.launched
    await agents.stop_all()


async def test_a_cancelled_stop_whose_removal_failed_reports_it_and_keeps_the_quota(monkeypatch):
    """#648 — 끊긴 뒤 실패한 제거가 WARN 없이 사라지고 예외가 루프로 새면 05 위반이다."""
    import gc

    import structlog.testing

    fresh_engine_log(monkeypatch)

    client = HeldClient("remove")
    agents = held_launcher(client, max_agents=1)
    launched = await agents.start(grouped())
    loop = asyncio.get_running_loop()
    leaked: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: leaked.append(context))
    try:
        with structlog.testing.capture_logs() as logs:
            await cancel_while_held(client, asyncio.create_task(agents.stop("worker")), 1)
            client._remove_failures = 1
            client.release()
            await until(lambda: not agents.engine._cleanups)
            await asyncio.sleep(0)
        gc.collect()
    finally:
        loop.set_exception_handler(None)

    failed = [e for e in logs if e["event"] == "container removal failed"]
    assert len(failed) == 1
    assert {"agent", "container_id", "image"} <= failed[0].keys()
    assert not leaked, "끊긴 제거의 예외가 회수되지 않았다"
    assert not launched.stopped
    with pytest.raises(AaestroError) as excinfo:
        await agents.start(grouped("other"))
    assert excinfo.value.code == ErrorCode.RT_006, "지우지 못한 컨테이너의 자리가 풀렸다"
    await agents.stop_all()


async def test_an_overlapping_stop_does_not_unmark_a_replica_an_earlier_removal_cleared():
    """#648 — 먼저 끝난 제거가 세운 표시를, 이미 없는 컨테이너에 대한 늦은 실패가 되돌렸다."""
    client = HeldClient("remove")
    agents = held_launcher(client, max_agents=1)
    launched = await agents.start(grouped())
    await cancel_while_held(client, asyncio.create_task(agents.stop("worker")), 1)
    assert not launched.stopped

    second = asyncio.create_task(agents.stop("worker"))
    await until(lambda: len(client.held) == 2)  # 두 번째 정지가 가드를 지나 제거에 닿았다
    client.held[0].set()
    await until(lambda: launched.stopped)
    client._remove_failures = 1  # 이미 지운 컨테이너 — 두 번째 제거는 실패한다
    client.held[1].set()
    await second

    assert launched.stopped, "지운 레플리카가 다시 남은 것으로 돌아갔다"
    assert ("worker", 0) not in agents.launched
    client.release()
    await agents.start(grouped("other"))
    await agents.stop_all()


async def test_cancelled_starts_of_one_replica_with_different_declarations_are_not_undercounted():
    """#648 — 첫 선언 하나를 건수에 곱하면 CPU 1 과 2 의 두 자리를 2 로 세어 상한을 넘긴다."""
    client = HeldClient("create")
    agents = held_launcher(client, cpu="4.0")
    try:
        await cancel_while_held(client, asyncio.create_task(agents.start(grouped(cpu="1.0"))), 1)
        await cancel_while_held(client, asyncio.create_task(agents.start(grouped(cpu="2.0"))), 2)

        with pytest.raises(AaestroError) as excinfo:
            await agents.start(grouped("other", cpu="1.5"))
        assert excinfo.value.code == ErrorCode.RT_006, "정리 중인 두 자리의 예약 3 을 2 로 셌다"
    finally:
        client.release()

    await agents.engine.settle_cleanups()
    await agents.stop_all()


async def test_admit_counts_agent_and_sidecar_maxima_of_differing_declarations_separately():
    """#648 — 합계의 최댓값은 에이전트 몫이 큰 선언과 사이드카 몫이 큰 선언이 갈리면 모자란다."""
    from tests.fixtures.builders import make_manifest

    sidecar = make_manifest(
        metadata={"name": "worker", "version": "0.1.0", "group": "research"},
        spec={
            "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
            "promptset": {"ref": "promptsets/test@0.1.0"},
            "runtime": {"resources": {"cpu": "1.0", "memory": "1Gi"}},
            "mcp": {
                "servers": [
                    {
                        "name": "browser",
                        "transport": "streamable-http",
                        "sidecar": {
                            "image": "mcp/playwright:1.2.0",
                            "resources": {"cpu": "2.0", "memory": "1Gi"},
                        },
                    }
                ]
            },
        },
    )
    agents = held_launcher(HeldClient("create"), cpu="5.5")
    # 정리 중인 한 자리가 두 선언 중 어느 것인지 모른다 — 에이전트 2 + 이번 2 + 사이드카 2 = 6
    agents._settling[asyncio.get_running_loop().create_future()] = [grouped(cpu="2.0"), sidecar]  # type: ignore[index]

    with pytest.raises(AaestroError) as excinfo:
        agents.admit(grouped(cpu="2.0"), replica=1)
    assert excinfo.value.code == ErrorCode.RT_006


def test_admit_accepts_fractional_cpu_that_exactly_fits_the_quota():
    """합계에서 사이드카 몫을 빼면 0.2 × 3 + 0.4 가 1.0 을 넘어 딱 맞는 quota 가 거절된다."""
    from tests.fixtures.builders import make_manifest

    worker = make_manifest(
        metadata={"name": "worker", "version": "0.1.0", "group": "research"},
        spec={
            "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
            "promptset": {"ref": "promptsets/test@0.1.0"},
            "runtime": {"resources": {"cpu": "0.2", "memory": "1Gi"}},
            "mcp": {
                "servers": [
                    {
                        "name": "browser",
                        "transport": "streamable-http",
                        "sidecar": {
                            "image": "mcp/playwright:1.2.0",
                            "resources": {"cpu": "0.4", "memory": "1Gi"},
                        },
                    }
                ]
            },
        },
    )
    agents = held_launcher(HeldClient("create"), cpu="1.0")
    agents._admitted[("worker", 0)] = worker
    agents._admitted[("worker", 1)] = worker

    agents.admit(worker, replica=2)


# --- 재부착이 세는 한도 (#649) ----------------------------------------------------


async def adopt_first(
    client: FakeDockerClient, agents: AgentLauncher, declared=None, found=None
) -> bool:
    """한 번 띄운 컨테이너를 (같은 프로세스에서) 다시 붙인다 — 재부착의 한도 읽기만 본다.

    ``found`` 는 기동 뒤 그 컨테이너의 inspect 값에 덮을 필드다 (기동 대조는 통과시킨다).
    """
    first = await agents.start(manifest())
    agents.launched.clear()
    if found is not None:
        container_id = first.handle.container_id
        client.hardening_by_id[container_id] = {**client.hardening_of(container_id), **found}
    return await agents.adopt(
        first.agent,
        replica=0,
        container_id=first.handle.container_id,
        image=first.handle.image,
        control_port=first.handle.control_port,
        token="remembered",  # noqa: S106 — 테스트 값
        restart_args={"manifest": declared or manifest()},
    )


@pytest.mark.parametrize(
    "found", [{"NanoCpus": None}, {"NanoCpus": 0}, {"Memory": -1}, {"Memory": False}]
)
async def test_adopt_refuses_a_container_without_resource_limits(found):
    """경화 대조 뒤에 한도가 사라졌어도 선언으로 세지 않는다 — 붙이지 않는다 (#649)."""
    client = FakeDockerClient()
    agents = launcher(client)

    assert await adopt_first(client, agents, found=found) is False
    assert agents.launched == {}


async def test_adopt_refuses_a_container_whose_limits_cannot_be_read():
    client = FakeDockerClient()
    agents = launcher(client)
    first = await agents.start(manifest())
    agents.launched.clear()
    client._hardening_error = RuntimeError("daemon hiccup")

    adopted = await agents.adopt(
        first.agent,
        replica=0,
        container_id=first.handle.container_id,
        image=first.handle.image,
        control_port=first.handle.control_port,
        token="remembered",  # noqa: S106 — 테스트 값
    )

    assert adopted is False
    assert agents.launched == {}


async def test_adopt_counts_a_sidecar_as_the_larger_of_declared_and_held():
    """사이드카 몫은 서버마다 max(지금 선언, 실제 한도) — 줄인 선언으로 과소 계산하지 않는다."""
    from aaestro.runtime.sidecars import McpSidecars

    client = FakeDockerClient()
    agents = launcher(client)

    def declared(cpu: str) -> AgentManifest:
        doc = manifest().model_dump(by_alias=True, mode="json")
        sidecar = {"image": "mcp/search:0.3.0", "resources": {"cpu": cpu, "memory": "64Mi"}}
        doc["spec"]["mcp"] = {
            "servers": [{"name": "search", "transport": "streamable-http", "sidecar": sidecar}]
        }
        return AgentManifest.model_validate(doc)

    await McpSidecars(client).start(declared("1.0"), {})

    assert await adopt_first(client, agents, declared("0.25")) is True
    held = agents.sidecars_held["echo"]
    assert held.cpu_cores == 1.0
    assert held.memory_bytes == 64 * 1024**2


async def test_adopt_refuses_when_a_declared_sidecar_is_missing():
    """선언한 사이드카의 한도를 확인하지 못하면 붙이지 않는다 — 세지 못한 몫이 남는다."""
    client = FakeDockerClient()
    agents = launcher(client)
    doc = manifest().model_dump(by_alias=True, mode="json")
    doc["spec"]["mcp"] = {
        "servers": [
            {"name": "search", "transport": "streamable-http",
             "sidecar": {"image": "mcp/search:0.3.0"}}
        ]
    }  # fmt: skip

    assert await adopt_first(client, agents, AgentManifest.model_validate(doc)) is False


def with_sidecar_cpu(cpu: str, *, name: str = "echo") -> AgentManifest:
    """echo (0.5 코어) 에 CPU 만 정한 사이드카 하나를 단 선언."""
    doc = manifest().model_dump(by_alias=True, mode="json")
    doc["metadata"]["name"] = name
    sidecar = {"image": "mcp/search:0.3.0", "resources": {"cpu": cpu, "memory": "64Mi"}}
    doc["spec"]["mcp"] = {
        "servers": [{"name": "search", "transport": "streamable-http", "sidecar": sidecar}]
    }
    return AgentManifest.model_validate(doc)


async def stop_the_adopted_replica_while_another_starts(*, fail: bool) -> AgentLauncher:
    """실제 1.0 · 선언 0.25 인 사이드카로 레플리카 0 을 재부착하고, 레플리카 1 이 엔진 기동을
    기다리는 동안 레플리카 0 을 멈춘 뒤 기동을 끝낸다 (``fail`` 이면 기동이 실패한다)."""
    from aaestro.runtime.sidecars import McpSidecars

    client = FakeDockerClient()
    agents = launcher(client)
    await McpSidecars(client).start(with_sidecar_cpu("1.0"), {})
    assert await adopt_first(client, agents, with_sidecar_cpu("0.25")) is True

    entered, gate = asyncio.Event(), asyncio.Event()
    original = agents.engine.start

    async def held_start(spec):
        entered.set()
        await gate.wait()
        if fail:
            raise AaestroError(
                category=ErrorCategory.RUNTIME, code=ErrorCode.RT_001, message="boom"
            )
        return await original(spec)

    agents.engine.start = held_start  # type: ignore[method-assign]
    starting = asyncio.create_task(agents.start(with_sidecar_cpu("0.25"), replica=1))
    await entered.wait()
    await agents.stop("echo", replica=0)
    gate.set()
    if fail:
        with pytest.raises(AaestroError):
            await starting
    else:
        await starting
    agents.engine.start = original  # type: ignore[method-assign]
    return agents


async def test_a_sidecar_share_outlives_a_stop_while_another_replica_starts():
    """레플리카 1 이 기동 중일 때 레플리카 0 을 멈춰도 사이드카는 돈다 — 몫을 지우지 않는다 (#649).

    echo 0.5 + 사이드카 실제 1.0 + beta 0.5 = 2.0 > 1.75. 몫을 잊으면 1.25 로 통과한다.
    """
    agents = await stop_the_adopted_replica_while_another_starts(fail=False)
    assert ("echo", 1) in agents.launched
    assert "echo" in agents.sidecars_held
    agents.host_cpu_cores = 1.75

    beta = manifest().model_dump(by_alias=True, mode="json")
    beta["metadata"]["name"] = "beta"
    with pytest.raises(AaestroError) as caught:
        await agents.start(AgentManifest.model_validate(beta))

    assert caught.value.code == ErrorCode.RT_006
    await agents.stop_all()


async def test_a_sidecar_share_is_dropped_when_the_pending_start_fails():
    """미룬 정리는 끝나는 기동이 마저 한다 — 레플리카가 남지 않으면 몫도 토큰과 함께 버린다."""
    agents = await stop_the_adopted_replica_while_another_starts(fail=True)

    assert agents.launched == {}
    assert "echo" not in agents.sidecars_held


async def test_a_released_replica_keeps_its_container_and_quota_until_the_next_stop():
    """lost 배포의 레플리카는 Stopped 로 남아 quota 를 쥐고, 다음 정지가 지운다 (#659)."""
    client = FakeDockerClient()
    agents, _ = quota_launcher(client, max_agents=1)
    launched = await agents.start(grouped())

    await agents.release("worker", replica=0)

    assert agents.launched[("worker", 0)] is launched
    assert launched.lifecycle.state is AgentState.STOPPED
    assert not client.removed, "놓은 컨테이너가 지워졌다"
    assert ("worker", 0) not in agents._monitors
    with pytest.raises(AaestroError) as routed:
        agents.route("worker")
    assert routed.value.code == ErrorCode.RT_009
    with pytest.raises(AaestroError) as excinfo:
        await agents.start(grouped("other"))
    assert excinfo.value.code == ErrorCode.RT_006, "놓은 컨테이너의 자리가 풀렸다"

    await agents.stop_all()

    assert client.removed, "정지가 놓은 컨테이너를 지우지 않았다"
    assert agents.launched == {}
    await agents.start(grouped("other"))
    await agents.stop_all()


# --- 취소된 기동의 제거 실패 — launcher 가 소유한다 (#654) ------------------------------


def orphan_launcher(client: HeldClient, *, cancel_wait_s: float) -> AgentLauncher:
    groups = {"research": quota_group(max_agents=1)}
    return AgentLauncher(
        engine=DockerEngine(client=client, cancel_wait_s=cancel_wait_s),
        groups=groups.get,
        restart_sleep=_no_wait,
    )


async def assert_quota_held(agents: AgentLauncher, reason: str) -> None:
    with pytest.raises(AaestroError) as excinfo:
        await agents.start(grouped("other"))
    assert excinfo.value.code == ErrorCode.RT_006, reason


async def test_a_cancelled_start_whose_immediate_removal_failed_keeps_the_quota_until_stop():
    """#654 — 상한 안에 끝난 취소 기동의 제거가 실패하면 WARN 만 남고 자리가 풀렸다."""
    client = HeldClient("create")
    agents = orphan_launcher(client, cancel_wait_s=5.0)
    starting = asyncio.create_task(agents.start(grouped()))
    await until(lambda: len(client.held) >= 1)
    client._remove_failures = 1
    starting.cancel()
    await asyncio.sleep(0)  # 취소가 엔진의 기다림에 닿게 한다
    client.release()
    await until(starting.done, timeout_s=1.0)

    assert starting.cancelled()
    assert not agents.engine._cleanups, "그 자리에서 끝나야 하는 정리가 넘어갔다"
    assert not client.removed
    await assert_quota_held(agents, "제거에 실패한 컨테이너의 자리가 풀렸다")

    await agents.stop("worker")  # 다음 정지가 다시 지운다
    assert client.removed == ["container-0001" + "0" * 20]
    assert not agents._orphans
    await agents.start(grouped("other"))
    await agents.stop_all()


async def test_a_handed_off_cleanup_whose_removal_failed_keeps_the_quota_until_the_next_start():
    """#654 — 백그라운드 정리의 제거가 실패하면 정리 태스크가 끝나며 `_settling` 자리가 풀렸다."""
    client = HeldClient("create")
    agents = orphan_launcher(client, cancel_wait_s=0.01)
    try:
        await cancel_while_held(client, asyncio.create_task(agents.start(grouped())), 1)
        client._remove_failures = 1
    finally:
        client.release()
    await agents.engine.settle_cleanups()

    assert not agents._settling
    assert not client.removed
    await assert_quota_held(agents, "정리가 제거에 실패한 컨테이너의 자리가 풀렸다")

    # 같은 레플리카의 다음 기동이 남은 컨테이너를 먼저 지우고 자리를 푼다
    await agents.start(grouped())
    assert len(client.removed) == 1
    assert not agents._orphans
    assert ("worker", 0) in agents.launched
    await agents.stop_all()


async def test_a_cut_cleanup_whose_removal_later_failed_keeps_the_quota():
    """#654 — 종료 상한에 끊긴 정리가 넘긴 제거가 늦게 실패하면 WARN 만 남고 자리가 풀렸다."""
    client = HeldClient("create", "remove")
    agents = orphan_launcher(client, cancel_wait_s=0.01)
    try:
        await cancel_while_held(client, asyncio.create_task(agents.start(grouped())), 1)
        client.held[0].set()  # 생성은 끝나고 정리가 제거에서 붙잡힌다
        await until(lambda: len(client.held) == 2)
        await agents.engine.settle_cleanups(timeout_s=0.01)
        client._remove_failures = 1
    finally:
        client.release()
    await until(lambda: not agents.engine._cleanups)

    assert not agents._settling
    assert agents._orphans, "끊긴 뒤 실패한 제거의 컨테이너를 넘겨받지 않았다"
    await assert_quota_held(agents, "끊긴 뒤 제거에 실패한 컨테이너의 자리가 풀렸다")

    await agents.stop_all()
    assert not agents._orphans
    assert client.removed


async def test_an_orphan_whose_removal_fails_again_stays_counted():
    """#654 — 다시 지우다 실패하면 놓지 않는다 — 놓으면 그 컨테이너는 다시 소유자가 없다."""
    client = HeldClient("create")
    agents = orphan_launcher(client, cancel_wait_s=0.01)
    try:
        await cancel_while_held(client, asyncio.create_task(agents.start(grouped())), 1)
        client._remove_failures = 2
    finally:
        client.release()
    await agents.engine.settle_cleanups()

    await agents.stop("worker")
    assert agents._orphans
    await assert_quota_held(agents, "다시 지우지 못한 컨테이너의 자리가 풀렸다")
    await agents.stop("worker")
    assert not agents._orphans


async def test_an_orphan_is_counted_with_every_declaration_its_cleanup_held():
    """#654 — 정리가 쥔 선언 중 첫 것만 넘기면 다시 선언된 큰 자리를 작은 것으로 센다."""
    client = HeldClient("create")
    agents = held_launcher(client, cpu="4.0")
    try:
        await cancel_while_held(client, asyncio.create_task(agents.start(grouped(cpu="1.0"))), 1)
        await cancel_while_held(client, asyncio.create_task(agents.start(grouped(cpu="2.0"))), 2)
        client._remove_failures = 1
        client.held[0].set()  # 첫 정리만 끝나며 제거에 실패한다
        await until(lambda: len(agents.engine._cleanups) == 1)
    finally:
        client.release()
    await agents.engine.settle_cleanups()

    assert len(agents._orphans) == 1
    with pytest.raises(AaestroError) as excinfo:
        await agents.start(grouped("other", cpu="2.5"))
    assert excinfo.value.code == ErrorCode.RT_006, "남은 컨테이너를 첫 선언 (CPU 1) 으로만 셌다"
    await agents.stop_all()


@pytest.mark.parametrize("failure", ["hardening_error", "start_error"])
async def test_a_failed_start_whose_half_made_container_was_not_removed_keeps_the_quota(failure):
    """#654 — 경화 대조 · 시작이 실패한 뒤 반쯤 만든 컨테이너의 제거가 실패하면 자리가 풀렸다."""
    client = FakeDockerClient(remove_failures=1, **{failure: RuntimeError("daemon hiccup")})
    agents = orphan_launcher(client, cancel_wait_s=0.01)
    with pytest.raises(AaestroError) as excinfo:
        await agents.start(grouped())
    assert excinfo.value.code == ErrorCode.RT_001

    assert not client.removed
    assert agents._orphans, "지우지 못한 반쯤 만든 컨테이너를 넘겨받지 않았다"
    await assert_quota_held(agents, "지우지 못한 반쯤 만든 컨테이너의 자리가 풀렸다")

    await agents.stop("worker")  # 다음 정지가 다시 지운다
    assert client.removed == ["container-0001" + "0" * 20]
    assert not agents._orphans
    setattr(client, f"_{failure}", None)
    await agents.start(grouped("other"))
    await agents.stop_all()


async def test_an_orphan_taken_by_a_smaller_start_keeps_the_larger_declaration():
    """#654 qa — 큰 선언의 정리가 남긴 컨테이너를 그 정리의 완료 콜백보다 먼저 작은 선언의 실패한
    기동이 넘겨받으면 CPU 2 의 컨테이너를 1 로 세었다."""
    from aaestro.runtime.docker.engine import ContainerHandle
    from aaestro.runtime.spec import container_name

    client = FakeDockerClient(start_error=RuntimeError("daemon hiccup"))
    agents = held_launcher(client, cpu="4.0")
    name = container_name("worker", 0)
    failed, finish = asyncio.Event(), asyncio.Event()
    big = ContainerHandle(agent="worker", container_id="big" + "0" * 20, image="x", control_port=0)

    async def big_cleanup() -> None:
        # 엔진의 정리가 제거에 실패해 넘긴 직후 — 완료 콜백 (`settled`) 은 아직 돌지 않았다
        agents.engine._orphans.setdefault(name, []).append(big)
        failed.set()
        await finish.wait()

    cleanup = asyncio.create_task(big_cleanup())
    agents.engine._hold(cleanup, name)
    agents._settle(grouped(cpu="2.0"), replica=0)
    await failed.wait()

    with pytest.raises(AaestroError):  # 작은 선언의 기동이 실패하며 그 이름의 것을 넘겨받는다
        await agents.start(grouped(cpu="1.0"))
    finish.set()
    await cleanup
    await asyncio.sleep(0)

    assert not agents._settling
    assert big.container_id in agents._orphans
    client._start_error = None
    with pytest.raises(AaestroError) as excinfo:
        await agents.start(grouped("other", cpu="2.5"))
    assert excinfo.value.code == ErrorCode.RT_006, "CPU 2 의 남은 컨테이너를 1 로 셌다"
    await agents.stop_all()


async def test_stop_all_retries_an_orphan_its_settling_left_behind():
    """#654 qa — 종료가 정리를 기다리는 사이 제거에 실패한 컨테이너는 다시 지우는 순회가 없었다."""
    client = HeldClient("create", "remove")
    agents = orphan_launcher(client, cancel_wait_s=0.01)
    await cancel_while_held(client, asyncio.create_task(agents.start(grouped())), 1)
    client.held[0].set()  # 생성은 끝나고 정리가 제거에서 붙잡힌다
    await until(lambda: len(client.held) == 2)

    stopping = asyncio.create_task(agents.stop_all())
    await asyncio.sleep(0)  # 정지 대상 이름이 정해지고 정리를 기다리기 시작한다
    assert not agents._orphans
    client._remove_failures = 1
    client.release()
    await stopping

    assert not agents._orphans, "종료 중에 생긴 남은 컨테이너를 다시 지우지 않았다"
    assert client.removed == ["container-0001" + "0" * 20]


@pytest.mark.parametrize("removal_fails", [False, True])
async def test_stop_keeps_the_port_while_a_cut_replacement_is_still_settling(removal_fails):
    """해체의 정지가 끊은 교체 기동을 엔진이 아직 정리 중이면 포트를 놓지 않는다 — 정리가 지우면
    그때 놓고, 지우지 못하면 넘겨받은 컨테이너가 쥔다 (#686)."""
    client = HeldClient()  # 처음 기동은 붙잡지 않는다
    agents = held_launcher(client)
    agents.ports = A2APortAllocator(port_range=(9100, 9110))
    worker = grouped()
    port = 9105  # 배포가 provision 에서 잡아 넘기는 포트
    agents.ports.reserve(worker.name, port)
    launched = await agents.start(worker, a2a_port=port)
    client.steps = {"create"}
    replacing = asyncio.create_task(agents._replace(launched))  # noqa: SLF001
    try:
        await until(lambda: len(client.held) >= 1)
        replacing.cancel()
        await until(replacing.done, timeout_s=1.0)
        assert agents.engine.pending_cleanups(container_name(worker.name, 0))
        await agents.stop(worker.name)
        assert agents.ports.held(worker.name) == port, "정리 중인 교체 기동의 포트를 정지가 놓았다"
        client._remove_failures = int(removal_fails)  # noqa: SLF001
    finally:
        client.release()
    await agents.engine.settle_cleanups()

    if removal_fails:
        assert agents.orphans_of(worker.name)
        assert agents.ports.held(worker.name) == port, "지우지 못한 컨테이너의 포트를 놓았다"
        await agents.stop(worker.name)
    assert agents.ports.held(worker.name) is None
    assert not agents.orphans_of(worker.name)
