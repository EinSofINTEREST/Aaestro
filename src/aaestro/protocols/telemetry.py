"""Protocol-layer metric collection.

MCP/A2A 원격 호출과 circuit breaker 상태의 메트릭 집계. 주입하지 않으면
아무 것도 기록하지 않는다 — 프로토콜 계층은 메트릭 없이도 온전히 동작한다.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from aaestro.observability.metrics import Metrics

STATUS_COMPLETED: Final = "completed"
STATUS_FAILED: Final = "failed"


class McpTelemetry:
    """Records MCP tool-call metrics.

    MCP tool 호출 메트릭을 기록합니다. 서버·tool 라벨은 호출마다 달라지므로
    기록 시점에 받고, ``agent`` 만 생성 시 고정합니다.
    """

    def __init__(self, metrics: Metrics, *, agent: str) -> None:
        self._metrics = metrics
        self._agent = agent

    def tool_called(self, *, server: str, tool: str, status: str) -> None:
        """tool 호출 결과를 집계한다."""
        self._metrics.counter("aaestro_mcp_tool_calls_total").labels(
            agent=self._agent, server=server, tool=tool, status=status
        ).inc()


class A2aTelemetry:
    """Records A2A call metrics.

    A2A 호출 메트릭을 기록합니다. 대시보드의 caller×callee 매트릭스가 이
    라벨 조합에 의존합니다.
    """

    def __init__(self, metrics: Metrics, *, caller: str) -> None:
        self._metrics = metrics
        self._caller = caller

    def call_finished(self, *, callee: str, status: str) -> None:
        """peer 호출 결과를 집계한다 — allowlist 거부도 실패로 남긴다."""
        self._metrics.counter("aaestro_a2a_calls_total").labels(
            caller=self._caller, callee=callee, status=status
        ).inc()


class A2aRefusal(StrEnum):
    """Why the callee turned an inbound A2A request away — a closed set (``reason`` label).

    수신 측 거절 사유. 호출자 이름은 라벨로 쓰지 않는다 — 확인되지 않은 값이라 위조되고 요청마다
    새 시계열이 생긴다 (09 의 ``undeclared`` 와 같은 결).
    """

    NO_CREDENTIAL = "no_credential"
    """자격 (토큰 · 표) 을 싣지 않았다."""
    DENIED = "denied"
    """자격이 틀렸거나 판정이 거부했다 (A2A_004)."""
    MALFORMED = "malformed"
    """본문이 계약에 맞지 않는다 (VAL_001 · VAL_002)."""
    TOO_DEEP = "too_deep"
    """위임 깊이 초과 (A2A_005)."""
    UNSUPPORTED = "unsupported"
    """열지 않은 요청 — 태스크 조회 · 구독 · 취소 · push · 확장 카드 · 기존 태스크에 잇는 메시지."""


class A2aInboundTelemetry:
    """Counts inbound A2A refusals on the callee side.

    피호출자 쪽 거절을 셉니다 — 자격 없는 요청이 몰려도 ``aaestro_a2a_calls_total`` (호출자 쪽
    집계) 에는 잡히지 않습니다 (#420).
    """

    def __init__(self, metrics: Metrics, *, callee: str) -> None:
        self._metrics = metrics
        self._callee = callee

    def refused(self, reason: A2aRefusal) -> None:
        """거절 하나를 센다."""
        self._metrics.counter("aaestro_a2a_refusals_total").labels(
            callee=self._callee, reason=reason.value
        ).inc()
