"""A minimal MCP server for integration tests.

참조 서버를 네트워크에서 받아오지 않는다 — 저장소 안에 두면 CI 가 오프라인
이어도 stdio 세션을 실제로 검증할 수 있다.
"""

from __future__ import annotations

import asyncio

from mcp.server import MCPServer

server = MCPServer(name="aaestro-test-server", version="0.1.0")


def echo(text: str) -> str:
    """Echo the given text back.

    세션 왕복 검증이 목적이다 — 스키마는 시그니처에서 도출된다.
    """
    return text


server.add_tool(echo)


if __name__ == "__main__":
    asyncio.run(server.run_stdio_async())
