"""Protocol-layer metric collection.

MCP/A2A 원격 호출과 circuit breaker 상태의 메트릭 집계. 주입하지 않으면
아무 것도 기록하지 않는다 — 프로토콜 계층은 메트릭 없이도 온전히 동작한다.
"""

from __future__ import annotations

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
