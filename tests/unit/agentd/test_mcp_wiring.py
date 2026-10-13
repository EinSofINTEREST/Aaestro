"""agentd starts the MCP sessions its manifest declares (#282).

MCP 세션 배선이 없어 선언된 MCP 도구가 기동 때부터 광고도 실행도 되지 않았다. 세 층을 본다:
- 조립: 기동이 세션을 열고 도구를 광고·실행하며, 필수 서버 실패는 먼저 뜬 세션을 남기지 않는다
- 리로드: 떠 있는 세션을 다시 열지 않고 같은 도구 목록을 만든다
- 진입점: 조립과 서빙이 한 이벤트 루프에서 돌고, 끝나면 세션을 닫는다
"""

from __future__ import annotations

import asyncio

import pytest
import yaml

from aaestro.agentd import __main__ as agentd
from aaestro.agentd.mcp import build_mcp_client
from aaestro.core.errors import AaestroError
from aaestro.core.manifest import AgentManifest
from aaestro.core.skill import SkillContext
from aaestro.observability.metrics import Metrics
from aaestro.protocols.mcp.client import McpClient
from aaestro.protocols.mcp.transport import HttpTransport, StdioTransport, TransportSelector
from tests.fixtures.fake_mcp import FakeHttpClient, FakeStdioClient
from tests.unit.agentd.test_reload import planner, root  # noqa: F401 — fixture


def with_servers(*servers: dict) -> AgentManifest:
    doc = planner().model_dump(mode="json", by_alias=True, exclude_none=True)
    doc["spec"]["mcp"] = {"servers": list(servers)}
    return AgentManifest.model_validate(doc)


FS = {"name": "fs", "transport": "stdio", "command": ["mcp-server-fs"]}


class Launchers:
    """build_mcp_client 자리에 대역 전송을 끼운다 — 만든 클라이언트를 기억한다."""

    def __init__(self, stdio: FakeStdioClient, http: FakeHttpClient | None = None) -> None:
        self.stdio = stdio
        self.http = http or FakeHttpClient()
        self.built: list[McpClient] = []

    def __call__(self, manifest, *, metrics=None, environ=None) -> McpClient | None:
        if not manifest.spec.mcp.servers:
            return None
        client = McpClient(
            agent=manifest.name,
            transports=TransportSelector(
                stdio=StdioTransport(agent=manifest.name, client=self.stdio, environ={}),
                http=HttpTransport(agent=manifest.name, client=self.http, environ={}),
            ),
        )
        self.built.append(client)
        return client


def test_nothing_declared_builds_no_client():
    assert build_mcp_client(planner()) is None
    assert isinstance(build_mcp_client(with_servers(FS), environ={}), McpClient)


async def test_declared_tools_are_advertised_and_run_from_startup(root, monkeypatch):  # noqa: F811
    launchers = Launchers(FakeStdioClient(tools=["read_file"]))
    monkeypatch.setattr(agentd, "build_mcp_client", launchers)

    executor = await agentd.build_executor(with_servers(FS))

    assert executor.binding.tools.mcp is launchers.built[0]
    assert "mcp__fs__read_file" in {spec.name for spec in executor.tool_schemas}
    ctx = SkillContext(agent="planner", task_id="t", run_id="r")
    result = await executor.binding.tools.call("mcp__fs__read_file", {}, ctx)
    assert result.content == "read_file"
    await launchers.built[0].shutdown()


async def test_reload_keeps_the_live_session_and_the_same_tools(root, monkeypatch):  # noqa: F811
    stdio = FakeStdioClient(tools=["read_file"])
    launchers = Launchers(stdio)
    monkeypatch.setattr(agentd, "build_mcp_client", launchers)
    manifest = with_servers(FS)
    executor = await agentd.build_executor(manifest)
    before = [spec.name for spec in executor.tool_schemas]

    await agentd.build_reload(manifest, executor)()

    assert len(stdio.spawned) == 1, "리로드가 떠 있는 MCP 서버를 다시 띄웠다"
    assert executor.binding.tools.mcp is launchers.built[0]
    assert [spec.name for spec in executor.tool_schemas] == before
    await launchers.built[0].shutdown()


async def test_a_failed_required_server_leaves_no_session_behind(root, monkeypatch):  # noqa: F811
    stdio = FakeStdioClient(tools=["read_file"])

    class Refusing(FakeHttpClient):
        async def connect(self, *, url, headers):
            raise ConnectionError("remote server down")

    launchers = Launchers(stdio, Refusing())
    monkeypatch.setattr(agentd, "build_mcp_client", launchers)
    remote = {"name": "corp", "transport": "streamable-http", "url": "https://mcp.example/mcp"}

    with pytest.raises(AaestroError) as exc_info:
        await agentd.build_executor(with_servers(FS, remote))

    assert exc_info.value.code == "MCP_001"
    assert stdio.terminated == 1, "먼저 뜬 stdio 서버가 좀비로 남았다"


def test_the_daemon_builds_and_serves_on_one_loop_and_closes_sessions(
    root,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    stdio = FakeStdioClient(tools=["read_file"])
    launchers = Launchers(stdio)
    monkeypatch.setattr(agentd, "build_mcp_client", launchers)
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(with_servers(FS).model_dump(mode="json", by_alias=True, exclude_none=True)),
        encoding="utf-8",
    )
    monkeypatch.setenv(agentd.MANIFEST_ENV, str(manifest_path))
    monkeypatch.setenv(agentd.TOKEN_ENV, "agent-token")
    monkeypatch.setattr(agentd, "_setup_observability", Metrics)
    monkeypatch.setattr(agentd, "_configure_logging", lambda: None)
    loops = {}
    original = agentd.build_executor

    async def build(manifest, **kwargs):
        loops["built"] = asyncio.get_running_loop()
        return await original(manifest, **kwargs)

    async def serve(app, manifest, executor, bind_host):
        loops["served"] = asyncio.get_running_loop()
        loops["live_while_serving"] = executor.binding.tools.mcp.sessions["fs"].connected

    monkeypatch.setattr(agentd, "build_executor", build)
    monkeypatch.setattr(agentd, "_serve", serve)

    agentd.main()

    assert loops["built"] is loops["served"], "세션을 연 루프가 닫힌 뒤에 서빙한다"
    assert loops["live_while_serving"] is True
    assert stdio.terminated == 1, "종료 시 MCP 세션을 닫지 않았다"


def test_an_agent_without_mcp_servers_does_not_load_the_mcp_sdk():
    """SDK import 만으로 기동이 늘어 health 창을 넘기면 runtime 이 뜨는 컨테이너를 재시작한다."""
    import subprocess
    import sys

    probe = (
        "import sys, aaestro.agentd.__main__;"
        "print(any(m == 'mcp' or m.startswith('mcp.') for m in sys.modules))"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()

    assert out == "False"


async def test_a_failure_after_the_modules_load_still_closes_the_sessions(
    root,  # noqa: F811
    monkeypatch,
):
    """세션이 열린 뒤 실행기를 만들다 실패해도 자식 프로세스를 남기지 않는다 (#298 리뷰)."""
    stdio = FakeStdioClient(tools=["read_file"])
    monkeypatch.setattr(agentd, "build_mcp_client", Launchers(stdio))
    monkeypatch.setattr(
        agentd, "_telemetry_for", lambda *a: (_ for _ in ()).throw(RuntimeError("x"))
    )

    with pytest.raises(RuntimeError):
        await agentd.build_executor(with_servers(FS))

    assert stdio.terminated == 1


def test_a_failure_while_assembling_the_app_closes_the_sessions(
    root,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    stdio = FakeStdioClient(tools=["read_file"])
    monkeypatch.setattr(agentd, "build_mcp_client", Launchers(stdio))
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(with_servers(FS).model_dump(mode="json", by_alias=True, exclude_none=True)),
        encoding="utf-8",
    )
    monkeypatch.setenv(agentd.MANIFEST_ENV, str(manifest_path))
    monkeypatch.setenv(agentd.TOKEN_ENV, "agent-token")
    monkeypatch.setattr(agentd, "_setup_observability", Metrics)
    monkeypatch.setattr(agentd, "_configure_logging", lambda: None)

    def broken(*args, **kwargs):
        raise RuntimeError("app assembly failed")

    monkeypatch.setattr(agentd, "build_app", broken)

    with pytest.raises(RuntimeError):
        agentd.main()

    assert stdio.terminated == 1


async def test_an_optional_server_that_failed_at_startup_shows_as_degraded(root, monkeypatch):  # noqa: F811
    """생성자가 묶음의 일부만 받으면 degraded 가 빠져, 리로드 전까지 healthy 로 답한다."""
    from aaestro.agentd.health import agent_health
    from aaestro.core.agent import HealthState

    class Refusing(FakeHttpClient):
        async def connect(self, *, url, headers):
            raise ConnectionError("remote server down")

    launchers = Launchers(FakeStdioClient(tools=["read_file"]), Refusing())
    monkeypatch.setattr(agentd, "build_mcp_client", launchers)
    remote = {
        "name": "corp",
        "transport": "streamable-http",
        "url": "https://mcp.example/mcp",
        "optional": True,
    }

    executor = await agentd.build_executor(with_servers(FS, remote))

    status = agent_health(executor)
    assert status.status is HealthState.DEGRADED
    assert status.components["mcp:corp"].state is HealthState.DEGRADED
    await launchers.built[0].shutdown()


async def test_shutdown_closes_the_memory_client_too():
    """열린 Memory Service 커넥션을 두고 끝내지 않는다 (#321)."""
    from types import SimpleNamespace

    closed = []

    class Memory:
        async def aclose(self):
            closed.append("memory")

    executor = SimpleNamespace(
        binding=SimpleNamespace(tools=SimpleNamespace(mcp=None, memory=Memory()))
    )

    await agentd._close_wiring(executor)

    assert closed == ["memory"]


async def test_shutdown_without_memory_wiring_is_quiet():
    from types import SimpleNamespace

    executor = SimpleNamespace(
        binding=SimpleNamespace(tools=SimpleNamespace(mcp=None, memory=None))
    )

    await agentd._close_wiring(executor)
