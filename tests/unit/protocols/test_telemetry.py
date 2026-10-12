"""Protocol-layer metric wiring tests.

MCP/A2A 호출과 circuit breaker 상태가 실제 실행 경로에서 집계되는지 검증한다.
registry 는 테스트마다 격리한다 (프로세스 전역 오염 금지).
"""

from __future__ import annotations

import pytest
from prometheus_client import CollectorRegistry

from aaestro.core.agent import TaskResult
from aaestro.core.errors import AaestroError, CircuitBreaker, CircuitState, ErrorCategory, ErrorCode
from aaestro.observability.circuit import CircuitTelemetry
from aaestro.observability.metrics import Metrics
from aaestro.protocols.a2a.allowlist import Allowlist, Edge
from aaestro.protocols.a2a.client import A2AClient
from aaestro.protocols.mcp.client import McpClient
from aaestro.protocols.mcp.session import ToolResult
from aaestro.protocols.telemetry import McpTelemetry
from tests.fixtures.builders import make_task

SECRET = b"runtime-secret"


def make_metrics() -> Metrics:
    """이 테스트만의 registry."""
    return Metrics(registry=CollectorRegistry())


def value(metrics: Metrics, name: str, **labels: str) -> float:
    """해당 라벨 조합의 현재 값 — 미기록이면 0.0."""
    return metrics.registry.get_sample_value(name, labels) or 0.0


# --- MCP tool 호출 -----------------------------------------------------------


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_mcp_tool_calls_are_counted_per_status(status):
    metrics = make_metrics()
    telemetry = McpTelemetry(metrics, agent="researcher")

    telemetry.tool_called(server="filesystem", tool="read_file", status=status)

    assert (
        value(
            metrics,
            "aaestro_mcp_tool_calls_total",
            agent="researcher",
            server="filesystem",
            tool="read_file",
            status=status,
        )
        == 1.0
    )


# --- A2A 호출 ----------------------------------------------------------------


class FakePeer:
    """peer 응답을 스크립트하는 전송 대역."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    async def send(self, *, callee, task, token, headers):
        if self._error is not None:
            raise self._error
        return TaskResult.completed(task, output={"answer": "42"})


def make_a2a_client(
    *, metrics: Metrics | None = None, transport=None, edges=(("researcher", "planner"),)
) -> A2AClient:
    allowlist = Allowlist(
        edges=frozenset(Edge(caller=c, callee=e) for c, e in edges),
        secret=SECRET,
        max_depth=3,
    )
    return A2AClient(
        agent="researcher",
        allowlist=allowlist,
        transport=transport or FakePeer(),
        metrics=metrics,
    )


async def test_successful_peer_call_is_counted():
    """대시보드의 caller×callee 매트릭스가 이 라벨 조합에 의존한다."""
    metrics = make_metrics()
    client = make_a2a_client(metrics=metrics)

    await client.call("planner", make_task())

    assert (
        value(
            metrics,
            "aaestro_a2a_calls_total",
            caller="researcher",
            callee="planner",
            status="completed",
        )
        == 1.0
    )


async def test_allowlist_rejection_is_counted_as_a_failed_call():
    """거부도 호출 시도다 — 빠지면 A2A_004 위반이 메트릭에서 보이지 않는다."""
    metrics = make_metrics()
    client = make_a2a_client(metrics=metrics)

    with pytest.raises(AaestroError) as exc_info:
        await client.call("writer", make_task())

    assert exc_info.value.code == ErrorCode.A2A_004
    assert (
        value(
            metrics,
            "aaestro_a2a_calls_total",
            caller="researcher",
            callee="writer",
            status="failed",
        )
        == 1.0
    )


async def test_unreachable_peer_is_counted_as_failed():
    metrics = make_metrics()
    client = make_a2a_client(metrics=metrics, transport=FakePeer(error=ConnectionError("down")))

    with pytest.raises(AaestroError):
        await client.call("planner", make_task())

    assert (
        value(
            metrics,
            "aaestro_a2a_calls_total",
            caller="researcher",
            callee="planner",
            status="failed",
        )
        == 1.0
    )


async def test_peer_calls_work_without_metrics():
    """메트릭 미주입 시에도 호출 경로가 무오류로 동작해야 한다."""
    client = make_a2a_client()

    result = await client.call("planner", make_task())

    assert result.output == {"answer": "42"}


# --- circuit breaker 상태 ----------------------------------------------------


def make_breaker(metrics: Metrics, *, target: str, max_failures: int = 2) -> CircuitBreaker:
    observer = CircuitTelemetry(metrics, target=target)
    return CircuitBreaker(
        max_failures=max_failures,
        target=target,
        open_category=ErrorCategory.A2A,
        open_code=ErrorCode.A2A_002,
        on_transition=observer.observe,
    )


def test_opening_the_circuit_is_observed():
    """open 전환이 관측되지 않으면 장애 확산을 사후에 재구성할 수 없다."""
    metrics = make_metrics()
    breaker = make_breaker(metrics, target="a2a:planner")

    breaker.record_failure()
    breaker.record_failure()

    assert value(metrics, "aaestro_circuit_state", target="a2a:planner") == 1.0


def test_recovering_returns_the_gauge_to_closed():
    metrics = make_metrics()
    breaker = make_breaker(metrics, target="a2a:planner")
    breaker.record_failure()
    breaker.record_failure()

    breaker.record_success()

    assert value(metrics, "aaestro_circuit_state", target="a2a:planner") == 0.0


def test_reset_timeout_moves_the_gauge_to_half_open():
    """시간 판정은 주입된 clock 으로 결정적으로 만든다 (06 규칙)."""
    metrics = make_metrics()
    now = [0.0]
    observer = CircuitTelemetry(metrics, target="mcp:filesystem")
    breaker = CircuitBreaker(
        max_failures=1,
        reset_timeout_s=60.0,
        target="mcp:filesystem",
        open_category=ErrorCategory.MCP,
        open_code=ErrorCode.MCP_004,
        clock=lambda: now[0],
        on_transition=observer.observe,
    )
    breaker.record_failure()

    now[0] = 61.0
    assert breaker.state is CircuitState.HALF_OPEN

    assert value(metrics, "aaestro_circuit_state", target="mcp:filesystem") == 2.0


def test_repeated_failures_do_not_re_notify():
    """같은 상태로의 재진입은 관찰자를 부르지 않는다."""
    seen: list[CircuitState] = []
    breaker = CircuitBreaker(max_failures=1, on_transition=seen.append)

    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()

    assert seen == [CircuitState.OPEN]


def test_breaker_works_without_an_observer():
    breaker = CircuitBreaker(max_failures=1)

    breaker.record_failure()

    assert breaker.state is CircuitState.OPEN


# --- MCP 배선 (계측 클래스가 아니라 실제 호출 경로) ---------------------------


class FakeSession:
    """tool 결과를 스크립트하는 세션 대역."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    async def call_tool(self, tool: str, arguments):
        if self._error is not None:
            raise self._error
        return ToolResult(content=tool)


def make_mcp_client(metrics: Metrics | None, *, error: Exception | None = None) -> McpClient:
    client = McpClient(agent="researcher", transports=None, metrics=metrics)  # type: ignore[arg-type]
    client.sessions["filesystem"] = FakeSession(error)  # type: ignore[assignment]
    return client


async def test_call_tool_counts_a_successful_call():
    """계측 클래스만 테스트하면 배선을 지워도 통과한다 — 경로로 검증한다."""
    metrics = make_metrics()
    client = make_mcp_client(metrics)

    await client.call_tool("mcp__filesystem__read_file", {"path": "a"})

    assert (
        value(
            metrics,
            "aaestro_mcp_tool_calls_total",
            agent="researcher",
            server="filesystem",
            tool="read_file",
            status="completed",
        )
        == 1.0
    )


async def test_call_tool_counts_a_failed_call():
    metrics = make_metrics()
    client = make_mcp_client(metrics, error=RuntimeError("transport lost"))

    with pytest.raises(RuntimeError):
        await client.call_tool("mcp__filesystem__read_file", {})

    assert (
        value(
            metrics,
            "aaestro_mcp_tool_calls_total",
            agent="researcher",
            server="filesystem",
            tool="read_file",
            status="failed",
        )
        == 1.0
    )


async def test_call_tool_works_without_metrics():
    client = make_mcp_client(None)

    result = await client.call_tool("mcp__filesystem__read_file", {})

    assert result.content == "read_file"
