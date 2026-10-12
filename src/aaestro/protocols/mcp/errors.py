"""MCP error mapping.

MCP 계층의 boundary 변환. 재시도/라우팅 전략이 카테고리·코드에 의존하므로
원 예외를 여기서 반드시 구조화 에러로 바꾼다 (05 Layer Rules).
"""

from __future__ import annotations

from typing import Any

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode


def mcp_error(
    code: ErrorCode | str,
    message: str,
    *,
    agent: str,
    server: str,
    retryable: bool = False,
    **details: Any,
) -> AaestroError:
    """Build a structured MCP error.

    MCP 실패를 구조화 에러로 만듭니다 — 로그/메트릭이 쓰는 표준 필드
    (``agent``, ``mcp_server``) 를 항상 채웁니다.

    Args:
        code: The MCP error code.
        message: Lowercase message without a trailing period.
        agent: Owning agent name.
        server: MCP server name.
        retryable: Whether a retry could succeed.
        **details: Extra machine-readable context.

    Returns:
        The structured error, ready to raise ``from`` the cause.
    """
    return AaestroError(
        category=ErrorCategory.MCP,
        code=code,
        message=message,
        agent=agent,
        retryable=retryable,
        details={"mcp_server": server, **details},
    )


def startup_failed(agent: str, server: str, **details: Any) -> AaestroError:
    """기동/initialize 실패 — 설정 문제이므로 재시도 무의미."""
    return mcp_error(
        ErrorCode.MCP_001,
        f"mcp server failed to initialize: {server}",
        agent=agent,
        server=server,
        **details,
    )


def unknown_tool(agent: str, server: str, tool: str) -> AaestroError:
    """선언되지 않았거나 필터로 걸러진 tool."""
    return mcp_error(
        ErrorCode.MCP_002,
        f"unknown mcp tool: {tool}",
        agent=agent,
        server=server,
        tool=tool,
    )


def tool_failed(agent: str, server: str, tool: str, **details: Any) -> AaestroError:
    """tool 실행 실패 — 재시도 여부는 tool 성격에 달렸으므로 기본 False."""
    return mcp_error(
        ErrorCode.MCP_003,
        f"mcp tool call failed: {tool}",
        agent=agent,
        server=server,
        tool=tool,
        **details,
    )


def transport_lost(agent: str, server: str, **details: Any) -> AaestroError:
    """전송 단절 — 재연결 후 성공할 수 있으므로 retryable."""
    return mcp_error(
        ErrorCode.MCP_004,
        "mcp transport disconnected",
        agent=agent,
        server=server,
        retryable=True,
        **details,
    )


__all__ = [
    "mcp_error",
    "startup_failed",
    "tool_failed",
    "transport_lost",
    "unknown_tool",
]
