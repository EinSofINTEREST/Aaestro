"""Real MCP stdio session over the SDK binding.

저장소 안의 최소 서버를 실제로 띄운다 — 참조 서버를 네트워크에서 받지 않으므로
오프라인 CI 에서도 세션 왕복이 검증된다.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from aaestro.core.manifest import McpServerSpec
from aaestro.protocols.mcp.sdk import SdkStdioClient
from aaestro.protocols.mcp.session import SUPPORTED_PROTOCOL_VERSIONS, McpSession
from aaestro.protocols.mcp.transport import StdioTransport

pytestmark = pytest.mark.integration

REPO_ROOT = str(Path(__file__).resolve().parents[3])
SERVER_COMMAND = [sys.executable, "-m", "tests.fixtures.mcp_server"]


def spec(**overrides) -> McpServerSpec:
    base = {
        "name": "testserver",
        "transport": "stdio",
        "command": SERVER_COMMAND,
    }
    base.update(overrides)
    return McpServerSpec.model_validate(base)


@pytest.fixture
async def connection():
    """실제 서버 프로세스와의 연결 — finalizer 가 자식을 회수한다."""
    client = SdkStdioClient()
    established = await asyncio.wait_for(
        client.spawn(command=SERVER_COMMAND, env={**os.environ, "PYTHONPATH": REPO_ROOT}),
        timeout=30,
    )
    try:
        yield client, established
    finally:
        await client.terminate(established)


async def test_session_lists_tools_with_their_schemas(connection):
    """이름만 읽으면 모델이 인자를 채울 수 없다."""
    _client, established = connection

    assert established.tools == ("echo",)
    assert established.schemas["echo"]["required"] == ["text"]
    assert established.schemas["echo"]["properties"]["text"]["type"] == "string"


async def test_negotiated_version_is_one_we_accept(connection):
    """SDK 가 협상한 버전을 우리가 거부하면 정상 서버가 MCP_001 로 막힌다."""
    _client, established = connection

    assert established.protocol_version in SUPPORTED_PROTOCOL_VERSIONS


async def test_tool_call_round_trips(connection):
    client, established = connection

    result = await client.call(established, "echo", {"text": "hello"})

    assert result.content == ["hello"]
    assert not result.is_error


async def test_session_binds_the_real_server():
    """세션 계층까지 실제 서버로 확인한다 — 대역이 가리는 부분이 없도록."""
    session = McpSession(
        spec=spec(),
        transport=StdioTransport(
            agent="researcher",
            client=SdkStdioClient(),
            environ={**os.environ, "PYTHONPATH": REPO_ROOT},
        ),
        agent="researcher",
    )

    tools = await asyncio.wait_for(session.initialize(), timeout=30)
    try:
        assert tools == ("echo",)
        assert session.schemas["echo"]["required"] == ["text"]
        result = await session.call_tool("echo", {"text": "via session"})
        assert result.content == ["via session"]
    finally:
        await session.shutdown()


async def test_allowed_tools_filter_hides_the_schema_too():
    """차단한 tool 의 스키마가 새면 모델이 그것을 부르려 든다."""
    session = McpSession(
        spec=spec(allowed_tools=["nothing"]),
        transport=StdioTransport(
            agent="researcher",
            client=SdkStdioClient(),
            environ={**os.environ, "PYTHONPATH": REPO_ROOT},
        ),
        agent="researcher",
    )

    await asyncio.wait_for(session.initialize(), timeout=30)
    try:
        assert session.tools == ()
        assert session.schemas == {}
    finally:
        await session.shutdown()
