"""Tool namespace contract.

Tool 이름의 네임스페이스 규약. MCP tool 은 접두사로 skillset tool 과 구분되며,
이 구분에 재시도·알림 전략(05)과 메트릭 라벨이 걸려 있다.

정의가 여러 곳에 흩어지면 하나만 바뀌었을 때 **조용히 갈라진다** — MCP tool
실패가 ``SKILL_001`` 로 잘못 변환되고 메트릭의 출처 라벨도 함께 틀어진다.
그래서 이 상수는 저장소에 하나만 존재한다.
"""

from __future__ import annotations

from typing import Final

MCP_TOOL_PREFIX: Final = "mcp__"


def is_mcp_tool(name: str) -> bool:
    """이 tool 이름이 MCP 서버에서 온 것인지."""
    return name.startswith(MCP_TOOL_PREFIX)


def namespaced(server: str, tool: str) -> str:
    """MCP tool 을 ``mcp__{server}__{tool}`` 로 네임스페이싱한다."""
    return f"{MCP_TOOL_PREFIX}{server}__{tool}"


def split_namespaced(name: str) -> tuple[str, str] | None:
    """Split a namespaced MCP tool name into server and tool.

    ``mcp__{server}__{tool}`` 를 서버와 tool 로 나눕니다. 형식이 아니면 None —
    skillset tool 과 구분하는 판별에도 씁니다.

    Args:
        name: The possibly namespaced tool name.

    Returns:
        ``(server, tool)`` or None if the name is not an MCP tool.
    """
    if not is_mcp_tool(name):
        return None
    remainder = name[len(MCP_TOOL_PREFIX) :]
    server, separator, tool = remainder.partition("__")
    if not separator or not server or not tool:
        return None
    return server, tool


__all__ = ["MCP_TOOL_PREFIX", "is_mcp_tool", "namespaced", "split_namespaced"]
