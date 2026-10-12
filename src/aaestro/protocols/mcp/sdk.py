"""MCP SDK transport bindings.

``StdioClient`` / ``HttpClient`` 계약 뒤에 공식 ``mcp`` SDK 를 바인딩한다.
SDK 의 async context manager 수명은 ``Connection.handle`` 뒤에 감춘다 —
세션이 자기 연결을 들고 있어야 호출과 정리가 같은 것을 가리킨다.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED

from aaestro.protocols.mcp.session import Connection, ToolResult

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass
class _Live:
    """열린 SDK 세션과 그 수명을 소유한 태스크.

    SDK 는 anyio 위에서 돌고, anyio 의 cancel scope 는 **연 태스크에서만**
    닫을 수 있다. 그래서 ``AsyncExitStack`` 을 호출자에게 넘기지 않고 전용
    태스크가 들고 있다가 신호를 받으면 스스로 닫는다 — 그러지 않으면 drain 이나
    shutdown 처럼 다른 태스크가 정리하는 경로에서 영원히 멈춘다.
    """

    session: ClientSession
    owner: asyncio.Task[None]
    stop: asyncio.Event
    protocol_version: str


async def _open(opener: Any) -> _Live:
    """전송을 열고 그 수명을 전용 태스크에 맡긴다.

    Args:
        opener: 전송 async context manager 를 만드는 콜러블.

    Returns:
        살아있는 세션 핸들.
    """
    ready: asyncio.Future[tuple[ClientSession, str]] = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()

    async def own() -> None:
        try:
            async with AsyncExitStack() as stack:
                streams = await stack.enter_async_context(opener())
                session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                initialized = await session.initialize()
                ready.set_result((session, str(initialized.protocol_version)))
                await stop.wait()
        except BaseException as err:  # noqa: BLE001 - 기동 실패를 호출자에게 전달
            if not ready.done():
                ready.set_exception(err)

    task = asyncio.create_task(own(), name="mcp-session")
    try:
        session, version = await ready
    except BaseException:
        # 부분 기동을 남기지 않는다 — 실패한 연결의 자식 프로세스를 회수할
        # 핸들이 사라지면 좀비가 된다
        stop.set()
        await task
        raise
    return _Live(session=session, owner=task, stop=stop, protocol_version=version)


def _to_connection(live: _Live) -> Connection:
    """SDK 세션을 프레임워크 표현으로 옮긴다."""
    return Connection(
        tools=(),
        protocol_version=live.protocol_version,
        handle=live,
    )


async def _load_tools(connection: Connection) -> Connection:
    """tool 이름과 **스키마**를 함께 읽는다.

    이름만 읽으면 모델이 인자를 채울 수 없다 — 스키마가 곧 계약이다.
    """
    live: _Live = connection.handle
    listed = await live.session.list_tools()
    return Connection(
        tools=tuple(tool.name for tool in listed.tools),
        protocol_version=connection.protocol_version,
        handle=live,
        schemas={tool.name: dict(tool.input_schema or {}) for tool in listed.tools},
    )


async def _call(connection: Connection, tool: str, arguments: Mapping[str, Any]) -> ToolResult:
    """SDK 로 tool 을 실행하고 결과를 프레임워크 표현으로 옮긴다."""
    live: _Live = connection.handle
    try:
        result = await live.session.call_tool(tool, dict(arguments))
    except MCPError as err:
        if err.code != CONNECTION_CLOSED:
            raise
        # 서버가 사라지면 SDK 는 전송 예외가 아니라 이 코드로 답한다 — 도구 실패(MCP_003)로 두면
        # 세션이 재연결하지 않고, 재연결 소진이 health 에 닿지 않는다 (#311)
        raise ConnectionError("mcp connection closed") from err
    return ToolResult(
        content=[_render_block(block) for block in getattr(result, "content", [])],
        is_error=bool(getattr(result, "is_error", False)),
    )


def _render_block(block: Any) -> Any:
    """결과 블록을 직렬화 가능한 형태로 — **신뢰하지 않는 입력**이다 (03 Security 6)."""
    text = getattr(block, "text", None)
    return text if text is not None else block.model_dump(mode="json")


async def _terminate(connection: Connection) -> None:
    """소유 태스크에 정지를 알리고 정리를 기다린다.

    스택을 여기서 직접 닫지 않는다 — anyio cancel scope 는 연 태스크에서만
    닫을 수 있어, 다른 태스크가 닫으면 영원히 멈춘다.
    """
    live: _Live | None = connection.handle
    if live is None:
        return
    live.stop.set()
    await live.owner


@dataclass
class SdkStdioClient:
    """The ``StdioClient`` implementation backed by the ``mcp`` SDK.

    자식 프로세스는 소유 에이전트 컨테이너 안에서만 돕니다 (03 Placement 1).
    """

    async def spawn(self, *, command: list[str], env: dict[str, str]) -> Connection:
        """Spawn the server process and initialize its session.

        서버 프로세스를 띄우고 세션을 initialize 합니다.

        Args:
            command: Executable and arguments — 이미지에 설치된 것만.
            env: Environment resolved from ``env_allowlist``.

        Returns:
            The established connection with its tool names and schemas.
        """
        live = await _open(
            lambda: stdio_client(
                StdioServerParameters(command=command[0], args=list(command[1:]), env=env)
            )
        )
        return await _load_tools(_to_connection(live))

    async def call(
        self, connection: Connection, tool: str, arguments: Mapping[str, Any]
    ) -> ToolResult:
        """tool 을 실행한다."""
        return await _call(connection, tool, arguments)

    async def terminate(self, connection: Connection) -> None:
        """세션 종료 후 자식 프로세스를 회수한다."""
        await _terminate(connection)


def _open_streamable_http(*, url: str, headers: dict[str, str]) -> Any:
    """streamable-http 전송을 연다.

    SDK 는 헤더를 직접 받지 않는다 — 인증 헤더는 ``http_client`` 에 실어야
    ``auth.token_env`` 선언이 실제로 전달된다 (03 서버 선언 스펙).
    """
    # SDK 의 공개 시그니처가 httpx2.AsyncClient 를 받는다 (mcp 2.x) — 프로젝트의 httpx 와 다른
    # 패키지라 바꿔 넣을 수 없다. 메이저 경계는 pyproject 가 고정하고, 시그니처가 바뀌면
    # test_sdk 의 계약 테스트가 먼저 깨진다 (#317)
    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    return streamable_http_client(url, http_client=httpx2.AsyncClient(headers=headers))


@dataclass
class SdkHttpClient:
    """The ``HttpClient`` implementation backed by the ``mcp`` SDK.

    사이드카와 external 서버 모두 이 경로를 씁니다 — 차이는 URL 뿐입니다.
    """

    _connect: Any = field(default=None, repr=False)

    async def connect(self, *, url: str, headers: dict[str, str]) -> Connection:
        """Connect to a remote server and initialize its session.

        원격 서버에 접속해 세션을 initialize 합니다.
        """
        opener = self._connect or _open_streamable_http
        live = await _open(lambda: opener(url=url, headers=headers))
        return await _load_tools(_to_connection(live))

    async def call(
        self, connection: Connection, tool: str, arguments: Mapping[str, Any]
    ) -> ToolResult:
        """tool 을 실행한다."""
        return await _call(connection, tool, arguments)

    async def disconnect(self, connection: Connection) -> None:
        """세션을 정리한다."""
        await _terminate(connection)


__all__ = ["SdkHttpClient", "SdkStdioClient"]
