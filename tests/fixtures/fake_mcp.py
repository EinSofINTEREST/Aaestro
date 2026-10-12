"""Fake MCP transport and clients.

실제 MCP 서버 없이 세션 시나리오(성공/실패/단절/지연)를 스크립트한다.
"""

from __future__ import annotations

from typing import Any

from aaestro.protocols.mcp.session import Connection, ToolResult

SUPPORTED_VERSION = "2025-06-18"


class FakeTransport:
    """스크립트된 연결/호출 결과를 돌려주는 전송 대역."""

    def __init__(
        self,
        tools: list[str] | None = None,
        *,
        protocol_version: str = SUPPORTED_VERSION,
        connect_errors: list[Exception | None] | None = None,
        call_error: Exception | None = None,
        result: Any = "ok",
    ) -> None:
        self._tools = tuple(tools or ["read_file", "write_file"])
        self._protocol_version = protocol_version
        self._connect_errors = list(connect_errors or [])
        self._call_error = call_error
        self._result = result
        self.connects = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed: list[Connection] = []

    def fail_calls_with(self, error: Exception) -> None:
        """이후 호출이 이 예외로 실패한다."""
        self._call_error = error

    def heal(self) -> None:
        """이후 호출이 성공한다 — 재연결 후 복구 시나리오용."""
        self._call_error = None

    async def connect(self, spec: Any) -> Connection:
        self.connects += 1
        if self._connect_errors:
            error = self._connect_errors.pop(0)
            if error is not None:
                raise error
        return Connection(
            tools=self._tools, protocol_version=self._protocol_version, handle=object()
        )

    async def call(self, connection: Connection, tool: str, arguments: Any) -> ToolResult:
        self.calls.append((tool, dict(arguments)))
        if self._call_error is not None:
            raise self._call_error
        return ToolResult(content=self._result)

    async def close(self, connection: Connection) -> None:
        self.closed.append(connection)


class FakeStdioClient:
    """자식 프로세스 기동을 스크립트하는 stdio 클라이언트 대역."""

    def __init__(self, tools: list[str] | None = None) -> None:
        self._tools = tuple(tools or ["read_file"])
        self.spawned: list[tuple[list[str], dict[str, str]]] = []
        self.terminated = 0

    async def spawn(self, *, command: list[str], env: dict[str, str]) -> Connection:
        self.spawned.append((command, env))
        return Connection(tools=self._tools, protocol_version=SUPPORTED_VERSION)

    async def call(self, connection: Connection, tool: str, arguments: Any) -> ToolResult:
        return ToolResult(content=tool)

    async def terminate(self, connection: Connection) -> None:
        self.terminated += 1


class FakeHttpClient:
    """원격 접속을 스크립트하는 HTTP 클라이언트 대역."""

    def __init__(self, tools: list[str] | None = None, *, refusals: int = 0) -> None:
        self._tools = tuple(tools or ["search"])
        self._refusals = refusals
        self.connections: list[tuple[str, dict[str, str]]] = []
        self.disconnected = 0

    async def connect(self, *, url: str, headers: dict[str, str]) -> Connection:
        self.connections.append((url, dict(headers)))
        if self._refusals > 0:
            # 아직 듣지 않는 서버 — 막 뜬 사이드카
            self._refusals -= 1
            raise ConnectionRefusedError(url)
        return Connection(tools=self._tools, protocol_version=SUPPORTED_VERSION)

    async def call(self, connection: Connection, tool: str, arguments: Any) -> ToolResult:
        return ToolResult(content=tool)

    async def disconnect(self, connection: Connection) -> None:
        self.disconnected += 1


__all__ = ["SUPPORTED_VERSION", "FakeHttpClient", "FakeStdioClient", "FakeTransport"]
