"""Unit tests for the agent lifecycle state machine and restart policy.

시간 의존 동작은 전부 주입된 fake clock 으로 검증한다 — 실제 대기 금지.
"""

from __future__ import annotations

import pytest

from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.runtime.lifecycle import (
    AgentLifecycle,
    AgentState,
    RestartPolicy,
)


class FakeClock:
    """수동으로 흐르는 시계."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def ready_agent(name: str = "researcher", clock: FakeClock | None = None) -> AgentLifecycle:
    """Ready 상태까지 정상 전이시킨 에이전트."""
    agent = AgentLifecycle(agent=name, restart_policy=RestartPolicy(clock=clock or FakeClock()))
    agent.transition(AgentState.BUILT)
    agent.transition(AgentState.STARTING)
    agent.transition(AgentState.READY)
    return agent


# --- 상태 전이 --------------------------------------------------------------


def test_happy_path_reaches_ready():
    assert ready_agent().state is AgentState.READY


def test_full_lifecycle_to_stopped():
    agent = ready_agent()

    agent.transition(AgentState.DRAINING)
    agent.transition(AgentState.STOPPED)

    assert agent.is_terminal is True


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (AgentState.DECLARED, AgentState.READY),  # 빌드를 건너뛸 수 없다
        (AgentState.DECLARED, AgentState.STARTING),
        (AgentState.READY, AgentState.BUILT),  # 되돌아갈 수 없다
        (AgentState.DRAINING, AgentState.READY),  # drain 은 취소되지 않는다
        (AgentState.STOPPED, AgentState.READY),
    ],
)
def test_illegal_transitions_are_rejected(start, target):
    """조용히 통과시키면 lifecycle 버그가 런타임 깊은 곳에서 드러난다."""
    agent = AgentLifecycle(agent="a", state=start)

    with pytest.raises(AaestroError) as exc_info:
        agent.transition(target)

    assert exc_info.value.code == "RT_007"
    assert exc_info.value.category is ErrorCategory.RUNTIME
    assert agent.state is start  # 실패해도 상태는 유지된다


def test_transition_to_the_same_state_is_a_noop():
    agent = ready_agent()

    assert agent.transition(AgentState.READY) is AgentState.READY


def test_stopped_agent_can_be_restarted():
    agent = ready_agent()
    agent.transition(AgentState.STOPPED)

    assert agent.transition(AgentState.STARTING) is AgentState.STARTING


def test_failed_agent_can_be_redeployed():
    agent = AgentLifecycle(agent="a", state=AgentState.FAILED)

    assert agent.transition(AgentState.STARTING) is AgentState.STARTING


def test_only_ready_accepts_tasks():
    """drain 중에는 새 태스크를 받지 않는다."""
    agent = ready_agent()
    assert agent.accepts_tasks is True

    agent.transition(AgentState.DRAINING)
    assert agent.accepts_tasks is False


# --- health 반영 ------------------------------------------------------------


def test_unhealthy_after_three_consecutive_failures():
    agent = ready_agent()

    for _ in range(2):
        agent.record_health(healthy=False)
    assert agent.state is AgentState.READY  # 임계 미달

    agent.record_health(healthy=False)
    assert agent.state is AgentState.UNHEALTHY


def test_success_resets_the_failure_streak():
    agent = ready_agent()
    agent.record_health(healthy=False)
    agent.record_health(healthy=False)

    agent.record_health(healthy=True)
    agent.record_health(healthy=False)

    assert agent.state is AgentState.READY
    assert agent.consecutive_health_failures == 1


def test_recovery_returns_to_ready():
    agent = ready_agent()
    for _ in range(3):
        agent.record_health(healthy=False)

    agent.record_health(healthy=True)

    assert agent.state is AgentState.READY


def test_custom_threshold_is_honored():
    agent = ready_agent()

    agent.record_health(healthy=False, threshold=1)

    assert agent.state is AgentState.UNHEALTHY


def test_health_does_not_disturb_draining():
    """drain 중 health 실패가 상태를 되돌리면 안 된다."""
    agent = ready_agent()
    agent.transition(AgentState.DRAINING)

    for _ in range(5):
        agent.record_health(healthy=False)

    assert agent.state is AgentState.DRAINING


# --- 재시작 정책 ------------------------------------------------------------


def test_backoff_grows_and_clamps():
    policy = RestartPolicy(clock=FakeClock())

    assert [policy.delay_for(a) for a in range(1, 8)] == [1, 2, 4, 8, 16, 32, 60]


def test_delay_rejects_zero_attempt():
    with pytest.raises(ValueError, match="attempt must be >= 1"):
        RestartPolicy().delay_for(0)


def test_restarts_outside_the_window_are_forgotten():
    """오래된 재시작이 영원히 카운트되면 멀쩡한 에이전트가 Failed 로 간다."""
    clock = FakeClock()
    policy = RestartPolicy(clock=clock)

    for _ in range(5):
        policy.record_restart()
    assert policy.restarts_in_window() == 5

    clock.advance(601)
    assert policy.restarts_in_window() == 0


def test_plan_restart_returns_growing_delays():
    agent = ready_agent(clock=FakeClock())

    assert [agent.plan_restart() for _ in range(3)] == [1, 2, 4]


def test_crash_loop_marks_the_agent_failed():
    """10분 내 5회를 넘기면 재시도를 멈춘다."""
    agent = ready_agent(clock=FakeClock())
    for _ in range(5):
        agent.plan_restart()

    with pytest.raises(AaestroError) as exc_info:
        agent.plan_restart()

    assert exc_info.value.code == "RT_008"
    assert "restart limit exceeded" in exc_info.value.message
    assert agent.state is AgentState.FAILED


def test_restarts_spread_over_time_do_not_trip_the_ceiling():
    clock = FakeClock()
    agent = ready_agent(clock=clock)

    for _ in range(10):
        agent.plan_restart()
        clock.advance(601)  # 창 밖으로 밀어낸다

    assert agent.state is not AgentState.FAILED


def test_lifecycle_errors_use_distinct_codes():
    """RT_002 는 "컨테이너 unhealthy" 다 — lifecycle 위반과 섞으면 라우팅이 흐려진다."""
    illegal = AgentLifecycle(agent="a", state=AgentState.DECLARED)
    with pytest.raises(AaestroError) as transition_err:
        illegal.transition(AgentState.READY)

    looping = ready_agent(clock=FakeClock())
    for _ in range(5):
        looping.plan_restart()
    with pytest.raises(AaestroError) as restart_err:
        looping.plan_restart()

    codes = {transition_err.value.code, restart_err.value.code}
    assert codes == {"RT_007", "RT_008"}


def test_failing_startup_health_becomes_unhealthy():
    """initialize 가 끝내 성공하지 못하면 STARTING 에 머물지 않아야 한다."""
    agent = AgentLifecycle(agent="a", state=AgentState.DECLARED)
    agent.transition(AgentState.BUILT)
    agent.transition(AgentState.STARTING)

    for _ in range(3):
        agent.record_health(healthy=False)

    assert agent.state is AgentState.UNHEALTHY


def test_startup_health_success_leaves_state_for_the_caller():
    """기동 성공 판정은 runtime 이 한다 — health 성공만으로 READY 로 올리지 않는다."""
    agent = AgentLifecycle(agent="a", state=AgentState.DECLARED)
    agent.transition(AgentState.BUILT)
    agent.transition(AgentState.STARTING)

    agent.record_health(healthy=True)

    assert agent.state is AgentState.STARTING


def test_failed_agent_can_be_cleaned_up():
    """멈출 수 없으면 그 컨테이너가 샌다 — 재배포만이 유일한 출구일 수 없다."""
    agent = AgentLifecycle(agent="a", state=AgentState.FAILED)

    assert agent.transition(AgentState.STOPPED) is AgentState.STOPPED
