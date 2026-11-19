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
from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.core.manifest import AgentManifest, GroupManifest
from aaestro.runtime.docker.engine import DockerEngine
from aaestro.runtime.launcher import AgentLauncher
from aaestro.runtime.lifecycle import AgentState
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
