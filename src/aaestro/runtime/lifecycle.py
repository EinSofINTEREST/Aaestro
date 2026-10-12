"""Agent lifecycle state machine and restart policy.

에이전트 lifecycle. 상태 전이와 재시작 정책을 Docker 와 분리해 순수 로직으로
두어, 컨테이너 없이 전이 규칙을 검증할 수 있게 한다.

시간 의존 동작(backoff, 재시작 임계)은 주입된 clock 으로만 판단한다 —
테스트가 실제로 기다리지 않아야 하기 때문이다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_DRAIN_TIMEOUT_S: Final = 30.0
DEFAULT_STOP_GRACE_S: Final = 30.0

RESTART_INITIAL_DELAY_S: Final = 1.0
RESTART_MAX_DELAY_S: Final = 60.0
RESTART_MULTIPLIER: Final = 2.0
# 10분 내 5회를 넘기면 재시도를 멈추고 Failed 로 전환한다 (crash loop 차단)
RESTART_WINDOW_S: Final = 600.0
RESTART_MAX_IN_WINDOW: Final = 5


class AgentState(StrEnum):
    """에이전트 lifecycle 상태 (02 Lifecycle States)."""

    DECLARED = "declared"
    BUILT = "built"
    STARTING = "starting"
    READY = "ready"
    UNHEALTHY = "unhealthy"
    DRAINING = "draining"
    STOPPED = "stopped"
    FAILED = "failed"


# 허용된 전이만 명시 — 그 외는 프로그래밍 오류로 즉시 드러낸다
_ALLOWED: Final[dict[AgentState, frozenset[AgentState]]] = {
    AgentState.DECLARED: frozenset({AgentState.BUILT, AgentState.FAILED}),
    AgentState.BUILT: frozenset({AgentState.STARTING, AgentState.FAILED}),
    AgentState.STARTING: frozenset(
        {AgentState.READY, AgentState.UNHEALTHY, AgentState.FAILED, AgentState.STOPPED}
    ),
    AgentState.READY: frozenset({AgentState.UNHEALTHY, AgentState.DRAINING, AgentState.STOPPED}),
    AgentState.UNHEALTHY: frozenset(
        {AgentState.STARTING, AgentState.READY, AgentState.FAILED, AgentState.STOPPED}
    ),
    AgentState.DRAINING: frozenset({AgentState.STOPPED, AgentState.FAILED}),
    AgentState.STOPPED: frozenset({AgentState.STARTING}),
    # 재배포(Starting)뿐 아니라 **정리(Stopped)** 도 되어야 한다 — 실패한
    # 에이전트를 멈출 수 없으면 그 컨테이너가 샌다
    AgentState.FAILED: frozenset({AgentState.STARTING, AgentState.STOPPED}),
}

TERMINAL_STATES: Final = frozenset({AgentState.STOPPED, AgentState.FAILED})


def _monotonic() -> float:
    """단조 시계 — 기본 clock. 테스트는 주입으로 대체한다."""
    return time.monotonic()


@dataclass
class RestartPolicy:
    """Exponential backoff restart policy with a crash-loop ceiling.

    지수 백오프 재시작 정책. 창(window) 안의 재시작 횟수가 상한을 넘으면
    더 시도하지 않는다 — 무한 재시작은 리소스만 태운다.
    """

    initial_delay_s: float = RESTART_INITIAL_DELAY_S
    max_delay_s: float = RESTART_MAX_DELAY_S
    multiplier: float = RESTART_MULTIPLIER
    window_s: float = RESTART_WINDOW_S
    max_in_window: int = RESTART_MAX_IN_WINDOW
    clock: Callable[[], float] = field(default_factory=lambda: _monotonic)

    _restarts: list[float] = field(default_factory=list, init=False)

    def delay_for(self, attempt: int) -> float:
        """Compute the backoff delay for a 1-based restart attempt.

        1-based 재시작 횟수에 대한 대기 시간(초)을 계산합니다.
        """
        if attempt < 1:
            raise ValueError("attempt must be >= 1")
        delay = self.initial_delay_s * (self.multiplier ** (attempt - 1))
        return min(delay, self.max_delay_s)

    def record_restart(self) -> None:
        """재시작을 기록한다 — 창 밖의 오래된 기록은 버린다."""
        now = self.clock()
        self._restarts = [t for t in self._restarts if now - t < self.window_s]
        self._restarts.append(now)

    def restarts_in_window(self) -> int:
        """현재 창 안의 재시작 횟수."""
        now = self.clock()
        return len([t for t in self._restarts if now - t < self.window_s])

    def should_give_up(self) -> bool:
        """상한을 넘겨 재시작을 포기해야 하는지."""
        return self.restarts_in_window() > self.max_in_window


@dataclass
class AgentLifecycle:
    """Tracks one agent's lifecycle state.

    에이전트 하나의 lifecycle 상태를 추적한다.
    """

    agent: str
    state: AgentState = AgentState.DECLARED
    restart_policy: RestartPolicy = field(default_factory=RestartPolicy)
    consecutive_health_failures: int = 0

    def transition(self, target: AgentState) -> AgentState:
        """Move to a new state, rejecting illegal transitions.

        상태를 전이합니다. 허용되지 않은 전이는 즉시 거부됩니다 —
        조용히 통과시키면 lifecycle 버그가 런타임 깊은 곳에서 드러납니다.

        Args:
            target: The state to move to.

        Returns:
            The new state.

        Raises:
            AaestroError: RUNTIME/``RT_002`` for an illegal transition.
        """
        if target == self.state:
            return self.state
        if target not in _ALLOWED[self.state]:
            raise AaestroError(
                category=ErrorCategory.RUNTIME,
                code=ErrorCode.RT_007,
                message=f"illegal lifecycle transition: {self.state} -> {target}",
                agent=self.agent,
                details={"from": str(self.state), "to": str(target)},
            )
        self.state = target
        return self.state

    @property
    def is_terminal(self) -> bool:
        """종료 상태인지."""
        return self.state in TERMINAL_STATES

    @property
    def accepts_tasks(self) -> bool:
        """새 태스크를 받을 수 있는 상태인지 — drain 중에는 받지 않는다."""
        return self.state is AgentState.READY

    def record_health(
        self, *, healthy: bool, threshold: int = 3, starting_up: bool = False
    ) -> AgentState:
        """Fold a health check result into the lifecycle.

        Health 결과를 반영합니다. 연속 실패가 임계에 닿으면 Unhealthy 로 전이하고,
        성공하면 카운터를 리셋합니다.

        Args:
            healthy: Whether the check passed.
            threshold: Consecutive failures before marking unhealthy.
            starting_up: 아직 기동 유예 안이다 — 실패를 세되 Unhealthy 로 전이하지 않는다.
                기동이 확인 주기보다 길면(MCP 서버 기동, 원격 initialize) 뜨는 중인 컨테이너가
                재시작돼 같은 시간이 다시 걸린다 (#297).

        Returns:
            The resulting state.
        """
        if healthy:
            self.consecutive_health_failures = 0
            if self.state is AgentState.UNHEALTHY:
                self.transition(AgentState.READY)
            return self.state

        self.consecutive_health_failures += 1
        if starting_up and self.state is AgentState.STARTING:
            # 유예가 끝나면 아래 규칙이 그대로 적용된다 — 영원히 기다리지 않는다
            return self.state
        # 기동 중(STARTING) 실패도 Unhealthy 로 — READY 만 보면 initialize 가
        # 끝내 성공하지 못한 컨테이너가 계속 STARTING 에 머문다
        if self.consecutive_health_failures >= threshold and self.state in (
            AgentState.READY,
            AgentState.STARTING,
        ):
            self.transition(AgentState.UNHEALTHY)
        return self.state

    def plan_restart(self) -> float:
        """Record a restart and return how long to wait first.

        재시작을 기록하고 대기 시간을 반환합니다.

        Returns:
            Backoff delay in seconds.

        Raises:
            AaestroError: RUNTIME/``RT_002`` when the crash-loop ceiling is hit;
                the agent is moved to ``FAILED``.
        """
        self.restart_policy.record_restart()
        if self.restart_policy.should_give_up():
            self.state = AgentState.FAILED
            raise AaestroError(
                category=ErrorCategory.RUNTIME,
                code=ErrorCode.RT_008,
                message="restart limit exceeded — agent marked failed",
                agent=self.agent,
                details={
                    "restarts": self.restart_policy.restarts_in_window(),
                    "max_in_window": self.restart_policy.max_in_window,
                },
            )
        return self.restart_policy.delay_for(self.restart_policy.restarts_in_window())
