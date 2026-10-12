"""Integration tests for MCP sessions against a real server process.

실제 자식 프로세스를 띄워 stdio 세션의 수립/호출/단절/재연결을 검증한다.
Docker 도 외부 서비스도 필요 없다 — 참조 서버는 이 파일이 spawn 하는
파이썬 스크립트다 (결정적이고, 오류·지연 시나리오를 스크립트할 수 있다).
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from aaestro.core.errors import AaestroError
from aaestro.core.manifest import McpServerSpec
from aaestro.protocols.mcp.session import Connection, McpSession, ToolResult

pytestmark = pytest.mark.integration

SERVER_SCRIPT = """
import json, os, sys, time

# __die__ / __slow__ 는 시나리오 제어용 tool — 서버가 광고해야 세션이 바인딩한다
TOOLS = ["read_file", "list_directory", "__die__", "__slow__"]

def main():
    sys.stdout.write(json.dumps({"protocol_version": "2025-06-18", "tools": TOOLS}) + "\\n")
    sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        if request.get("tool") == "__die__":
            sys.exit(1)                       # 단절 시나리오
        if request.get("tool") == "__slow__":
            time.sleep(float(os.environ.get("SLOW_SECONDS", "5")))
        sys.stdout.write(json.dumps({"content": f"ran:{request.get('tool')}"}) + "\\n")
        sys.stdout.flush()

main()
"""


@dataclass
class ProcessStdioTransport:
    """Spawns the reference server as a child process.

    참조 서버를 자식 프로세스로 띄우는 전송 — 실제 프로세스 경계를 넘는다.
    """

    script: Path
    env: dict[str, str] = field(default_factory=dict)
    processes: list[asyncio.subprocess.Process] = field(default_factory=list)

    async def connect(self, spec: McpServerSpec) -> Connection:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(self.script),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", **self.env},
        )
        self.processes.append(process)
        assert process.stdout is not None
        handshake = json.loads(await process.stdout.readline())
        return Connection(
            tools=tuple(handshake["tools"]),
            protocol_version=handshake["protocol_version"],
            handle=process,
        )

    async def call(self, connection: Connection, tool: str, arguments: Any) -> ToolResult:
        process: asyncio.subprocess.Process = connection.handle
        assert process.stdin is not None and process.stdout is not None

        if process.returncode is not None:
            raise ConnectionError("server process is gone")

        process.stdin.write((json.dumps({"tool": tool}) + "\n").encode())
        await process.stdin.drain()

        line = await process.stdout.readline()
        if not line:  # EOF — 프로세스가 죽었다
            raise ConnectionError("server closed the stream")
        return ToolResult(content=json.loads(line)["content"])

    async def close(self, connection: Connection) -> None:
        process: asyncio.subprocess.Process = connection.handle
        if process.returncode is None:
            process.terminate()
        await process.wait()


@pytest.fixture
def server_script(tmp_path: Path) -> Path:
    """참조 MCP 서버 스크립트."""
    path = tmp_path / "reference_server.py"
    path.write_text(SERVER_SCRIPT, encoding="utf-8")
    return path


@pytest.fixture
async def transport(server_script: Path):
    """전송 — finalizer 가 남은 자식 프로세스를 반드시 회수한다."""
    transport = ProcessStdioTransport(script=server_script)
    try:
        yield transport
    finally:
        for process in transport.processes:
            if process.returncode is None:
                process.kill()
            await process.wait()


def spec(**overrides: Any) -> McpServerSpec:
    base = {"name": "reference", "transport": "stdio", "command": ["python"]}
    base.update(overrides)
    return McpServerSpec.model_validate(base)


def make_session(transport: ProcessStdioTransport, **overrides: Any) -> McpSession:
    async def sleep(_delay: float) -> None:
        """재연결 대기를 즉시 넘긴다 — 실제 시간을 쓰지 않는다."""

    return McpSession(
        spec=overrides.pop("spec", spec()),
        transport=transport,
        agent="researcher",
        sleep=sleep,
        **overrides,
    )


async def test_session_establishes_against_a_real_process(transport):
    """실제 프로세스와 핸드셰이크해 tool 목록을 받는다."""
    session = make_session(transport)

    tools = await session.initialize()

    assert tools[:2] == ("read_file", "list_directory")
    assert transport.processes[0].returncode is None


async def test_tool_call_crosses_the_process_boundary(transport):
    session = make_session(transport)
    await session.initialize()

    result = await session.call_tool("read_file", {"path": "a.txt"})

    assert result.content == "ran:read_file"


async def test_allowed_tools_filters_a_real_server(transport):
    session = make_session(transport, spec=spec(allowed_tools=["read_file"]))

    tools = await session.initialize()

    assert tools == ("read_file",)
    with pytest.raises(AaestroError) as exc_info:
        await session.call_tool("list_directory", {})
    assert exc_info.value.code == "MCP_002"


async def test_server_death_is_detected_and_reconnected(transport):
    """서버가 죽으면 재연결해 호출을 이어간다 — 단절이 곧 실패가 아니다."""
    session = make_session(transport)
    await session.initialize()

    with pytest.raises(AaestroError):
        await session.call_tool("__die__", {})

    # 죽은 뒤의 호출은 재연결을 거쳐 성공한다
    result = await session.call_tool("read_file", {})

    assert result.content == "ran:read_file"
    # 최초 기동 + __die__ 직후 재연결 + 그 재연결로 살아난 세션에서의 성공.
    # 죽은 프로세스가 재사용되지 않는 것이 요점이다
    assert len(transport.processes) >= 2
    assert transport.processes[0].returncode is not None


async def test_slow_tool_hits_the_timeout(transport):
    """지연 시나리오 — tool timeout 이 걸린다."""
    session = make_session(transport, tool_timeout_s=0.2)
    await session.initialize()

    with pytest.raises(AaestroError) as exc_info:
        await session.call_tool("__slow__", {})

    assert exc_info.value.code == "TO_002"


async def test_shutdown_reaps_the_child_process(transport):
    """좀비 프로세스를 남기지 않는다."""
    session = make_session(transport)
    await session.initialize()
    process = transport.processes[0]

    await session.shutdown()

    assert process.returncode is not None
