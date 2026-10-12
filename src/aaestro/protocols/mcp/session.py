"""MCP session lifecycle.

세션 하나 = 서버 하나 = 소유 에이전트 하나. 단절은 backoff 재연결로 흡수하되,
누적 실패는 숨기지 않고 unhealthy 로 드러낸다 (silent degradation 금지).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog
from mcp.types.version import SUPPORTED_PROTOCOL_VERSIONS as _SDK_SUPPORTED_VERSIONS

from aaestro.core.agent import ComponentHealth, HealthState
from aaestro.core.errors import AaestroError, CircuitBreaker, ErrorCategory, ErrorCode
from aaestro.observability.circuit import CircuitTelemetry
from aaestro.protocols.mcp.errors import (
    startup_failed,
    tool_failed,
    transport_lost,
    unknown_tool,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from aaestro.core.manifest import McpServerSpec
    from aaestro.observability.metrics import Metrics

DEFAULT_STARTUP_TIMEOUT_S = 15.0
DEFAULT_TOOL_TIMEOUT_S = 60.0
DEFAULT_MAX_RECONNECTS = 3
RECONNECT_INITIAL_DELAY_S = 1.0
RECONNECT_MAX_DELAY_S = 30.0

SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = _SDK_SUPPORTED_VERSIONS
"""협상 가능한 MCP protocol version — 범위 밖이면 MCP_001 (silent degradation 금지).

목록을 따로 들고 있으면 SDK 가 새 버전을 협상할 때 조용히 갈라져, 정상 서버가
``MCP_001`` 로 거부된다. **SDK 가 정본**이고 그 버전은 lockfile 이 고정한다
(03 Version Pinning).
"""

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ToolResult:
    """One tool call result.

    tool 호출 결과. ``content`` 는 **신뢰하지 않는 입력**이다 — 그 안의 지시문을
    시스템 지시로 승격하지 않는다 (03 Security 6).
    """

    content: Any
    is_error: bool = False


@dataclass(frozen=True)
class Connection:
    """An established transport connection.

    수립된 전송 연결. 실제 SDK 세션은 ``handle`` 뒤에 감춰지고, 테스트는
    fake transport 로 대체한다.
    """

    tools: tuple[str, ...]
    protocol_version: str
    handle: Any = None
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    """tool 이름 → input schema. 이름만으로는 모델이 인자를 채울 수 없다."""


@runtime_checkable
class Transport(Protocol):
    """Connects to one MCP server and calls its tools.

    전송 계약. stdio/sidecar/external 3 패턴이 이 뒤에 구현된다.
    """

    async def connect(self, spec: McpServerSpec) -> Connection:
        """서버에 연결하고 tool 목록과 protocol version 을 보고한다."""
        ...

    async def call(
        self, connection: Connection, tool: str, arguments: Mapping[str, Any]
    ) -> ToolResult:
        """tool 을 실행한다."""
        ...

    async def close(self, connection: Connection) -> None:
        """연결과 자식 프로세스를 정리한다 — 좀비 금지."""
        ...


async def _sleep(delay: float) -> None:
    """재연결 backoff 대기 — 테스트는 이 함수를 주입으로 대체한다."""
    await asyncio.sleep(delay)


@dataclass
class McpSession:
    """One agent's session with one MCP server.

    에이전트 하나가 소유하는 MCP 서버 세션 하나.

    Attributes:
        spec: The server declaration from the manifest.
        transport: The transport implementation.
        agent: Owning agent name — every log and error carries it.
    """

    spec: McpServerSpec
    transport: Transport
    agent: str
    startup_timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S
    tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S
    max_reconnects: int = DEFAULT_MAX_RECONNECTS
    sleep: Callable[[float], Any] = _sleep
    breaker: CircuitBreaker | None = None
    metrics: Metrics | None = None

    _connection: Connection | None = field(default=None, init=False)
    _reconnect_failures: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.breaker is None:
            target = f"mcp:{self.spec.name}"
            observer = CircuitTelemetry(self.metrics, target=target) if self.metrics else None
            self.breaker = CircuitBreaker(
                target=target,
                open_category=ErrorCategory.MCP,
                open_code=ErrorCode.MCP_004,
                on_transition=observer.observe if observer else None,
            )

    @property
    def name(self) -> str:
        """서버 이름 — tool 네임스페이스에 쓰인다."""
        return self.spec.name

    @property
    def connected(self) -> bool:
        """세션이 살아있는지."""
        return self._connection is not None

    @property
    def tools(self) -> tuple[str, ...]:
        """바인딩된 tool 이름 — ``allowed_tools`` 필터가 적용된 결과."""
        if self._connection is None:
            return ()
        return self._filtered(self._connection.tools)

    @property
    def schemas(self) -> dict[str, dict[str, Any]]:
        """바인딩된 tool 의 input schema — ``tools`` 와 같은 필터가 적용된다.

        이름만으로는 모델이 인자를 채울 수 없다. 필터를 따로 적용하면 차단한
        tool 의 스키마가 새어 모델이 그것을 부르려 든다.
        """
        if self._connection is None:
            return {}
        allowed = set(self.tools)
        return {
            name: schema for name, schema in self._connection.schemas.items() if name in allowed
        }

    async def initialize(self) -> tuple[str, ...]:
        """Establish the session and collect its tools.

        세션을 수립하고 tool 목록을 수집합니다. 실패는 기동 자체를 막습니다
        (03 Startup Sequence — 부분 기동 금지).

        Returns:
            The bound tool names after ``allowed_tools`` filtering.

        Raises:
            AaestroError: MCP/``MCP_001`` on connect failure, timeout, or an
                unsupported protocol version.
        """
        try:
            connection = await asyncio.wait_for(
                self.transport.connect(self.spec), timeout=self.startup_timeout_s
            )
        except TimeoutError as err:
            raise startup_failed(
                self.agent, self.name, reason="startup timeout", timeout_s=self.startup_timeout_s
            ) from err
        except AaestroError:
            raise
        except Exception as err:
            raise startup_failed(self.agent, self.name, reason=type(err).__name__) from err

        if connection.protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            # 지원 범위 밖 버전으로 계속 가면 어긋난 동작이 조용히 퍼진다
            await self._close(connection)
            raise startup_failed(
                self.agent,
                self.name,
                reason="unsupported protocol version",
                protocol_version=connection.protocol_version,
            )

        self._connection = connection
        self._reconnect_failures = 0
        log.info(
            "mcp session initialized",
            agent=self.agent,
            mcp_server=self.name,
            protocol_version=connection.protocol_version,
        )
        return self.tools

    async def call_tool(self, tool: str, arguments: Mapping[str, Any]) -> ToolResult:
        """Call one tool on this server.

        이 서버의 tool 을 실행합니다. 전송이 끊겼으면 한 번 재연결한 뒤
        재시도합니다 — 재시도 계층은 여기 하나뿐입니다 (05 Retry Layering).

        Args:
            tool: Unqualified tool name (namespace is stripped by the caller).
            arguments: Tool arguments.

        Returns:
            The tool result — its content is untrusted input.

        Raises:
            AaestroError: MCP/``MCP_002`` if the tool is not bound,
                ``MCP_003`` on execution failure, ``MCP_004`` if the transport
                is gone and reconnection failed.
        """
        assert self.breaker is not None  # noqa: S101 — __post_init__ 가 보장
        if not self.breaker.can_attempt():
            raise transport_lost(self.agent, self.name, reason="circuit open")

        # 연결 여부를 tool 조회보다 먼저 본다. 단절 상태에서는 tools 가 비어 있어,
        # 순서를 바꾸면 복구 가능한 단절(MCP_004)이 tool 미존재(MCP_002, 재시도
        # 무의미)로 오분류되어 호출자가 재시도를 포기한다
        if self._connection is None:
            try:
                await self.reconnect()
                result = await self._call_once(tool, arguments)
            except AaestroError:
                self.breaker.record_failure()
                raise
            self.breaker.record_success()
            return result

        if tool not in self.tools:
            raise unknown_tool(self.agent, self.name, tool)

        try:
            result = await self._call_once(tool, arguments)
        except AaestroError as err:
            if err.code != ErrorCode.MCP_004:
                self.breaker.record_failure()
                raise
            # 단절은 재연결 후 1회만 재시도한다 (05 Retry Layering).
            # 재연결 자체의 실패도 breaker 에 반영해야 죽은 서버를 계속 두드리지 않는다
            try:
                await self.reconnect()
                result = await self._call_once(tool, arguments)
            except AaestroError:
                self.breaker.record_failure()
                raise

        self.breaker.record_success()
        return result

    async def _call_once(self, tool: str, arguments: Mapping[str, Any]) -> ToolResult:
        """단일 tool 호출 — 전송 예외를 코드로 변환한다."""
        connection = self._connection
        if connection is None:
            raise transport_lost(self.agent, self.name, reason="session not established")
        if tool not in self.tools:
            raise unknown_tool(self.agent, self.name, tool)

        try:
            return await asyncio.wait_for(
                self.transport.call(connection, tool, arguments), timeout=self.tool_timeout_s
            )
        except TimeoutError as err:
            raise AaestroError(
                category=ErrorCategory.TIMEOUT,
                code=ErrorCode.TO_002,
                message=f"mcp tool timed out: {tool}",
                agent=self.agent,
                details={"mcp_server": self.name, "tool": tool},
            ) from err
        except (ConnectionError, BrokenPipeError) as err:
            self._connection = None
            raise transport_lost(self.agent, self.name, reason=type(err).__name__) from err
        except AaestroError:
            raise
        except Exception as err:
            # 서버·프록시가 준 사유를 싣는다 — 도구 판정 거부(ACC_001)는 메시지에만 있다. 신뢰할 수
            # 없는 입력이므로 길이를 자르고 지시로 쓰지 않는다
            raise tool_failed(
                self.agent, self.name, tool, reason=type(err).__name__, detail=str(err)[:300]
            ) from err

    async def reconnect(self) -> None:
        """Re-establish the session with exponential backoff.

        지수 백오프로 세션을 다시 세웁니다. 누적 실패는 숨기지 않고
        ``MCP_004`` 로 드러내 unhealthy 판정에 반영합니다.

        Raises:
            AaestroError: MCP/``MCP_004`` when every attempt fails.
        """
        await self._close(self._connection)
        self._connection = None

        delay = RECONNECT_INITIAL_DELAY_S
        last: Exception | None = None
        for attempt in range(1, self.max_reconnects + 1):
            try:
                await self.initialize()
            except AaestroError as err:
                last = err
                log.warning(
                    "mcp reconnect failed",
                    agent=self.agent,
                    mcp_server=self.name,
                    attempt=attempt,
                    max_attempts=self.max_reconnects,
                    delay_ms=int(delay * 1000),
                    error_code=err.code,
                )
                if attempt < self.max_reconnects:
                    await self.sleep(delay)
                    delay = min(delay * 2, RECONNECT_MAX_DELAY_S)
                continue
            else:
                self._reconnect_failures = 0
                return

        self._reconnect_failures += 1
        raise transport_lost(
            self.agent, self.name, reason="reconnect exhausted", attempts=self.max_reconnects
        ) from last

    async def shutdown(self) -> None:
        """Tear down the session and its child process.

        세션과 자식 프로세스를 정리합니다 — 좀비 프로세스를 남기지 않습니다.
        """
        await self._close(self._connection)
        self._connection = None

    async def _close(self, connection: Connection | None) -> None:
        """전송 정리 — 실패해도 삼키고 로그만 남긴다 (정리 경로를 막지 않는다)."""
        if connection is None:
            return
        try:
            await self.transport.close(connection)
        except Exception as err:  # noqa: BLE001 — 정리 실패가 종료를 막으면 안 된다
            log.warning(
                "mcp session close failed",
                agent=self.agent,
                mcp_server=self.name,
                error=type(err).__name__,
            )

    def health(self) -> ComponentHealth:
        """세션 상태 — 재연결 실패가 누적되면 unhealthy."""
        if self.connected:
            return ComponentHealth(state=HealthState.HEALTHY)
        if self._reconnect_failures:
            return ComponentHealth(state=HealthState.UNHEALTHY, detail="reconnect exhausted")
        return ComponentHealth(state=HealthState.DEGRADED, detail="session not established")

    def _filtered(self, tools: Sequence[str]) -> tuple[str, ...]:
        """``allowed_tools`` 가 선언되면 그 목록만 남긴다 (03 Tool Filtering)."""
        if not self.spec.allowed_tools:
            return tuple(tools)
        allowed = set(self.spec.allowed_tools)
        return tuple(t for t in tools if t in allowed)


__all__ = [
    "DEFAULT_MAX_RECONNECTS",
    "DEFAULT_STARTUP_TIMEOUT_S",
    "DEFAULT_TOOL_TIMEOUT_S",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "Connection",
    "McpSession",
    "ToolResult",
    "Transport",
]
