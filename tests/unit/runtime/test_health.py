"""Unit tests for periodic health monitoring.

시간 판정은 전부 주입 — 실제로 자면 느려지고 비결정적이 된다 (06 Testing 2).
"""

from __future__ import annotations

import asyncio

import pytest

from aaestro.core.agent import ComponentHealth, HealthState, HealthStatus
from aaestro.core.errors import (
    AaestroError,
    CircuitBreaker,
    CircuitState,
    ErrorCategory,
    ErrorCode,
)
from aaestro.observability.metrics import Metrics
from aaestro.runtime.health import HealthMonitor, HealthProbe, track_running
from aaestro.runtime.lifecycle import AgentLifecycle, AgentState


class FakeProbe:
    """상태 응답을 스크립트하는 probe 대역."""

    def __init__(self, results: list[HealthStatus | Exception] | None = None) -> None:
        self._results = list(results or [])
        self.calls = 0

    async def health(self) -> HealthStatus:
        self.calls += 1
        if not self._results:
            return HealthStatus(status=HealthState.HEALTHY)
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def healthy() -> HealthStatus:
    return HealthStatus(status=HealthState.HEALTHY)


def unhealthy() -> HealthStatus:
    return HealthStatus(status=HealthState.UNHEALTHY)


def degraded() -> HealthStatus:
    return HealthStatus.aggregate({"mcp:fs": ComponentHealth(state=HealthState.DEGRADED)})


def make_monitor(probe: FakeProbe | None = None, **overrides) -> HealthMonitor:
    lifecycle = overrides.pop("lifecycle", None) or ready_lifecycle()
    return HealthMonitor(
        agent="researcher",
        probe=probe or FakeProbe(),
        lifecycle=lifecycle,
        **overrides,
    )


def ready_lifecycle() -> AgentLifecycle:
    """Ready 상태까지 전이시킨 lifecycle."""
    lifecycle = AgentLifecycle(agent="researcher")
    lifecycle.transition(AgentState.BUILT)
    lifecycle.transition(AgentState.STARTING)
    lifecycle.transition(AgentState.READY)
    return lifecycle


# --- 계약 --------------------------------------------------------------------


def test_fake_probe_satisfies_the_contract():
    assert isinstance(FakeProbe(), HealthProbe)


# --- 단일 확인 ----------------------------------------------------------------


async def test_healthy_check_keeps_the_agent_ready():
    monitor = make_monitor(FakeProbe([healthy()]))

    state = await monitor.check_once()

    assert state is AgentState.READY
    assert monitor.consecutive_failures == 0


async def test_degraded_is_not_treated_as_failure():
    """degraded 는 살아있는 상태다 — optional 자원이 빠졌을 뿐 재시작 대상이 아니다."""
    monitor = make_monitor(FakeProbe([degraded()]))

    state = await monitor.check_once()

    assert state is AgentState.READY
    assert monitor.consecutive_failures == 0


async def test_unhealthy_response_counts_as_failure():
    monitor = make_monitor(FakeProbe([unhealthy()]))

    await monitor.check_once()

    assert monitor.consecutive_failures == 1


async def test_probe_exception_counts_as_failure():
    monitor = make_monitor(FakeProbe([ConnectionError("refused")]))

    await monitor.check_once()

    assert monitor.consecutive_failures == 1


async def test_probe_timeout_counts_as_failure():
    class Slow:
        async def health(self):
            await asyncio.sleep(5)
            return healthy()

    monitor = make_monitor(lifecycle=ready_lifecycle(), timeout_s=0.01)
    monitor.probe = Slow()

    await monitor.check_once()

    assert monitor.consecutive_failures == 1


# --- 임계 전이 ----------------------------------------------------------------


async def test_threshold_failures_transition_to_unhealthy():
    """3회 연속 실패해야 Unhealthy — 한 번의 순간적 실패로 재시작하지 않는다."""
    monitor = make_monitor(FakeProbe([unhealthy(), unhealthy(), unhealthy()]))

    states = [await monitor.check_once() for _ in range(3)]

    assert states[0] is AgentState.READY
    assert states[1] is AgentState.READY
    assert states[2] is AgentState.UNHEALTHY


async def test_an_unreachable_agent_reaches_unhealthy_in_the_declared_rounds():
    """#217 — 확인 안에서 또 재시도하면 이 회차가 성립하지 않는다.

    `NETWORK_RETRY` 를 health 안에서 돌리면 한 번의 확인이 backoff 만으로
    3초를 넘겨 monitor 의 timeout 에 잘렸다. 02 가 규정한 "3회 연속 실패"가
    실제로는 9회 시도 + 9초가 되고, 그 사이 아픈 에이전트는 방치된다.
    """
    refused = AaestroError(
        category=ErrorCategory.NETWORK, code=ErrorCode.NET_001, message="connection refused"
    )
    probe = FakeProbe([refused, refused, refused])
    monitor = make_monitor(probe)

    states = [await monitor.check_once() for _ in range(3)]

    assert states[-1] is AgentState.UNHEALTHY
    assert probe.calls == 3, "확인 한 번은 시도 한 번이다 — 안에서 재시도하면 이중이다"


async def test_the_failure_reason_is_the_error_code():
    """05 의 사고 대응은 **에러 코드 분포**로 원인을 가른다 (#217).

    예외 클래스 이름만 남기면 `AaestroError` 가 전부 같은 글자로 뭉개져,
    도달 불가와 5xx 를 로그에서 구분할 수 없다.
    """
    recorded: list[str | None] = []
    monitor = make_monitor(
        FakeProbe(
            [
                AaestroError(
                    category=ErrorCategory.NETWORK,
                    code=ErrorCode.NET_001,
                    message="connection refused",
                )
            ]
        )
    )
    original = monitor._record

    def spy(*, healthy: bool, reason: str | None = None):
        recorded.append(reason)
        return original(healthy=healthy, reason=reason)

    monitor._record = spy  # type: ignore[method-assign]

    await monitor.check_once()

    assert recorded == [str(ErrorCode.NET_001)]


async def test_recovery_resets_the_failure_streak():
    monitor = make_monitor(FakeProbe([unhealthy(), unhealthy(), healthy()]))

    for _ in range(3):
        await monitor.check_once()

    assert monitor.consecutive_failures == 0


# --- circuit breaker ----------------------------------------------------------


async def test_open_circuit_skips_the_probe():
    """응답 없는 에이전트를 계속 두드리면 runtime 이 그 대기에 묶인다."""
    probe = FakeProbe([ConnectionError("gone") for _ in range(10)])
    breaker = CircuitBreaker(
        max_failures=2,
        target="control:researcher",
        open_category=ErrorCategory.RUNTIME,
        open_code=ErrorCode.RT_002,
    )
    monitor = make_monitor(probe, breaker=breaker)

    for _ in range(2):
        await monitor.check_once()
    calls_before = probe.calls

    await monitor.check_once()

    assert probe.calls == calls_before  # 호출하지 않았다
    assert monitor.consecutive_failures == 3  # 그래도 실패로 센다


async def test_success_closes_the_circuit():
    probe = FakeProbe([ConnectionError("x"), healthy()])
    breaker = CircuitBreaker(
        max_failures=5,
        target="control:researcher",
        open_category=ErrorCategory.RUNTIME,
        open_code=ErrorCode.RT_002,
    )
    monitor = make_monitor(probe, breaker=breaker)

    await monitor.check_once()
    await monitor.check_once()

    assert breaker.can_attempt() is True


# --- 주기 실행 ----------------------------------------------------------------


async def test_run_polls_the_requested_number_of_times():
    probe = FakeProbe([healthy() for _ in range(3)])
    slept: list[float] = []

    async def sleep(delay: float) -> None:
        slept.append(delay)

    monitor = make_monitor(probe, interval_s=10.0, sleep=sleep)

    await monitor.run(iterations=3)

    assert probe.calls == 3
    # 마지막 확인 뒤에는 기다리지 않는다
    assert slept == [10.0, 10.0]


async def test_run_uses_the_declared_interval():
    slept: list[float] = []

    async def sleep(delay: float) -> None:
        slept.append(delay)

    monitor = make_monitor(interval_s=7.5, sleep=sleep)

    await monitor.run(iterations=2)

    assert slept == [7.5]


# --- 메트릭 -------------------------------------------------------------------


async def test_health_metric_tracks_the_result():
    metrics = Metrics()
    monitor = make_monitor(FakeProbe([healthy()]), metrics=metrics)

    await monitor.check_once()

    gauge = metrics.gauge("aaestro_agent_health")
    assert gauge.labels(agent="researcher")._value.get() == 1


async def test_health_metric_drops_on_failure():
    metrics = Metrics()
    monitor = make_monitor(FakeProbe([unhealthy()]), metrics=metrics)

    await monitor.check_once()

    gauge = metrics.gauge("aaestro_agent_health")
    assert gauge.labels(agent="researcher")._value.get() == 0


def test_running_gauge_tracks_containers():
    metrics = Metrics()

    track_running(metrics, "researcher", running=True)

    gauge = metrics.gauge("aaestro_containers_running")
    assert gauge.labels(agent="researcher")._value.get() == 1


def test_gauge_lookup_rejects_a_non_gauge():
    metrics = Metrics()

    with pytest.raises(TypeError, match="not a gauge"):
        metrics.gauge("aaestro_agent_tasks_total")


async def test_run_awaits_a_future_returning_sleep():
    """Future 를 반환하는 sleep 을 건너뛰면 대기가 사라져 루프가 busy-run 한다."""
    awaited: list[float] = []

    def sleep(delay: float):
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        future.set_result(None)
        awaited.append(delay)
        return future

    monitor = make_monitor(interval_s=5.0, sleep=sleep)

    await monitor.run(iterations=2)

    assert awaited == [5.0]


# --- 기동 유예 (#297) ------------------------------------------------------------


class Clock:
    """주입 시계 — 06 은 시간 의존 로직이 실제로 자는 것을 금지한다."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def starting_lifecycle() -> AgentLifecycle:
    lifecycle = AgentLifecycle(agent="researcher")
    lifecycle.transition(AgentState.BUILT)
    lifecycle.transition(AgentState.STARTING)
    return lifecycle


async def test_failures_inside_the_startup_grace_do_not_mark_the_agent_unhealthy():
    """기동이 확인 주기보다 길 수 있다 — 유예 안의 실패로 재시작하면 같은 시간이 다시 걸린다."""
    clock = Clock()
    lifecycle = starting_lifecycle()
    monitor = make_monitor(
        FakeProbe([unhealthy(), unhealthy(), unhealthy(), unhealthy()]),
        lifecycle=lifecycle,
        startup_grace_s=30.0,
        clock=clock,
    )

    for _ in range(4):
        clock.now += 5.0
        state = await monitor.check_once()

    assert state is AgentState.STARTING, "유예 안에서 Unhealthy 로 전이했다"
    assert monitor.consecutive_failures == 4, "실패를 세지 않으면 유예가 끝난 뒤를 판단할 수 없다"


async def test_the_grace_ends_and_the_usual_threshold_applies():
    clock = Clock()
    monitor = make_monitor(
        FakeProbe([unhealthy()] * 6),
        lifecycle=starting_lifecycle(),
        startup_grace_s=30.0,
        clock=clock,
    )

    for _ in range(3):
        clock.now += 5.0
        assert await monitor.check_once() is AgentState.STARTING

    clock.now += 30.0  # 유예를 넘긴다 — 이제 뜨는 중이 아니라 뜨지 못한 것이다
    assert await monitor.check_once() is AgentState.UNHEALTHY


async def test_after_the_first_healthy_check_the_grace_is_over():
    """한 번 Ready 가 된 뒤의 실패는 기동이 아니라 고장이다 — 유예 시간이 남아도 재시작한다."""
    clock = Clock()
    monitor = make_monitor(
        FakeProbe([healthy(), unhealthy(), unhealthy(), unhealthy()]),
        lifecycle=starting_lifecycle(),
        startup_grace_s=1_000.0,
        clock=clock,
    )

    assert await monitor.check_once() is AgentState.STARTING
    monitor.lifecycle.transition(AgentState.READY)
    for _ in range(3):
        state = await monitor.check_once()

    assert state is AgentState.UNHEALTHY
    assert monitor.starting_up() is False


async def test_no_grace_keeps_the_previous_behaviour():
    monitor = make_monitor(
        FakeProbe([unhealthy()] * 3), lifecycle=starting_lifecycle(), startup_grace_s=0.0
    )

    for _ in range(3):
        state = await monitor.check_once()

    assert state is AgentState.UNHEALTHY


async def test_startup_failures_do_not_open_the_control_api_circuit():
    """유예 안에서 회로가 열리면, 유예가 끝나는 순간 확인도 못 하고 Unhealthy 다 (#297 리뷰)."""
    clock = Clock()
    breaker = CircuitBreaker(
        max_failures=2,
        target="control:researcher",
        open_category=ErrorCategory.RUNTIME,
        open_code=ErrorCode.RT_002,
    )
    probe = FakeProbe([ConnectionError("starting") for _ in range(4)] + [healthy()])
    monitor = make_monitor(
        probe,
        lifecycle=starting_lifecycle(),
        startup_grace_s=30.0,
        clock=clock,
        breaker=breaker,
    )

    for _ in range(4):
        clock.now += 2.0
        assert await monitor.check_once() is AgentState.STARTING

    assert breaker.state is CircuitState.CLOSED, "기동 중 거절로 회로가 열렸다"
    # 늦게 뜬 에이전트를 실제로 확인한다 — 회로가 열렸다면 두드리지도 않았을 것이다
    clock.now += 2.0
    assert await monitor.check_once() is AgentState.STARTING
    assert probe.calls == 5
    assert monitor.ready_once is True


async def test_a_reattached_agent_gets_no_startup_grace():
    """이미 Ready 였던 기록만 붙인다 — 유예를 다시 주면 고장이 임계만큼 늦게 드러난다."""
    monitor = make_monitor(
        FakeProbe([unhealthy()] * 3),
        lifecycle=starting_lifecycle(),
        startup_grace_s=1_000.0,
        ready_once=True,
    )

    for _ in range(3):
        state = await monitor.check_once()

    assert state is AgentState.UNHEALTHY
    assert monitor.starting_up() is False
