"""A2A client — peer delegation and queries.

에이전트가 선언된 peer 에게 위임/질의하는 경로. 파이프라인 데이터 흐름을 A2A 로
구현하는 것은 안티패턴이다 — checkpoint/재개가 불가능해진다 (03 When to Use).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

from aaestro.core.agent import TaskStatus
from aaestro.core.errors import (
    AaestroError,
    CircuitBreaker,
    ErrorCategory,
    ErrorCode,
    RetryPolicy,
)
from aaestro.observability.circuit import CircuitTelemetry
from aaestro.protocols.a2a.errors import submit_failed, task_rejected, unreachable
from aaestro.protocols.telemetry import STATUS_COMPLETED, STATUS_FAILED, A2aTelemetry
from aaestro.resilience import retrying

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from aaestro.core.agent import TaskRequest, TaskResult
    from aaestro.observability.metrics import Metrics
    from aaestro.protocols.a2a.allowlist import Allowlist
    from aaestro.protocols.a2a.tickets import TicketSource

DEFAULT_CALL_TIMEOUT_S = 120.0

A2A_TERMINAL_STATES = {
    "completed": TaskStatus.COMPLETED,
    "failed": TaskStatus.FAILED,
    "canceled": TaskStatus.CANCELED,
}
"""A2A 종료 상태 → 내부 TaskStatus."""

A2A_INFLIGHT_STATES = frozenset({"submitted", "working"})
"""아직 끝나지 않은 A2A 상태 — TaskStatus 는 종료 상태만 표현하므로 매핑하지 않는다."""


log = structlog.get_logger(__name__)


def map_status(a2a_status: str) -> TaskStatus | None:
    """Map an A2A task state to the internal status.

    A2A task 상태를 내부 상태로 매핑합니다. ``TaskStatus`` 는 **종료 상태만**
    표현하므로 진행 중(``submitted``/``working``)은 ``None`` 을 돌려줍니다 —
    진행 중을 임의의 종료 상태로 뭉개면 호출자가 완료로 오해합니다.

    Args:
        a2a_status: The A2A-side state name.

    Returns:
        The terminal status, or None while the peer task is still in flight.

    Raises:
        ValueError: If the state is neither in-flight nor a known terminal state
            — 미지의 상태를 조용히 성공/실패로 해석하지 않습니다.
    """
    if a2a_status in A2A_INFLIGHT_STATES:
        return None
    try:
        return A2A_TERMINAL_STATES[a2a_status]
    except KeyError as err:
        raise ValueError(f"unknown a2a task state: {a2a_status}") from err


@runtime_checkable
class PeerTransport(Protocol):
    """Sends one task to a peer agent.

    peer 호출 전송 계약. 실제 A2A SDK 는 이 뒤에 감춰진다.
    """

    async def send(
        self, *, callee: str, task: TaskRequest, token: str, headers: Mapping[str, str]
    ) -> TaskResult:
        """peer 에게 태스크를 보내고 결과를 받는다."""
        ...


@dataclass
class A2AClient:
    """One agent's outbound A2A calls.

    에이전트 하나의 A2A 호출. allowlist 와 depth limit 이 caller 측 방어다.
    """

    agent: str
    allowlist: Allowlist
    transport: PeerTransport
    timeout_s: float = DEFAULT_CALL_TIMEOUT_S
    retry: RetryPolicy | None = None
    """도달 실패 재시도 정책 (05 Retry Layering — caller 가 재시도 주체다).
    미주입 시 재시도하지 않는다 — 조립하는 쪽이 켠다."""
    retry_sleep: Callable[[float], Awaitable[None]] | None = None
    """06 은 시간 의존 로직이 테스트에서 실제로 자는 것을 금지한다."""
    breakers: dict[str, CircuitBreaker] = field(default_factory=dict)
    # 주입 지점은 하나다 — telemetry 와 metrics 를 따로 받으면 한쪽만 주입됐을 때
    # 계측이 조용히 반쪽이 되거나 서로 다른 registry 로 흩어진다
    metrics: Metrics | None = None
    tickets: TicketSource | None = None
    """레지스트리 모드의 신원 증명 — 있으면 per-edge token 대신 피호출자 하나에만 쓰는 표."""
    _telemetry: A2aTelemetry | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.metrics is not None:
            self._telemetry = A2aTelemetry(self.metrics, caller=self.agent)

    def peers(self) -> tuple[str, ...]:
        """Peers this agent may call.

        부를 수 있는 peer 목록 — 에이전트는 주소를 직접 알지 못합니다.
        """
        return self.allowlist.peers_of(self.agent)

    def _breaker(self, callee: str) -> CircuitBreaker:
        """peer 별 circuit breaker — 죽은 peer 를 계속 두드리지 않는다."""
        if callee not in self.breakers:
            target = f"a2a:{callee}"
            observer = CircuitTelemetry(self.metrics, target=target) if self.metrics else None
            self.breakers[callee] = CircuitBreaker(
                target=target,
                open_category=ErrorCategory.A2A,
                open_code=ErrorCode.A2A_002,
                on_transition=observer.observe if observer else None,
            )
        return self.breakers[callee]

    async def call(self, callee: str, task: TaskRequest) -> TaskResult:
        """Delegate a task to a declared peer.

        선언된 peer 에게 태스크를 위임합니다. 호출 방향은 배선의 문제이며,
        같은 그룹이라도 선언이 없으면 거부됩니다 (group neutrality).

        Args:
            callee: The peer agent name.
            task: The task to delegate — its trace carries the delegation depth.

        Returns:
            The peer's result.

        Raises:
            AaestroError: A2A/``A2A_004`` if the direction is not declared,
                ``A2A_005`` if the chain is too deep, ``A2A_002`` if the peer is
                unreachable, ``A2A_003`` if the peer rejected the task.
        """
        # caller 측 방어 — 선언과 깊이를 먼저 본다.
        # 거부도 호출 시도다: 카운터에서 빼면 allowlist 위반이 관측되지 않는다
        try:
            self.allowlist.check_call(self.agent, callee, task.trace)
        except AaestroError:
            self._record(callee, status=STATUS_FAILED)
            raise

        breaker = self._breaker(callee)
        if not breaker.can_attempt():
            self._record(callee, status=STATUS_FAILED)
            raise unreachable(self.agent, callee, reason="circuit open")

        token, headers = await self._credentials(callee)
        # 위임 체인이 이어지도록 trace 를 자식으로 넘긴다 — run 전체가 단일 trace
        delegated = task.model_copy(update={"trace": task.trace.child(span_id=task.task_id)})

        started = time.monotonic()
        try:
            result = await self._attempt(callee, delegated, token, headers)
        except AaestroError as err:
            # A2A_003 은 peer 가 살아서 "그 태스크는 못 한다" 고 답한 것이다.
            # 이를 실패로 세면 멀쩡한 peer 가 도달 불가(A2A_002)로 차단된다
            if err.code != ErrorCode.A2A_003:
                breaker.record_failure()
            log.error(
                "a2a call failed",
                a2a_caller=self.agent,
                a2a_callee=callee,
                a2a_task_id=task.task_id,
                error_code=err.code,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            self._record(callee, status=STATUS_FAILED)
            raise
        except Exception:
            breaker.record_failure()
            log.error(
                "a2a call failed",
                a2a_caller=self.agent,
                a2a_callee=callee,
                a2a_task_id=task.task_id,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            self._record(callee, status=STATUS_FAILED)
            raise

        breaker.record_success()
        log.info(
            "a2a call completed",
            a2a_caller=self.agent,
            a2a_callee=callee,
            a2a_task_id=task.task_id,
            status=result.status,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        self._record(callee, status=STATUS_COMPLETED)
        return result

    async def _credentials(self, callee: str) -> tuple[str, dict[str, str]]:
        """피호출자에게 내밀 증명 — 레지스트리 모드면 표, 아니면 per-edge token.

        표를 못 받은 것은 호출 실패다 — 피호출자까지 가 봐야 거부된다.
        """
        if self.tickets is None:
            return self.allowlist.token_for(self.agent, callee), {}
        from aaestro.protocols.a2a.tickets import TICKET_HEADER

        try:
            ticket = await self.tickets.ticket_for(callee)
        except AaestroError:
            self._record(callee, status=STATUS_FAILED)
            raise
        return "", {TICKET_HEADER: ticket}

    async def _attempt(
        self, callee: str, task: TaskRequest, token: str, headers: Mapping[str, str]
    ) -> TaskResult:
        """Send once, retrying only what the policy allows.

        정책이 허용하는 실패만 재시도하며 보냅니다.

        **위임은 부수효과를 낳는다** (05 Rules 5): 재시도가 같은 a2a task_id 를
        유지해야 callee 의 멱등 처리가 걸린다 — 그래서 `task` 를 다시 만들지
        않고 그대로 넘긴다.

        `A2A_003` 은 재시도하지 않는다 — peer 가 **살아서** "그 태스크는 못
        한다" 고 답한 것이라, 다시 보내도 같은 답이 온다 (retryable=False).
        """
        if self.retry is None:
            return await self._send(callee, task, token, headers)

        async def send() -> TaskResult:
            return await self._send(callee, task, token, headers)

        return await retrying(
            self.retry,
            send,
            sleep=self.retry_sleep,
            a2a_caller=self.agent,
            a2a_callee=callee,
            a2a_task_id=task.task_id,
        )

    def _record(self, callee: str, *, status: str) -> None:
        """peer 호출을 메트릭에 남긴다 — telemetry 미주입 시 무동작."""
        if self._telemetry is not None:
            self._telemetry.call_finished(callee=callee, status=status)

    async def _send(
        self, callee: str, task: TaskRequest, token: str, headers: Mapping[str, str]
    ) -> TaskResult:
        """전송 예외를 A2A 코드로 변환한다."""
        try:
            result = await asyncio.wait_for(
                self.transport.send(callee=callee, task=task, token=token, headers=headers),
                timeout=self.timeout_s,
            )
        except TimeoutError as err:
            raise unreachable(
                self.agent, callee, reason="call timeout", timeout_s=self.timeout_s
            ) from err
        except (ConnectionError, OSError) as err:
            raise unreachable(self.agent, callee, reason=type(err).__name__) from err
        except AaestroError:
            # transport 가 이미 분류했다 — 피호출자의 A2A_004 를 A2A_001 로 뭉개면 회수(설정)와
            # 제출 실패(운영)가 구분되지 않는다
            raise
        except Exception as err:
            raise submit_failed(self.agent, callee, reason=type(err).__name__) from err

        if result.status is TaskStatus.FAILED:
            raise task_rejected(
                self.agent, callee, a2a_task_id=task.task_id, peer_error=result.error
            )
        return result


@dataclass
class A2AServer:
    """The callee-side guard for inbound peer calls.

    수신 측 방어. caller 가 자기 이름을 주장하는 것만으로는 부족하므로
    runtime 이 발급한 per-edge token 을 검증한다.
    """

    agent: str
    allowlist: Allowlist

    def authorize(self, caller: str, token: str) -> None:
        """Verify an inbound call.

        수신 호출을 검증합니다.

        Args:
            caller: The claimed calling agent.
            token: The presented per-edge token.

        Raises:
            AaestroError: A2A/``A2A_004`` if the direction is undeclared or the
                token does not match.
        """
        self.allowlist.verify(caller, self.agent, token)
        log.debug("a2a call authorized", a2a_caller=caller, a2a_callee=self.agent)


__all__ = [
    "A2A_INFLIGHT_STATES",
    "A2A_TERMINAL_STATES",
    "DEFAULT_CALL_TIMEOUT_S",
    "A2AClient",
    "A2AServer",
    "PeerTransport",
    "map_status",
]
