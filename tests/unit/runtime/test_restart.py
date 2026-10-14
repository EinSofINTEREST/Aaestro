"""Automatic restart of an unhealthy agent.

`RestartPolicy` 와 `plan_restart()`(상한 초과 시 `RT_008`)는 구현되어 있었는데
**부르는 곳이 없었다** (#215). #213 이 상태를 `UNHEALTHY` 까지 옮겼고, 여기서
그 다음을 잇는다.

06 에 따라 실제로 자지 않는다 — backoff 대기를 주입해 기록만 한다.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
import yaml
from prometheus_client import CollectorRegistry

from aaestro.core.agent import HealthState, HealthStatus
from aaestro.core.errors import AaestroError
from aaestro.core.manifest import AgentManifest
from aaestro.observability.metrics import Metrics
from aaestro.runtime.docker.engine import DockerEngine
from aaestro.runtime.launcher import AgentLauncher
from aaestro.runtime.lifecycle import AgentState, RestartPolicy
from tests.fixtures.fake_docker import FakeDockerClient
from tests.fixtures.waiting import spin, until

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def always_sick(monkeypatch: pytest.MonkeyPatch) -> None:
    """모든 Control 클라이언트를 결정적으로 아프게 만든다.

    재기동된 레플리카는 launcher 가 **새로** 만드는 클라이언트를 쓰므로,
    인스턴스만 갈아끼우면 그 뒤가 실제 네트워크를 두드린다 — Control API
    재시도가 실제로 자면서(#217) 테스트가 실시간에 묶인다.
    """
    from aaestro.runtime.control import ControlClient

    async def sick(self: ControlClient) -> HealthStatus:
        return HealthStatus(status=HealthState.UNHEALTHY)

    monkeypatch.setattr(ControlClient, "health", sick)


def manifest() -> AgentManifest:
    return AgentManifest.model_validate(
        yaml.safe_load((REPO_ROOT / "agents" / "echo" / "manifest.yaml").read_text("utf-8"))
    )


class Recorder:
    """대기를 기록만 하는 sleep 대역 — 실제로 자지 않는다."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.waits.append(delay)


def launcher(client: FakeDockerClient, waits: Recorder, **kwargs) -> AgentLauncher:
    """health 는 매번 통과, 재시작 backoff 만 기록한다."""

    async def health_sleep(_delay: float) -> None:
        await asyncio.sleep(0)

    kwargs.setdefault("health_interval_s", 10.0)
    # 이 파일은 **기동 유예가 끝난 뒤**의 재시작 계약을 본다 — 유예 자체는
    # `test_the_startup_grace_does_not_restart_a_container_that_is_still_starting` 이 본다 (#297)
    kwargs.setdefault("startup_grace_s", 0.0)
    return AgentLauncher(
        engine=DockerEngine(client=client),
        health_sleep=health_sleep,
        restart_sleep=waits,
        **kwargs,
    )


async def start_sick(agents: AgentLauncher) -> tuple:
    """기동한다 — probe 는 fixture 가 클래스 수준에서 아프게 해 두었다."""
    launched = await agents.start(manifest())
    return launched, None


async def restart_now(agents: AgentLauncher, launched) -> None:
    """재시작을 **직접** 돌린다.

    감시 루프를 기다리면 event loop 회차에 묶여, 스위트 전체가 돌 때 경합한다.
    루프가 실제로 트리거하는지는 아래 `test_the_health_loop_triggers_a_restart`
    가 따로 본다 — 여기서는 재시작 자체의 계약을 결정적으로 확인한다.
    """
    launched.lifecycle.record_health(healthy=False, threshold=1)
    await agents._restart(launched)


async def test_an_unhealthy_agent_is_restarted():
    """#215 — RestartPolicy 를 부르는 곳이 없어 아픈 에이전트가 방치됐다."""
    client = FakeDockerClient()
    agents = launcher(client, Recorder())
    launched, _ = await start_sick(agents)

    await restart_now(agents, launched)

    assert len(client.created) > 1, "컨테이너가 다시 세워지지 않았다"
    await agents.stop_all()


async def test_a_restart_keeps_the_baked_image():
    """#266 — 재시작만 base 이미지로 돌면 커스텀 실행기가 조용히 사라진다."""
    client = FakeDockerClient()
    agents = launcher(client, Recorder())
    launched = await agents.start(manifest(), image="aaestro/agent-echo-baked:0.1.0")

    await restart_now(agents, launched)

    images = [created["image"] for created in client.created]
    assert images == ["aaestro/agent-echo-baked:0.1.0"] * 2
    await agents.stop_all()


async def test_the_startup_grace_does_not_restart_a_container_that_is_still_starting():
    """기동이 확인 주기보다 길면 뜨는 중인 컨테이너가 재시작돼 배포가 끝내 실패한다 (#297)."""
    client = FakeDockerClient()
    agents = launcher(client, Recorder(), startup_grace_s=1_000.0)
    launched, _ = await start_sick(agents)

    await spin(20)

    assert len(client.created) == 1, "유예 안에서 재시작했다"
    assert launched.lifecycle.state is AgentState.STARTING
    assert launched.lifecycle.consecutive_health_failures > 0, "실패를 세지도 않았다"
    await agents.stop_all()


async def test_the_health_loop_triggers_a_restart():
    """감시 루프가 Unhealthy 를 보면 재시작을 건다 — 이 배선이 #215 의 핵심이다."""
    client = FakeDockerClient()
    agents = launcher(client, Recorder())
    await start_sick(agents)

    await until(lambda: len(client.created) > 1)

    assert len(client.created) > 1
    await agents.stop_all()


async def test_the_restart_waits_for_the_backoff():
    """02 Rule 6 — 즉시 다시 세우면 crash loop 이 그대로 반복된다."""
    waits = Recorder()
    agents = launcher(FakeDockerClient(), waits)
    launched, _ = await start_sick(agents)

    await restart_now(agents, launched)

    assert waits.waits == [RestartPolicy().initial_delay_s]
    await agents.stop_all()


async def test_the_restart_counter_is_filled():
    """05 의 `ContainerRestartLoop` 알림은 이 카운터를 본다."""
    metrics = Metrics(registry=CollectorRegistry())
    waits = Recorder()
    agents = launcher(FakeDockerClient(), waits, metrics=metrics)
    launched, _ = await start_sick(agents)

    await restart_now(agents, launched)

    counter = metrics.counter("aaestro_container_restarts_total").labels(
        agent="echo", reason="unhealthy"
    )
    assert counter._value.get() == 1.0
    await agents.stop_all()


async def test_the_restart_ceiling_marks_the_agent_failed():
    """02 Rule 6 — 상한을 넘으면 Failed 로 가고 재시작을 멈춘다 (`RT_008`)."""
    client = FakeDockerClient()
    waits = Recorder()
    agents = launcher(client, waits)
    launched, _ = await start_sick(agents)
    # `should_give_up` 은 **초과**(>)를 본다 — 상한 0 이면 첫 시도에서
    # 포기하므로 재시작 회차를 기다리지 않고 그 경로를 직접 태운다
    launched.lifecycle.restart_policy = RestartPolicy(max_in_window=0)

    await until(lambda: launched.lifecycle.state is AgentState.FAILED)

    assert launched.lifecycle.state is AgentState.FAILED
    await agents.stop_all()


async def test_the_restart_window_survives_the_restart():
    """재시작이 lifecycle 을 **이어받아야** crash-loop 상한이 성립한다.

    새로 만들면 `RestartPolicy` 의 창(window)이 매번 리셋되어
    `should_give_up()` 이 영원히 참이 되지 않는다 — 무한히 되살아난다.

    감시 루프가 걸어 주기를 기다리지 않고 재시작을 **직접** 돌린다 —
    교체 도중에는 `replicas_of` 가 잠시 비므로, 그 틈을 폴링으로 넘겨다보면
    빈 목록을 집는다. 루프가 실제로 트리거하는지는
    `test_the_health_loop_triggers_a_restart` 가 따로 본다.
    """
    waits = Recorder()
    agents = launcher(FakeDockerClient(), waits)
    launched, _ = await start_sick(agents)
    before = launched.lifecycle

    await restart_now(agents, launched)

    revived = agents.replicas_of("echo")[0]
    assert revived.lifecycle is before, "재시작이 lifecycle 을 새로 만들면 상한이 리셋된다"
    assert before.restart_policy.restarts_in_window() == 1
    await agents.stop_all()


async def test_a_failed_agent_stops_being_watched():
    """포기한 에이전트를 계속 두드리면 runtime 이 그 대기에 묶인다."""
    waits = Recorder()
    agents = launcher(FakeDockerClient(), waits)
    launched, _ = await start_sick(agents)
    launched.lifecycle.restart_policy = RestartPolicy(max_in_window=0)

    await restart_now(agents, launched)

    # 감시가 남아 있으면 포기한 에이전트를 계속 두드린다
    assert not agents._monitors


async def test_routing_skips_an_unhealthy_replica():
    """재시작 중인 레플리카로 보내면 그 태스크가 전부 실패한다."""
    waits = Recorder()
    agents = launcher(FakeDockerClient(), waits)
    healthy = await agents.start(manifest(), replica=0)
    healthy.client.health = _always_well  # type: ignore[method-assign]
    sick, _ = await start_sick_replica(agents, replica=1)

    await until(lambda: not sick.lifecycle.accepts_tasks)

    assert all(agents.route("echo") is healthy.client for _ in range(3))
    await agents.stop_all()


async def _always_well() -> HealthStatus:
    return HealthStatus(status=HealthState.HEALTHY)


async def start_sick_replica(agents: AgentLauncher, *, replica: int) -> tuple:
    launched = await agents.start(manifest(), replica=replica)
    return launched, None


class HangingSleep(Recorder):
    """대기를 기록하고 **영원히 붙잡는다** — 재시작이 끝나지 않게 고정한다."""

    def __init__(self) -> None:
        super().__init__()
        self._blocked = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.waits.append(delay)
        await self._blocked.wait()


async def test_restart_is_not_scheduled_twice():
    """감시 루프는 Unhealthy 를 **매 확인마다** 보고한다 — 겹쳐 걸면 컨테이너가 난립한다.

    재시작을 backoff 에서 붙잡아 두면, 그 사이 들어오는 보고가 새 재시작을
    거는지 대기 횟수로 바로 드러난다.
    """
    waits = HangingSleep()
    agents = launcher(FakeDockerClient(), waits)
    await start_sick(agents)

    await until(lambda: bool(waits.waits))
    await spin(200)

    assert waits.waits == [RestartPolicy().initial_delay_s]


async def test_restart_is_off_without_supervision():
    """감시를 켜지 않은 조립은 재시작도 하지 않는다 — 루프의 소유자가 없다."""
    agents = AgentLauncher(engine=DockerEngine(client=FakeDockerClient()))

    await agents.start(manifest())
    await spin(30)

    assert not agents._restarts


async def test_a_reattached_agent_is_restarted_without_waiting_for_a_startup_grace():
    """재부착은 이미 Ready 였던 기록만 붙인다 — 유예를 다시 주면 고장이 늦게 드러난다."""
    client = FakeDockerClient()
    agents = launcher(client, Recorder(), startup_grace_s=1_000.0)
    first = await agents.start(manifest())
    await agents.stop_all()

    adopted = await agents.adopt(
        first.agent,
        replica=0,
        container_id=first.handle.container_id,
        image=first.handle.image,
        control_port=first.handle.control_port,
        token="remembered",  # noqa: S106 — 테스트 값
        restart_args={"manifest": manifest()},
    )

    assert adopted is True
    await until(lambda: len(client.created) > 1)
    await agents.stop_all()


class FakeClock:
    """재시작 창(window)이 읽는 시계 — backoff 대기만큼만 흐른다."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class ClockedSleep(Recorder):
    """대기를 기록하고 그만큼 시계를 흘린다 — 실제로 자지 않는다.

    `heal_after` 회 대기한 뒤 재기동이 다시 성공하게 엔진을 고친다.
    """

    def __init__(
        self, clock: FakeClock, client: FakeDockerClient, *, heal_after: int | None = None
    ) -> None:
        super().__init__()
        self._clock = clock
        self._client = client
        self._heal_after = heal_after

    async def __call__(self, delay: float) -> None:
        self.waits.append(delay)
        self._clock.now += delay
        if self._heal_after is not None and len(self.waits) > self._heal_after:
            self._client._start_error = None


class HangOnSecondSleep(HangingSleep):
    """첫 backoff 는 그냥 지나고 두 번째부터 붙잡는다 — 첫 재기동 실패 **뒤**에 고정한다."""

    async def __call__(self, delay: float) -> None:
        if not self.waits:
            self.waits.append(delay)
            return
        await super().__call__(delay)


def break_restarts(client: FakeDockerClient) -> None:
    """다음 재기동부터 `engine.start` 가 실패하게 한다 (`RT_001`)."""
    client._start_error = RuntimeError("daemon refused to start the container")


def restart_count(metrics: Metrics, reason: str) -> float:
    return (
        metrics.counter("aaestro_container_restarts_total")
        .labels(agent="echo", reason=reason)
        ._value.get()
    )


async def test_a_failed_restart_is_retried_with_backoff():
    """#482 — 재기동이 한 번 실패하면 감시도 재시도도 없이 그 레플리카가 사라졌다."""
    client = FakeDockerClient()
    clock = FakeClock()
    metrics = Metrics(registry=CollectorRegistry())
    waits = ClockedSleep(clock, client, heal_after=2)
    # 재시작을 직접 돌린다 — 감시 루프가 같은 재시작을 겹쳐 걸지 않게 끈다
    agents = launcher(client, waits, metrics=metrics, health_interval_s=None)
    launched, _ = await start_sick(agents)
    launched.lifecycle.restart_policy = RestartPolicy(clock=clock)
    break_restarts(client)

    await restart_now(agents, launched)

    # 첫 두 번의 재기동이 실패하고, 세 번째가 선다 — 매번 backoff 가 늘어난다
    assert waits.waits == [1.0, 2.0, 4.0]
    revived = agents.replicas_of("echo")
    assert len(revived) == 1, "재기동이 끝내 서지 않았다"
    assert revived[0].lifecycle is launched.lifecycle, "같은 lifecycle 을 이어붙여야 상한이 선다"
    assert revived[0].handle is not launched.handle
    assert restart_count(metrics, "failed") == 2.0
    assert restart_count(metrics, "unhealthy") == 1.0
    await agents.stop_all()


async def test_restarts_that_keep_failing_end_in_failed():
    """02 Rule 6 — 재기동 실패도 창 안의 재시작으로 센다: 상한을 넘으면 Failed + 알림."""
    client = FakeDockerClient()
    clock = FakeClock()
    metrics = Metrics(registry=CollectorRegistry())
    waits = ClockedSleep(clock, client)
    agents = launcher(client, waits, metrics=metrics, health_interval_s=None)
    launched, _ = await start_sick(agents)
    launched.lifecycle.restart_policy = RestartPolicy(clock=clock)
    break_restarts(client)

    await restart_now(agents, launched)

    # 1 → 2 → 4 → 8 → 16 — 여섯 번째 계획이 10분 안 5회 상한을 넘는다
    assert waits.waits == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert launched.lifecycle.state is AgentState.FAILED
    assert restart_count(metrics, "failed") == 5.0
    assert restart_count(metrics, "gave_up") == 1.0
    assert agents.replicas_of("echo") == [launched], "포기한 레플리카가 목록에서 사라졌다"
    await agents.stop_all()


async def test_a_replica_whose_restart_failed_stays_visible():
    """재기동 실패 뒤 레플리카가 목록에서 빠지면 배포는 READY 로만 보이고 정지도 못 한다."""
    client = FakeDockerClient()
    waits = HangOnSecondSleep()
    agents = launcher(client, waits)
    launched, _ = await start_sick(agents)
    break_restarts(client)
    launched.lifecycle.record_health(healthy=False, threshold=1)
    agents._schedule_restart(launched)

    # 첫 재기동이 실패하고 두 번째 backoff 에서 붙잡힌다
    await until(lambda: len(waits.waits) == 2)

    replicas = agents.replicas_of("echo")
    assert replicas == [launched], "재시도 중인 레플리카가 목록에서 사라졌다"
    assert launched.lifecycle.state is AgentState.UNHEALTHY
    assert not launched.lifecycle.accepts_tasks
    with pytest.raises(AaestroError) as caught:
        agents.route("echo")
    assert caught.value.code == "RT_009"

    # 정지가 재시도를 끊어야 한다 — 놔두면 정지한 자리에 컨테이너를 다시 세운다
    await agents.stop_all()
    assert not agents._restarts
    assert not agents.launched
    assert launched.lifecycle.state is AgentState.STOPPED


class GatedDockerClient(FakeDockerClient):
    """`create` 를 문 앞에 붙잡는 대역 — 교체가 레플리카를 목록에서 뺀 틈을 고정한다.

    실제 Docker 처럼 create 는 스레드에서 돈다: 기다리던 쪽이 취소돼도 스레드는 문이
    열리면 컨테이너를 끝까지 만든다.
    """

    def __init__(self) -> None:
        super().__init__()
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def create(self, **kwargs):
        if self.gate is not None:
            self.entered.set()
            self.gate.wait(GATE_TIMEOUT_S)
        return super().create(**kwargs)


GATE_TIMEOUT_S = 5.0
SECOND_CONTAINER = "container-0002" + "0" * 20


async def hold_a_replacement(agents: AgentLauncher, client: GatedDockerClient, launched) -> None:
    """재시작을 걸고, 교체가 새 컨테이너의 create 에 들어선 자리에서 멈춘다."""
    client.gate = threading.Event()
    launched.lifecycle.record_health(healthy=False, threshold=1)
    agents._schedule_restart(launched)
    await until(client.entered.is_set)
    assert ("echo", 0) not in agents.launched, (
        "교체가 레플리카를 목록에서 빼지 않았다 — 전제가 틀렸다"
    )


async def stop_while_held(client: GatedDockerClient, stopping) -> None:
    """정지를 걸어 둔 채 create 를 놓아 준다 — 정지는 create 가 끝나길 기다린다."""
    task = asyncio.create_task(stopping)
    await spin(5)
    assert client.gate is not None
    client.gate.set()
    await task


@pytest.mark.parametrize("stop_all", [False, True], ids=["stop", "stop_all"])
async def test_stopping_during_a_replacement_leaves_nothing_behind(stop_all: bool):
    """#482 — 교체가 목록에서 뺀 틈의 정지가 재시작을 놓치면, 정지한 자리에 컨테이너가 다시 선다."""
    client = GatedDockerClient()
    agents = launcher(client, Recorder(), health_interval_s=None)
    launched = await agents.start(manifest())
    await hold_a_replacement(agents, client, launched)

    await stop_while_held(client, agents.stop_all() if stop_all else agents.stop("echo"))
    await spin(20)

    assert not agents.launched, "정지 뒤 재시작이 레플리카를 다시 세웠다"
    assert not agents._restarts, "재시작 태스크가 소유자 없이 남았다"
    assert len(client.created) == 2, "취소된 교체가 만든 컨테이너가 있어야 한다 — 전제가 틀렸다"
    assert SECOND_CONTAINER in client.removed, "취소된 교체가 만든 컨테이너가 미아로 남았다"
    assert launched.lifecycle.state is AgentState.STOPPED


async def test_restart_reasons_exist_before_the_first_restart():
    """`increase()` 는 새로 생긴 계열의 첫 값을 증가로 보지 않는다 — 미리 0 으로 있어야 울린다."""
    registry = CollectorRegistry()
    metrics = Metrics(registry=registry)
    agents = launcher(FakeDockerClient(), Recorder(), metrics=metrics, health_interval_s=None)

    await agents.start(manifest())

    for reason in ("unhealthy", "failed", "gave_up"):
        sample = registry.get_sample_value(
            "aaestro_container_restarts_total", {"agent": "echo", "reason": reason}
        )
        assert sample == 0.0, f"{reason} 계열이 없다"
    await agents.stop_all()


async def test_a_stop_stuck_behind_a_hung_docker_call_still_times_out():
    """#482 — 끊은 태스크를 기다리며 바깥 취소까지 삼켜 `wait_for(stop)` 이 timeout 을 잃었다."""
    client = GatedDockerClient()
    agents = launcher(client, Recorder(), health_interval_s=None)
    launched = await agents.start(manifest())
    await hold_a_replacement(agents, client, launched)
    assert client.gate is not None

    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(agents.stop("echo"), STOP_DEADLINE_S)
        assert ("echo", 0) in agents._restarts, "끊다 만 재시작이 소유자를 잃었다"
    finally:
        client.gate.set()

    # 종료가 남은 재시작과 넘겨받은 컨테이너 정리를 끝까지 처리한다
    await agents.stop_all()
    assert not agents.launched
    assert not agents._restarts
    assert not agents.engine._cleanups
    assert SECOND_CONTAINER in client.removed, "걸렸던 기동이 만든 컨테이너가 미아로 남았다"


STOP_DEADLINE_S = 0.2


async def test_giving_up_logs_the_last_restart_failure(monkeypatch: pytest.MonkeyPatch):
    """상한에 닿기 직전 기동이 왜 실패했는지 포기 로그에 남아야 원인을 고친다."""
    import aaestro.runtime.launcher as launcher_module

    logged: list[tuple[str, dict]] = []

    class Capture:
        def __getattr__(self, level: str):
            return lambda event, **fields: logged.append((event, fields))

    monkeypatch.setattr(launcher_module, "log", Capture())
    client = FakeDockerClient()
    clock = FakeClock()
    agents = launcher(client, ClockedSleep(clock, client), health_interval_s=None)
    launched, _ = await start_sick(agents)
    launched.lifecycle.restart_policy = RestartPolicy(clock=clock)
    break_restarts(client)

    await restart_now(agents, launched)

    gave_up = [fields for event, fields in logged if event == "agent restart limit exceeded"]
    assert len(gave_up) == 1
    assert gave_up[0]["last_error_code"] == "RT_001"
    await agents.stop_all()
