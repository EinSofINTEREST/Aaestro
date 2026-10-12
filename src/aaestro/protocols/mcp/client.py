"""Per-agent MCP client — owns every session this agent declares.

에이전트 하나가 소유하는 MCP 세션 전부를 관리한다. 다른 에이전트와 세션을
공유하지 않는다 — 격리 경계가 곧 권한 경계다 (03 Core Principle).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from aaestro.core.tools import MCP_TOOL_PREFIX, namespaced, split_namespaced
from aaestro.protocols.mcp.errors import unknown_tool
from aaestro.protocols.mcp.session import (
    DEFAULT_STARTUP_TIMEOUT_S,
    McpSession,
    ToolResult,
)
from aaestro.protocols.telemetry import STATUS_COMPLETED, STATUS_FAILED, McpTelemetry

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from aaestro.core.agent import ComponentHealth
    from aaestro.core.manifest import McpServerSpec
    from aaestro.observability.metrics import Metrics
    from aaestro.protocols.mcp.transport import TransportSelector

log = structlog.get_logger(__name__)


@dataclass
class McpClient:
    """The MCP sessions belonging to one agent.

    에이전트 하나의 MCP 세션 모음. ``agentd`` 의 ``McpLauncher`` 계약을 구현해
    기동 시퀀스에 물린다.
    """

    agent: str
    transports: TransportSelector
    sessions: dict[str, McpSession] = field(default_factory=dict)
    # 주입 지점은 하나다 — telemetry 와 metrics 를 따로 받으면 한쪽만 주입됐을 때
    # 계측이 조용히 반쪽이 되거나 서로 다른 registry 로 흩어진다
    metrics: Metrics | None = None
    _telemetry: McpTelemetry | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.metrics is not None:
            self._telemetry = McpTelemetry(self.metrics, agent=self.agent)

    async def start(
        self, spec: McpServerSpec, *, timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S
    ) -> Sequence[str]:
        """Start one declared server and report its tools.

        선언된 서버 하나를 기동하고 tool 목록을 보고합니다 —
        ``agentd.bootstrap.McpLauncher`` 계약입니다.

        Args:
            spec: The server declaration.
            timeout_s: Per-server startup budget.

        Returns:
            The bound tool names after ``allowed_tools`` filtering.

        Raises:
            AaestroError: MCP/``MCP_001`` if the server fails to initialize.
        """
        live = self.sessions.get(spec.name)
        if live is not None and live.connected:
            # 리로드가 같은 기동 시퀀스를 탄다 — 떠 있는 세션을 다시 띄우면 연결과 스키마가 바뀐다
            return live.tools
        session = McpSession(
            spec=spec,
            transport=self.transports.for_spec(spec),
            agent=self.agent,
            startup_timeout_s=timeout_s,
            metrics=self.metrics,
        )
        tools = await session.initialize()
        self.sessions[spec.name] = session
        return tools

    def schemas(self) -> dict[str, dict[str, Any]]:
        """Namespaced tool name to its input schema.

        네임스페이스가 붙은 tool 이름 → input schema. 이름만으로는 모델이 인자를
        채울 수 없으므로, 기동이 이것을 tool registry 로 흘려보냅니다.
        """
        return {
            namespaced(server, tool): schema
            for server, session in self.sessions.items()
            for tool, schema in session.schemas.items()
        }

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        """Call a namespaced MCP tool.

        네임스페이스가 붙은 MCP tool 을 호출합니다. 반환 내용은
        **신뢰하지 않는 입력**입니다 (03 Security 6).

        Args:
            name: Namespaced name, ``mcp__{server}__{tool}``.
            arguments: Tool arguments.

        Returns:
            The tool result.

        Raises:
            AaestroError: MCP/``MCP_002`` if the name does not resolve to a
                bound tool on a live session.
        """
        parts = split_namespaced(name)
        if parts is None:
            raise unknown_tool(self.agent, "unknown", name)
        server, tool = parts

        session = self.sessions.get(server)
        if session is None:
            raise unknown_tool(self.agent, server, tool)

        started = time.monotonic()
        try:
            result = await session.call_tool(tool, arguments)
        except Exception:
            log.error(
                "mcp tool call failed",
                agent=self.agent,
                mcp_server=server,
                tool=tool,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            self._record(server=server, tool=tool, status=STATUS_FAILED)
            raise

        log.info(
            "mcp tool call completed",
            agent=self.agent,
            mcp_server=server,
            tool=tool,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        self._record(server=server, tool=tool, status=STATUS_COMPLETED)
        return result

    def _record(self, *, server: str, tool: str, status: str) -> None:
        """tool 호출을 메트릭에 남긴다 — telemetry 미주입 시 무동작."""
        if self._telemetry is not None:
            self._telemetry.tool_called(server=server, tool=tool, status=status)

    def tools(self) -> dict[str, str]:
        """바인딩된 tool 전체 — 네임스페이스 이름에서 소유 서버로."""
        return {
            namespaced(name, tool): name
            for name, session in self.sessions.items()
            for tool in session.tools
        }

    def health(self) -> dict[str, ComponentHealth]:
        """세션별 상태 — health 종합에 그대로 실린다."""
        return {f"mcp:{name}": session.health() for name, session in self.sessions.items()}

    async def shutdown(self) -> None:
        """모든 세션을 정리한다 — 하나가 실패해도 나머지를 계속 정리한다."""
        for session in self.sessions.values():
            await session.shutdown()
        self.sessions.clear()


__all__ = [
    "MCP_TOOL_PREFIX",
    "McpClient",
    "split_namespaced",
]
