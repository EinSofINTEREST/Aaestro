"""Decision call client — retry, circuit breaker, deadline and metrics around a provider.

provider 어댑터 (단일 시도) 를 감싸 결정 호출의 유일한 재시도 계층이 된다 (05 Retry Layering):
``DECISION_RETRY`` 로 한 번 다시 두드리고, 전체를 ``timeout_s`` 안에 끝내며 (넘으면 ``TO_004``),
circuit 이 열려 있으면 HTTP 없이 실패한다. 실패는 ``AaestroError`` 로 올리고 쓰임이 그것을
``unavailable`` 로 읽는다 — 이 클라이언트는 그 사유를
``aaestro_decision_unavailable_total`` 로 센다.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import structlog

from aaestro.core.errors import (
    DECISION_RETRY,
    AaestroError,
    CircuitBreaker,
    ErrorCategory,
    ErrorCode,
)
from aaestro.observability.circuit import CircuitTelemetry
from aaestro.observability.logging import LogField
from aaestro.resilience import retrying

if TYPE_CHECKING:
    import random
    from collections.abc import Awaitable, Callable, Sequence

    from aaestro.core.decision import Decision, Question
    from aaestro.decision.providers.registry import DecisionProvider
    from aaestro.observability.metrics import Metrics

log = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_S: Final = 5.0
"""``decision.timeout_s`` 기본값 (01 Configuration) — 재시도 대기까지 포함한 전체 상한."""

STATUS_COMPLETED: Final = "completed"
STATUS_FAILED: Final = "failed"
STATUS_RATE_LIMITED: Final = "rate_limited"

REASON_TIMEOUT: Final = "timeout"
REASON_ERROR: Final = "error"
REASON_CIRCUIT_OPEN: Final = "circuit_open"

# provider 장애가 아니라 요청이 한도를 넘은 것 — circuit 을 열 이유가 아니다
_NOT_PROVIDER_FAULT: Final = frozenset({ErrorCode.DEC_003})


@dataclass
class DecisionClient:
    """Calls one agent's decision provider with retry, deadline, circuit and metrics.

    에이전트별 결정 provider 클라이언트 (circuit 은 에이전트별, 05 Circuit Breaker).

    Attributes:
        provider: The adapter — single attempt, no retry of its own.
        agent: Agent name for logs and metric labels.
        provider_kind: Provider ``kind`` for labels (``jev``).
        model: Provider model id for labels.
        timeout_s: Whole-call budget including retry waits.
        metrics: Metrics sink; nothing is recorded when omitted.
        sleep: Injectable retry wait — 테스트가 실제로 자지 않게.
        rng: Injectable jitter randomness.
    """

    provider: DecisionProvider
    agent: str
    provider_kind: str
    model: str
    timeout_s: float = DEFAULT_TIMEOUT_S
    metrics: Metrics | None = None
    sleep: Callable[[float], Awaitable[None]] | None = None
    rng: random.Random | None = None
    breaker: CircuitBreaker = field(init=False)

    def __post_init__(self) -> None:
        target = f"decision:{self.agent}"
        observer = CircuitTelemetry(self.metrics, target=target) if self.metrics else None
        self.breaker = CircuitBreaker(
            target=target,
            open_category=ErrorCategory.DECISION,
            open_code=ErrorCode.DEC_001,
            on_transition=observer.observe if observer else None,
        )

    async def decide(
        self, state: str, questions: Sequence[Question], *, task_id: str, use: str
    ) -> list[Decision]:
        """Ask ``questions`` about ``state`` within the budget.

        Raises:
            AaestroError: ``DEC_*`` or TIMEOUT/``TO_004`` — 호출자는 unavailable 로 읽어
                원래 경로로 간다.
                circuit 이 열려 있으면 HTTP 없이 ``DEC_001`` (retryable).
        """
        context: dict[str, object] = {
            LogField.AGENT: self.agent,
            LogField.TASK_ID: task_id,
            LogField.DECISION_USE: use,
            LogField.QUESTION: ",".join(q.name for q in questions),
            LogField.PROVIDER: self.provider_kind,
        }
        if not self.breaker.can_attempt():
            self._unavailable(context, REASON_CIRCUIT_OPEN, ErrorCode.DEC_001)
            raise AaestroError(
                ErrorCategory.DECISION,
                ErrorCode.DEC_001,
                "circuit open",
                agent=self.agent,
                task_id=task_id,
                retryable=True,
                details={"target": self.breaker.target},
            )
        started = time.monotonic()
        try:
            decisions = await self._attempt(state, questions, task_id=task_id, context=context)
        except AaestroError as err:
            if err.code not in _NOT_PROVIDER_FAULT:
                self.breaker.record_failure()
            self._finish(use, started, err)
            reason = REASON_TIMEOUT if err.code == ErrorCode.TO_004 else REASON_ERROR
            self._unavailable(context, reason, err.code)
            raise
        self.breaker.record_success()
        self._finish(use, started, None)
        return decisions

    async def _attempt(
        self,
        state: str,
        questions: Sequence[Question],
        *,
        task_id: str,
        context: dict[str, object],
    ) -> list[Decision]:
        try:
            async with asyncio.timeout(self.timeout_s):
                return await retrying(
                    DECISION_RETRY,
                    lambda: self.provider.decide(state, questions),
                    rng=self.rng,
                    sleep=self.sleep,
                    **context,
                )
        except TimeoutError as err:
            raise AaestroError(
                ErrorCategory.TIMEOUT,
                ErrorCode.TO_004,
                "decision timed out",
                agent=self.agent,
                task_id=task_id,
                retryable=False,
                details={"timeout_s": self.timeout_s},
            ) from err

    def _finish(self, use: str, started: float, err: AaestroError | None) -> None:
        if self.metrics is None:
            return
        if err is None:
            status = STATUS_COMPLETED
        elif err.category is ErrorCategory.RATE_LIMIT:
            status = STATUS_RATE_LIMITED
        else:
            status = STATUS_FAILED
        self.metrics.counter("aaestro_decision_calls_total").labels(
            agent=self.agent, provider=self.provider_kind, model=self.model, use=use, status=status
        ).inc()
        self.metrics.histogram("aaestro_decision_duration_seconds").labels(
            agent=self.agent, provider=self.provider_kind
        ).observe(time.monotonic() - started)

    def _unavailable(self, context: dict[str, object], reason: str, code: str) -> None:
        log.warning(
            "decision unavailable, falling back to the original path",
            reason=reason,
            **context,
            **{LogField.ERROR_CODE: code},
        )
        if self.metrics is not None:
            self.metrics.counter("aaestro_decision_unavailable_total").labels(
                agent=self.agent, provider=self.provider_kind, reason=reason
            ).inc()
