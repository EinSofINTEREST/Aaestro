"""Periodic health monitoring for agent containers.

주기 health check 와 circuit breaker. 시간 판정은 전부 주입된 clock/sleep 을 쓴다 —
테스트가 실제로 자면 느려지고 비결정적이 된다 (06 Testing 2).
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

from aaestro.core.agent import HealthState
from aaestro.core.errors import CircuitBreaker, ErrorCategory, ErrorCode
from aaestro.observability.circuit import CircuitTelemetry
from aaestro.runtime.lifecycle import AgentState

if TYPE_CHECKING:
    from collections.abc import Callable

    from aaestro.core.agent import HealthStatus
    from aaestro.observability.metrics import Metrics
    from aaestro.runtime.lifecycle import AgentLifecycle

DEFAULT_INTERVAL_S = 10.0
DEFAULT_TIMEOUT_S = 3.0
DEFAULT_UNHEALTHY_THRESHOLD = 3
DEFAULT_STARTUP_GRACE_S = 45.0
"""기동 유예 — 이 시간 안의 실패는 재시작을 일으키지 않는다 (#297).

기동은 확인 주기보다 길 수 있다: MCP 서버는 서버마다 최대 15초, 원격 서버는 프록시를 거쳐
initialize 한다. 기본값은 배포의 ready 대기(60초)보다 짧게 둔다 — 유예가 그보다 길면 배포가 먼저
`RT_002` 로 끝나 유예가 의미를 잃는다.
"""

log = structlog.get_logger(__name__)


@runtime_checkable
class HealthProbe(Protocol):
    """Asks one agent for its health.

    에이전트 하나의 상태를 묻는 계약 — Control API 클라이언트가 이를 구현한다.
    """

    async def health(self) -> HealthStatus:
        """``/v1/health`` 를 호출한다."""
        ...


@dataclass
class HealthMonitor:
    """Polls one agent and feeds its lifecycle.

    에이전트 하나를 주기적으로 확인하고 결과를 lifecycle 에 넘긴다.
    연속 실패가 임계를 넘으면 Unhealthy 로 전이된다.
    """

    agent: str
    probe: HealthProbe
    lifecycle: AgentLifecycle
    interval_s: float = DEFAULT_INTERVAL_S
    timeout_s: float = DEFAULT_TIMEOUT_S
    unhealthy_threshold: int = DEFAULT_UNHEALTHY_THRESHOLD
    metrics: Metrics | None = None
    breaker: CircuitBreaker | None = None
    sleep: Callable[[float], object] | None = None
    startup_grace_s: float = DEFAULT_STARTUP_GRACE_S
    """Ready 가 되기 전의 유예 — 이 안의 실패는 세지만 Unhealthy 로 전이하지 않는다."""
    clock: Callable[[], float] = time.monotonic
    on_state: Callable[[AgentState, bool], None] | None = None
    """매 확인 뒤 ``(결과 상태, 이번 확인의 성공 여부)`` 를 받는 콜백 — 기동 성공
    판정처럼 **runtime 이** 내려야 하는 결정을 monitor 밖에 남긴다 (02 Lifecycle
    Rules 2). 상태만 넘기면 임계 미만의 실패도 STARTING 그대로라 성공과 구분되지
    않는다 — 실제 배포에서 첫 확인이 실패했는데 Ready 로 올라갔다 (#243)."""

    consecutive_failures: int = field(default=0, init=False)
    last_status: HealthState | None = field(default=None, init=False)
    ready_once: bool = False
    """한 번이라도 Ready 가 됐는가 — 그 뒤의 실패는 기동이 아니라 고장이다.

    재부착(`adopt`)은 이미 Ready 였던 기록만 붙이므로 참으로 시작한다 — 그렇지 않으면 control plane
    재시작이 떠 있던 에이전트에게 기동 유예를 다시 주게 된다."""
    started_at: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self.started_at = self.clock()
        if self.breaker is None:
            target = f"control:{self.agent}"
            observer = CircuitTelemetry(self.metrics, target=target) if self.metrics else None
            self.breaker = CircuitBreaker(
                target=target,
                open_category=ErrorCategory.RUNTIME,
                open_code=ErrorCode.RT_002,
                on_transition=observer.observe if observer else None,
            )

    async def check_once(self) -> AgentState:
        """Run one health check and record the outcome.

        한 번 확인하고 결과를 기록합니다. 회로가 열려 있으면 **호출하지 않고**
        실패로 간주합니다 — 응답 없는 에이전트를 계속 두드리면 runtime 이
        그 대기에 묶입니다.

        Returns:
            The lifecycle state after recording this result.
        """
        assert self.breaker is not None  # noqa: S101 — __post_init__ 가 보장

        # 기동 중에는 거절이 정상이다 — 그 실패로 회로를 열면 유예가 끝나는 순간 확인도 못 한 채
        # Unhealthy 가 된다. 회로는 **떠 있는** 에이전트를 두드리지 않기 위한 것이다 (#297 리뷰)
        starting_up = self.starting_up()
        if not starting_up and not self.breaker.can_attempt():
            return self._record(healthy=False, reason="circuit open")

        try:
            status = await asyncio.wait_for(self.probe.health(), timeout=self.timeout_s)
        except (TimeoutError, Exception) as err:  # noqa: B014 — TimeoutError 를 명시해 의도를 남긴다
            if not starting_up:
                self.breaker.record_failure()
            return self._record(healthy=False, reason=_reason(err))

        self.breaker.record_success()
        self.last_status = status.status
        # degraded 는 살아있는 상태다 — optional 자원이 빠졌을 뿐이라 재시작 대상이 아니다
        return self._record(healthy=status.status is not HealthState.UNHEALTHY)

    def _record(self, *, healthy: bool, reason: str | None = None) -> AgentState:
        """결과를 lifecycle 과 메트릭에 반영한다."""
        starting_up = self.starting_up()
        if healthy:
            self.consecutive_failures = 0
            self.ready_once = True
        else:
            self.consecutive_failures += 1
            log.warning(
                "agent health check failed",
                agent=self.agent,
                attempt=self.consecutive_failures,
                max_attempts=self.unhealthy_threshold,
                reason=reason,
                # 기동 유예 안의 실패는 재시작을 일으키지 않는다 — 로그로 구분한다 (#297)
                starting_up=starting_up,
            )

        state = self.lifecycle.record_health(
            healthy=healthy, threshold=self.unhealthy_threshold, starting_up=starting_up
        )
        if self.metrics is not None:
            self.metrics.gauge("aaestro_agent_health").labels(agent=self.agent).set(
                1 if healthy else 0
            )
        return state

    def starting_up(self) -> bool:
        """아직 기동 유예 안인가 — 기동 중이고, 한 번도 healthy 였던 적이 없고, 유예가 남았다."""
        if self.ready_once or self.lifecycle.state is not AgentState.STARTING:
            return False
        return self.clock() - self.started_at < self.startup_grace_s

    async def run(self, *, iterations: int | None = None) -> None:
        """Poll until stopped.

        정지될 때까지 주기적으로 확인합니다.

        Args:
            iterations: Stop after this many checks; None polls forever.
                테스트는 유한 횟수를 주어 루프를 끝냅니다.
        """
        sleeper = self.sleep or asyncio.sleep
        count = 0
        while iterations is None or count < iterations:
            state = await self.check_once()
            if self.on_state is not None:
                self.on_state(state, self.consecutive_failures == 0)
            count += 1
            if iterations is None or count < iterations:
                result = sleeper(self.interval_s)
                # Future/Task 를 건너뛰면 대기가 사라져 루프가 busy-run 한다
                if inspect.isawaitable(result):
                    await result


def _reason(err: BaseException) -> str:
    """실패 원인을 **기계 판독 가능한** 이름으로 남긴다.

    예외 클래스 이름만 남기면 `AaestroError` 가 전부 같은 글자로 뭉개져,
    도달 불가(`NET_001`)와 5xx(`NET_001` 아님)를 로그에서 구분할 수 없다 —
    05 의 사고 대응은 에러 코드 분포를 보고 원인을 가른다 (#217).
    """
    code = getattr(err, "code", None)
    return str(code) if code is not None else type(err).__name__


def track_running(metrics: Metrics, agent: str, *, running: bool) -> None:
    """Update the running-container gauge.

    실행 중 컨테이너 게이지를 갱신합니다.
    """
    metrics.gauge("aaestro_containers_running").labels(agent=agent).set(1 if running else 0)


__all__ = [
    "DEFAULT_INTERVAL_S",
    "DEFAULT_STARTUP_GRACE_S",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_UNHEALTHY_THRESHOLD",
    "HealthMonitor",
    "HealthProbe",
    "track_running",
]
