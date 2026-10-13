"""The MCP client this agent's declarations call for (#282).

선언된 MCP 서버의 세션을 소유할 클라이언트를 조립한다. 기동과 E2E 가 같은 조립을 탄다 — 두 벌이면
검증한 경로와 실제 경로가 갈린다.

MCP SDK 는 **선언한 에이전트만** 불러온다 — import 만으로 기동이 0.5초 넘게 늘어, 기동 중 health
확인 창을 넘기면 runtime 이 아직 뜨는 중인 컨테이너를 재시작한다.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aaestro.core.manifest import AgentManifest
    from aaestro.observability.metrics import Metrics
    from aaestro.protocols.mcp.client import McpClient


def build_mcp_client(
    manifest: AgentManifest,
    *,
    metrics: Metrics | None = None,
    environ: Mapping[str, str] | None = None,
) -> McpClient | None:
    """The client owning this agent's MCP sessions — None when nothing is declared."""
    if not manifest.spec.mcp.servers:
        return None
    from aaestro.access.model import ACCESS_CREDENTIAL_ENV
    from aaestro.protocols.mcp.client import McpClient
    from aaestro.protocols.mcp.sdk import SdkHttpClient, SdkStdioClient
    from aaestro.protocols.mcp.transport import (
        MCP_PROXY_URL_ENV,
        HttpTransport,
        StdioTransport,
        TransportSelector,
    )

    env = os.environ if environ is None else environ
    agent = manifest.name
    return McpClient(
        agent=agent,
        transports=TransportSelector(
            stdio=StdioTransport(agent=agent, client=SdkStdioClient(), environ=env),
            http=HttpTransport(
                agent=agent,
                client=SdkHttpClient(),
                environ=env,
                # 이그레스 프록시가 켜진 배포 — 원격 서버는 프록시가 종단하고 자격을 붙인다 (#282)
                proxy_url=env.get(MCP_PROXY_URL_ENV) or None,
                identity=env.get(ACCESS_CREDENTIAL_ENV) or None,
            ),
        ),
        metrics=metrics,
    )


__all__ = ["build_mcp_client"]
