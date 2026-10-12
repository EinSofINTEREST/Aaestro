"""Unit tests for MCP session lifecycle.

실제 MCP 서버 없이 검증한다 — 이 계층의 계약은 에러 변환과 세션 상태 전이지
전송 구현이 아니다. 재연결 대기는 sleep 주입으로 즉시 진행한다.
"""

from __future__ import annotations

import pytest

from aaestro.core.agent import HealthState
from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.core.manifest import McpServerSpec
from aaestro.protocols.mcp.session import McpSession, ToolResult
from tests.fixtures.fake_mcp import FakeTransport


def stdio_spec(**overrides) -> McpServerSpec:
    """stdio 서버 선언."""
    base = {
        "name": "filesystem",
        "transport": "stdio",
        "command": ["mcp-server-filesystem", "/workspace"],
    }
    base.update(overrides)
    return McpServerSpec.model_validate(base)


def make_session(transport: FakeTransport, **overrides) -> McpSession:
    """대기 없이 도는 세션 — sleep 을 주입해 실제 시간을 쓰지 않는다."""
    slept: list[float] = []

    async def sleep(delay: float) -> None:
        slept.append(delay)

    session = McpSession(
        spec=overrides.pop("spec", stdio_spec()),
        transport=transport,
        agent="researcher",
        sleep=sleep,
        **overrides,
    )
    session.slept = slept  # type: ignore[attr-defined]
    return session


# --- initialize --------------------------------------------------------------


async def test_initialize_binds_the_reported_tools():
    session = make_session(FakeTransport(["read_file", "write_file"]))

    tools = await session.initialize()

    assert tools == ("read_file", "write_file")
    assert session.connected is True


async def test_allowed_tools_filters_the_binding():
    """서버가 노출하는 전체를 무조건 바인딩하지 않는다 (03 Tool Filtering)."""
    session = make_session(
        FakeTransport(["read_file", "write_file", "list_directory"]),
        spec=stdio_spec(allowed_tools=["read_file", "list_directory"]),
    )

    tools = await session.initialize()

    assert tools == ("read_file", "list_directory")


async def test_connect_failure_is_mcp_001():
    """기동 실패는 설정 문제 — 재시도해도 소용없다."""
    session = make_session(FakeTransport(connect_errors=[RuntimeError("boom")]))

    with pytest.raises(AaestroError) as exc_info:
        await session.initialize()

    assert exc_info.value.code == "MCP_001"
    assert exc_info.value.category is ErrorCategory.MCP
    assert exc_info.value.retryable is False
    assert exc_info.value.details["mcp_server"] == "filesystem"


async def test_unsupported_protocol_version_is_rejected():
    """범위 밖 버전으로 계속 가면 어긋난 동작이 조용히 퍼진다."""
    transport = FakeTransport(protocol_version="1999-01-01")
    session = make_session(transport)

    with pytest.raises(AaestroError) as exc_info:
        await session.initialize()

    assert exc_info.value.code == "MCP_001"
    assert exc_info.value.details["protocol_version"] == "1999-01-01"
    # 거부한 연결은 정리하고 나간다 — 좀비 금지
    assert transport.closed


# --- tool 호출 ---------------------------------------------------------------


async def test_call_tool_returns_the_result():
    session = make_session(FakeTransport(["read_file"], result="contents"))
    await session.initialize()

    result = await session.call_tool("read_file", {"path": "a.txt"})

    assert isinstance(result, ToolResult)
    assert result.content == "contents"


async def test_unbound_tool_is_mcp_002():
    """필터로 걸러졌거나 서버가 노출하지 않는 tool."""
    session = make_session(FakeTransport(["read_file"]))
    await session.initialize()

    with pytest.raises(AaestroError) as exc_info:
        await session.call_tool("write_file", {})

    assert exc_info.value.code == "MCP_002"


async def test_tool_execution_failure_is_mcp_003():
    session = make_session(FakeTransport(["read_file"], call_error=ValueError("bad args")))
    await session.initialize()

    with pytest.raises(AaestroError) as exc_info:
        await session.call_tool("read_file", {})

    assert exc_info.value.code == "MCP_003"
    assert exc_info.value.retryable is False
    # 프록시의 도구 판정 거부(ACC_001)는 메시지에만 있다 — 사유가 호출자에게 닿아야 한다 (#282)
    assert exc_info.value.details["detail"] == "bad args"


async def test_transport_loss_is_retryable_mcp_004():
    """단절은 재연결하면 풀릴 수 있으므로 retryable 이어야 한다."""
    transport = FakeTransport(["read_file"], call_error=ConnectionError("gone"))
    session = make_session(transport, max_reconnects=1)
    await session.initialize()

    with pytest.raises(AaestroError) as exc_info:
        await session.call_tool("read_file", {})

    assert exc_info.value.code == "MCP_004"
    assert exc_info.value.retryable is True


async def test_call_without_a_session_reconnects_first():
    """세션이 없으면 재연결을 시도한다 — 살아있는 서버를 죽었다고 단정하지 않는다."""
    transport = FakeTransport(["read_file"])
    session = make_session(transport)

    result = await session.call_tool("read_file", {})

    assert result.content == "ok"
    assert transport.connects == 1


async def test_call_without_a_session_reports_transport_lost_when_dead():
    """재연결도 실패하면 tool 미존재가 아니라 단절로 보고한다."""
    session = make_session(
        FakeTransport(["read_file"], connect_errors=[RuntimeError("down")]), max_reconnects=1
    )

    with pytest.raises(AaestroError) as exc_info:
        await session.call_tool("read_file", {})

    assert exc_info.value.code == "MCP_004"
    assert exc_info.value.retryable is True


async def test_disconnected_session_does_not_report_unknown_tool():
    """단절 상태에서 tools 가 비어 MCP_002 로 오분류되면 호출자가 재시도를 포기한다."""
    transport = FakeTransport(["read_file"], connect_errors=[None, RuntimeError("down")])
    session = make_session(transport, max_reconnects=1)
    await session.initialize()
    transport.fail_calls_with(ConnectionError("gone"))

    with pytest.raises(AaestroError):
        await session.call_tool("read_file", {})
    assert session.connected is False

    with pytest.raises(AaestroError) as exc_info:
        await session.call_tool("read_file", {})

    assert exc_info.value.code == "MCP_004"
    assert exc_info.value.retryable is True


async def test_reconnect_failure_counts_toward_the_circuit():
    """재연결 자체가 실패해도 breaker 가 열려야 죽은 서버를 계속 두드리지 않는다."""
    transport = FakeTransport(
        ["read_file"], connect_errors=[None, *(RuntimeError("down") for _ in range(20))]
    )
    session = make_session(transport, max_reconnects=1)
    await session.initialize()
    transport.fail_calls_with(ConnectionError("gone"))

    assert session.breaker is not None
    for _ in range(5):
        with pytest.raises(AaestroError):
            await session.call_tool("read_file", {})

    assert session.breaker.can_attempt() is False


# --- 재연결 -------------------------------------------------------------------


async def test_transport_loss_reconnects_and_retries_once():
    """단절 후 재연결에 성공하면 호출이 이어진다 — 재시도 계층은 하나뿐이다."""
    transport = FakeTransport(["read_file"])
    session = make_session(transport)
    await session.initialize()
    transport.fail_calls_with(ConnectionError("gone"))

    original = session.transport.connect

    async def connect_and_heal(spec):
        connection = await original(spec)
        transport.heal()
        return connection

    session.transport.connect = connect_and_heal  # type: ignore[method-assign]

    result = await session.call_tool("read_file", {})

    assert result.content == "ok"
    assert transport.connects == 2  # 최초 + 재연결


async def test_reconnect_backoff_grows_and_is_capped():
    """백오프는 지수적으로 늘고 상한에서 멈춘다."""
    transport = FakeTransport(
        ["read_file"],
        connect_errors=[None, RuntimeError("x"), RuntimeError("x"), RuntimeError("x")],
    )
    session = make_session(transport, max_reconnects=3)
    await session.initialize()

    with pytest.raises(AaestroError) as exc_info:
        await session.reconnect()

    assert exc_info.value.code == "MCP_004"
    assert session.slept == [1.0, 2.0]  # 마지막 시도 뒤에는 대기하지 않는다


async def test_exhausted_reconnect_reports_unhealthy():
    """재연결 실패 누적은 숨기지 않는다 — health 로 드러낸다."""
    transport = FakeTransport(["read_file"], connect_errors=[None, RuntimeError("x")])
    session = make_session(transport, max_reconnects=1)
    await session.initialize()

    with pytest.raises(AaestroError):
        await session.reconnect()

    assert session.health().state is HealthState.UNHEALTHY


async def test_healthy_session_reports_healthy():
    session = make_session(FakeTransport(["read_file"]))
    await session.initialize()

    assert session.health().state is HealthState.HEALTHY


async def test_uninitialized_session_reports_degraded():
    session = make_session(FakeTransport(["read_file"]))

    assert session.health().state is HealthState.DEGRADED


# --- 정리 ---------------------------------------------------------------------


async def test_shutdown_closes_the_transport():
    """세션과 자식 프로세스를 회수한다 — 좀비 프로세스 금지."""
    transport = FakeTransport(["read_file"])
    session = make_session(transport)
    await session.initialize()

    await session.shutdown()

    assert len(transport.closed) == 1
    assert session.connected is False


async def test_shutdown_survives_a_failing_close():
    """정리 실패가 종료 경로를 막으면 안 된다."""

    class Failing(FakeTransport):
        async def close(self, connection):
            raise RuntimeError("close failed")

    session = make_session(Failing(["read_file"]))
    await session.initialize()

    await session.shutdown()

    assert session.connected is False


async def test_circuit_opens_after_repeated_failures():
    """반복 실패하는 서버를 계속 두드리지 않는다."""
    transport = FakeTransport(["read_file"], call_error=ValueError("bad"))
    session = make_session(transport)
    await session.initialize()

    for _ in range(5):
        with pytest.raises(AaestroError):
            await session.call_tool("read_file", {})

    with pytest.raises(AaestroError) as exc_info:
        await session.call_tool("read_file", {})

    assert exc_info.value.code == "MCP_004"
    assert "circuit open" in str(exc_info.value.details.get("reason", ""))
