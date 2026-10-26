"""Lifecycle supervision in the launcher.

02 Lifecycle 의 상태 기계는 구현되어 있는데 **프로덕션에서 한 번도 돌지
않았다** — `AgentLifecycle` 을 만드는 곳이 테스트뿐이었다 (#213).

그래서 health 주기 확인도, `aaestro_agent_health` 도, 레플리카 라우팅이 보는
`accepts_tasks` 도 전부 죽어 있었다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml
from prometheus_client import CollectorRegistry

from aaestro.core.agent import ComponentHealth, HealthState, HealthStatus
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import AgentManifest
from aaestro.observability.metrics import Metrics
from aaestro.runtime.docker.engine import DockerEngine
from aaestro.runtime.launcher import AgentLauncher
from aaestro.runtime.lifecycle import AgentState
from tests.fixtures.fake_docker import FakeDockerClient
from tests.fixtures.waiting import spin, until

REPO_ROOT = Path(__file__).resolve().parents[3]


def manifest() -> AgentManifest:
    return AgentManifest.model_validate(
        yaml.safe_load((REPO_ROOT / "agents" / "echo" / "manifest.yaml").read_text("utf-8"))
    )


class StepSleep:
    """정해진 횟수만 즉시 통과시키고, 그 뒤로는 영원히 대기하는 sleep 대역.

    0초로 통과시키기만 하면 감시 루프가 **busy-loop** 이 되어 회차를 셀 수
    없다 — 실제로 자지 않으면서 결정적이려면 통과 횟수를 쥐고 있어야 한다.
    """

    def __init__(self, passes: int) -> None:
        self.left = passes
        self.waits: list[float] = []
        self._blocked = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.waits.append(delay)
        if self.left <= 0:
            await self._blocked.wait()  # 테스트가 끝날 때까지 멈춘다
            return
        self.left -= 1


class ScriptedHealth:
    """정해진 응답을 순서대로 돌려주는 Control API 대역."""

    def __init__(self, results: list[HealthStatus]) -> None:
        self._results = list(results)
        self.calls = 0
        self.drains: list[float | None] = []
        self.drain_error: Exception | None = None

    async def drain(self, *, timeout_s: float | None = None) -> None:
        self.drains.append(timeout_s)
        if self.drain_error is not None:
            raise self.drain_error

    async def health(self) -> HealthStatus:
        self.calls += 1
        if self._results:
            return self._results.pop(0)
        return HealthStatus(status=HealthState.HEALTHY)


def healthy() -> HealthStatus:
    return HealthStatus(status=HealthState.HEALTHY)


def sick() -> HealthStatus:
    return HealthStatus(status=HealthState.UNHEALTHY)


def degraded() -> HealthStatus:
    return HealthStatus.aggregate({"mcp:fs": ComponentHealth(state=HealthState.DEGRADED)})


def launcher(sleep: StepSleep, **kwargs) -> AgentLauncher:
    kwargs.setdefault("health_interval_s", 10.0)
    return AgentLauncher(
        engine=DockerEngine(client=FakeDockerClient()), health_sleep=sleep, **kwargs
    )


async def start_with(agents: AgentLauncher, results: list[HealthStatus]):
    """기동하고 **첫 확인 전에** probe 를 갈아끼운다.

    `start()` 는 감시 태스크를 만든 뒤 await 하지 않으므로, 돌아온 직후가
    유일하게 안전한 지점이다. 기본 클라이언트를 두면 실제 HTTP 를 시도한다.
    """
    launched = await agents.start(manifest())
    probe = ScriptedHealth(results)
    launched.client.health = probe.health  # type: ignore[method-assign]
    launched.client.drain = probe.drain  # type: ignore[method-assign]
    return launched, probe


# --- 상태 전이 ---------------------------------------------------------------


async def test_a_launched_agent_starts_out_starting():
    """02 Rule 2 — 기동만으로 Ready 가 아니다. health OK 가 있어야 한다."""
    agents = AgentLauncher(engine=DockerEngine(client=FakeDockerClient()))

    launched = await agents.start(manifest())

    assert launched.lifecycle.state is AgentState.STARTING
    assert not launched.lifecycle.accepts_tasks


async def test_the_first_healthy_check_promotes_to_ready():
    """#213 — 이 배선이 없어 멀쩡히 뜬 에이전트도 태스크를 받지 못했다."""
    agents = launcher(StepSleep(0))
    launched, _ = await start_with(agents, [healthy()])

    await until(lambda: launched.lifecycle.state is AgentState.READY)

    assert launched.lifecycle.state is AgentState.READY
    assert launched.lifecycle.accepts_tasks
    await agents.stop_all()


async def test_a_failed_first_check_does_not_promote():
    """#243 실 배포에서 드러났다 — 첫 확인이 NET_001 로 실패했는데 Ready 가 됐다.

    임계 미만의 실패는 상태를 STARTING 에 두므로, 상태만 보고 올리면 실패도
    성공으로 읽힌다. 확인 한 번 뒤 루프를 멈춰 두고 상태를 본다.
    """
    agents = launcher(StepSleep(0))
    launched, probe = await start_with(agents, [sick()])

    await until(lambda: probe.calls >= 1)
    await spin(3)

    assert launched.lifecycle.state is AgentState.STARTING
    assert not launched.lifecycle.accepts_tasks
    await agents.stop_all()


async def test_the_first_success_after_a_failure_promotes():
    agents = launcher(StepSleep(1))
    launched, probe = await start_with(agents, [sick(), healthy()])

    await until(lambda: launched.lifecycle.state is AgentState.READY)

    assert probe.calls == 2
    await agents.stop_all()


async def test_repeated_failures_mark_the_agent_unhealthy():
    """02 Rule 3 — 3회 연속 실패가 Unhealthy 다."""
    # 통과 3회 = 확인 4회 (첫 확인은 대기 전이다). 더 돌면 대역이 다시
    # healthy 를 돌려줘 상태가 되돌아간다
    agents = launcher(StepSleep(3))
    launched, _ = await start_with(agents, [healthy(), sick(), sick(), sick()])

    await until(lambda: launched.lifecycle.state is AgentState.UNHEALTHY)

    assert launched.lifecycle.state is AgentState.UNHEALTHY
    assert not launched.lifecycle.accepts_tasks
    await agents.stop_all()


async def test_a_degraded_agent_still_accepts_tasks():
    """degraded 는 살아있는 상태다 — optional 자원이 빠졌을 뿐이다."""
    agents = launcher(StepSleep(0))
    launched, _ = await start_with(agents, [degraded()])

    await until(lambda: launched.lifecycle.state is AgentState.READY)

    assert launched.lifecycle.state is AgentState.READY
    await agents.stop_all()


# --- 정지 = drain 후 stop (02 Lifecycle 4·5) ---------------------------------------


async def test_stopping_a_ready_agent_drains_before_the_container_stops():
    """#243 — launcher.stop 은 Draining 으로 **표시만** 하고 agentd 에 drain 을 청하지
    않았다. 진행 중 태스크는 SIGTERM 과 함께 사라졌다."""
    agents = launcher(StepSleep(0), drain_timeout_s=7.0)
    launched, probe = await start_with(agents, [healthy()])
    await until(lambda: launched.lifecycle.state is AgentState.READY)
    order: list[str] = []
    probe_drain = probe.drain

    async def drain(*, timeout_s=None):
        order.append("drain")
        await probe_drain(timeout_s=timeout_s)

    engine_stop = agents.engine.stop

    async def stop(handle, **kwargs) -> bool:
        order.append("stop")
        return await engine_stop(handle, **kwargs)

    launched.client.drain = drain  # type: ignore[method-assign]
    agents.engine.stop = stop  # type: ignore[method-assign]

    await agents.stop(manifest().name)

    assert order == ["drain", "stop"]
    assert probe.drains == [7.0]


async def test_a_drain_that_times_out_still_stops_the_container():
    agents = launcher(StepSleep(0))
    launched, probe = await start_with(agents, [healthy()])
    await until(lambda: launched.lifecycle.state is AgentState.READY)
    probe.drain_error = AaestroError(
        category=ErrorCategory.TIMEOUT, code=ErrorCode.TO_001, message="slow"
    )

    await agents.stop(manifest().name)

    assert probe.drains, "drain was never requested"
    assert launched.lifecycle.state is AgentState.STOPPED
    assert agents.launched == {}


async def test_an_unexpected_drain_error_still_stops_the_container():
    """AaestroError 만 잡으면 JSON/타입 오류 같은 예외가 정지 경로를 통째로 끊는다."""
    agents = launcher(StepSleep(0))
    launched, probe = await start_with(agents, [healthy()])
    await until(lambda: launched.lifecycle.state is AgentState.READY)
    probe.drain_error = ValueError("garbled drain response")

    await agents.stop(manifest().name)

    assert launched.lifecycle.state is AgentState.STOPPED
    assert agents.launched == {}


async def test_a_restart_keeps_the_a2a_port_and_its_reservation():
    """peer 들의 env 에 이 포트가 굳어 있다 — 재시작이 번호를 바꾸면 peer 가 못 찾는다."""
    from aaestro.runtime.ports import A2APortAllocator
    from aaestro.runtime.spec import A2A_PORT_ENV

    agents = launcher(StepSleep(0), ports=A2APortAllocator(port_range=(9100, 9105)))
    document = yaml.safe_load((REPO_ROOT / "agents" / "echo" / "manifest.yaml").read_text("utf-8"))
    document["spec"]["a2a"] = {"enabled": True}
    launched = await agents.start(AgentManifest.model_validate(document))
    probe = ScriptedHealth([healthy()])
    launched.client.health = probe.health  # type: ignore[method-assign]
    launched.client.drain = probe.drain  # type: ignore[method-assign]
    await until(lambda: launched.lifecycle.state is AgentState.READY)
    port = launched.a2a_port
    assert port is not None
    launched.lifecycle.transition(AgentState.UNHEALTHY)

    await agents._replace(launched)  # noqa: SLF001 — 재시작 경로

    replaced = agents.launched[(manifest().name, 0)]
    client = agents.engine.client
    assert replaced.a2a_port == port
    assert client.created[-1]["environment"][A2A_PORT_ENV] == str(port)
    assert agents.ports is not None and list(agents.ports.assigned.values()) == [port]
    await agents.stop_all()


async def test_a_starting_agent_is_stopped_without_a_drain():
    """되감기는 아직 Ready 가 아닌 컨테이너를 내린다 — 받은 태스크가 없으니 기다릴 것도 없다."""
    agents = launcher(StepSleep(0))
    launched, probe = await start_with(agents, [sick()])
    await until(lambda: probe.calls >= 1)

    await agents.stop(manifest().name)

    assert probe.drains == []
    assert launched.lifecycle.state is AgentState.STOPPED


# --- 메트릭 -----------------------------------------------------------------


async def test_the_health_gauge_is_filled():
    """05 의 `AgentDown` 알림은 이 게이지를 본다 — 채우지 않으면 침묵한다."""
    metrics = Metrics(registry=CollectorRegistry())
    agents = launcher(StepSleep(0), metrics=metrics)
    await start_with(agents, [healthy()])
    gauge = metrics.gauge("aaestro_agent_health").labels(agent="echo")

    await until(lambda: gauge._value.get() == 1.0)

    assert gauge._value.get() == 1.0
    await agents.stop_all()


# --- 정지 -------------------------------------------------------------------


async def test_stopping_moves_the_lifecycle_to_stopped():
    agents = launcher(StepSleep(0))
    launched, _ = await start_with(agents, [healthy()])
    await until(lambda: launched.lifecycle.state is AgentState.READY)

    await agents.stop("echo")

    assert launched.lifecycle.state is AgentState.STOPPED


async def test_stopping_cancels_the_health_loop():
    """정지한 컨테이너를 계속 두드리면 runtime 이 그 대기에 묶인다."""
    agents = launcher(StepSleep(2))
    _, probe = await start_with(agents, [healthy()] * 50)
    await until(lambda: probe.calls > 0)
    seen = probe.calls

    await agents.stop("echo")
    await spin(50)

    assert probe.calls == seen
    assert not agents._monitors


# --- 감시를 켜지 않은 배선 -----------------------------------------------------


async def test_supervision_is_off_by_default():
    """루프의 소유자가 분명해야 한다 — 켜지 않은 조립은 태스크를 만들지 않는다."""
    agents = AgentLauncher(engine=DockerEngine(client=FakeDockerClient()))

    await agents.start(manifest())

    assert not agents._monitors


async def test_the_loop_uses_the_injected_wait():
    """06 Async 2 — 주입한 대기가 쓰여야 테스트가 초 단위로 늘어지지 않는다."""
    stepper = StepSleep(0)
    agents = launcher(stepper)
    await start_with(agents, [healthy()])

    await until(lambda: bool(stepper.waits))

    assert stepper.waits and all(delay == 10.0 for delay in stepper.waits)
    await agents.stop_all()
