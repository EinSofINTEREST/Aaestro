"""Per-agent MCP client.

에이전트별로 격리된 MCP 클라이언트. 모든 세션은 정확히 하나의 에이전트에 속한다.
"""

from aaestro.protocols.mcp.client import MCP_TOOL_PREFIX, McpClient, split_namespaced
from aaestro.protocols.mcp.errors import (
    mcp_error,
    startup_failed,
    tool_failed,
    transport_lost,
    unknown_tool,
)
from aaestro.protocols.mcp.session import (
    SUPPORTED_PROTOCOL_VERSIONS,
    Connection,
    McpSession,
    ToolResult,
    Transport,
)
from aaestro.protocols.mcp.transport import (
    HttpTransport,
    StdioTransport,
    TransportSelector,
    resolve_env,
)

__all__ = [
    "MCP_TOOL_PREFIX",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "Connection",
    "HttpTransport",
    "McpClient",
    "McpSession",
    "StdioTransport",
    "ToolResult",
    "Transport",
    "TransportSelector",
    "mcp_error",
    "resolve_env",
    "split_namespaced",
    "startup_failed",
    "tool_failed",
    "transport_lost",
    "unknown_tool",
]
