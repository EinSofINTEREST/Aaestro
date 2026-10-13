"""Egress through the proxy, decided per destination (#293) and per remote MCP tool (#282), with no
other way out (#280). Agent-owned MCP sidecars are reached the same way (#304).

떠 있는 에이전트 컨테이너 **안에서** 프록시를 거쳐 나간다 — 배포가 넣어 준 ``HTTPS_PROXY`` 그대로.

- 모델 API: 에이전트 env 에는 키가 없고, 대역 provider 는 프록시가 붙인 키를 받는다
- CONNECT: 선언한 목적지는 통과, 회수하면 컨테이너를 건드리지 않고 다음 CONNECT 부터 거부
- 레지스트리가 멈추면 캐시에 있던 허용은 유지, 처음 묻는 목적지는 거부
- record 모드는 선언 밖 목적지를 거부 판정으로 기록하되 통과시킨다

에이전트는 외부 경로가 없는 내부 네트워크(``AGENTS``)에만 있다. 프록시·Memory Service·control plane
포워더만 그 네트워크와 스택 네트워크에 함께 붙는다 — 대역 provider 는 스택 네트워크에만 있으므로
에이전트가 닿는 길은 프록시뿐이다.

대역 provider 는 사설 네트워크에 있으므로 프록시에 사설 목적지로 명시한다. 선언은 저장소를 복사한
작업 공간에서 researcher 에게만 ``fake-provider:8000`` 을 더한다 — 참조 에이전트는 바꾸지 않는다.
"""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.e2e.conftest import (
    CONTROL_PORT,
    NETWORK,
    REPO_ROOT,
    api,
    deployed_containers,
    memory_tokens,
    plane_healthy,
    start_plane,
    stop,
    until,
    write_config,
)
from tests.e2e.test_a2a_access import CALL as PEER_CALL
from tests.e2e.test_a2a_access import FORWARD, FORWARDER, run_in
from tests.e2e.test_memory_access import CALL as MEMORY_CALL
from tests.e2e.test_memory_access import MEMORY, MEMORY_ALIAS, memory_ready, start_memory
from tests.e2e.test_stack import docker, requires_docker

pytestmark = [pytest.mark.e2e, requires_docker]

ENFORCER_TOKEN = "e2e-enforcer-token"  # noqa: S105 — test_memory_access 의 Memory Service 와 같은 값
PROXY = "aaestro-e2e-egress"
PROXY_KEY = "e2e-proxy-held-key"
TARGET = "fake-provider:8000"
UNDECLARED = f"{MEMORY_ALIAS}:8090"
RESEARCHER, PLANNER = "aaestro-researcher-0", "aaestro-planner-0"
MCP_SERVER = "aaestro-e2e-fake-mcp"
MCP_TARGET = "fake-mcp:8000"
MCP_TOKEN = "e2e-remote-mcp-token"  # noqa: S105 — 프록시만 쥐는 테스트 값
MCP_TOKEN_ENV = "CORP_MCP_TOKEN"  # noqa: S105 — 키 이름이다

# 원격 MCP 서버 대역 — 프록시가 쥔 자격이 없으면 401. 스택 네트워크에만 있다
FAKE_MCP = f"""
import uvicorn
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

server = MCPServer(name="corp", version="0.1.0")

def echo(text: str) -> str:
    \"\"\"Echo the text back.\"\"\"
    return text

def add(a: int, b: int) -> int:
    \"\"\"Add two numbers.\"\"\"
    return a + b

server.add_tool(echo)
server.add_tool(add)
inner = server.streamable_http_app(
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

async def app(scope, receive, send):
    if scope["type"] == "http":
        headers = dict(scope["headers"])
        if headers.get(b"authorization") != b"Bearer {MCP_TOKEN}":
            await send({{"type": "http.response.start", "status": 401, "headers": []}})
            await send({{"type": "http.response.body", "body": b"credential required"}})
            return
    await inner(scope, receive, send)

uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
"""

# 컨테이너 안에서 agentd 의 조립 그대로 **한 세션**을 열어 두고, 줄마다 도구를 부른다 — 회수 전후가
# 같은 세션이어야 세션을 열 때만 판정하는 회귀를 잡는다. 결과 또는 에러 코드·사유를 한 줄로 답한다
MCP_SESSION = """
import asyncio, json, os, sys
import yaml
from aaestro.agentd.mcp import build_mcp_client
from aaestro.core.errors import AaestroError
from aaestro.core.manifest import AgentManifest

async def main():
    with open(os.environ["AAESTRO_MANIFEST"], encoding="utf-8") as handle:
        manifest = AgentManifest.model_validate(yaml.safe_load(handle))
    client = build_mcp_client(manifest)
    server = sys.argv[1]
    [spec] = [s for s in manifest.spec.mcp.servers if s.name == server]
    await client.start(spec)
    print("@@READY", flush=True)
    loop = asyncio.get_running_loop()
    try:
        while line := await loop.run_in_executor(None, sys.stdin.readline):
            tool, arguments = line.rstrip("\\n").split(" ", 1)
            try:
                result = await client.call_tool(f"mcp__{server}__{tool}", json.loads(arguments))
                print("@@OK:" + json.dumps(result.content), flush=True)
            except AaestroError as err:
                detail = str(err.details.get("detail", ""))[:200]
                print("@@ERR:" + err.code + ":" + detail, flush=True)
    finally:
        await client.shutdown()

asyncio.run(main())
"""
SIDECAR_IMAGE = "aaestro/e2e-mcp-sidecar:0.1.0"
SIDECAR = "aaestro-researcher--mcp-tools"
SIDECAR_NET = "aaestro-researcher--mcp"

# 사이드카 이미지의 서버 — 자격 없이 듣는다. 닿는 길은 사이드카 네트워크의 프록시뿐이어야 한다
SIDECAR_MCP = """
import uvicorn
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

server = MCPServer(name="tools", version="0.1.0")

def echo(text: str) -> str:
    \"\"\"Echo the text back.\"\"\"
    return text

def add(a: int, b: int) -> int:
    \"\"\"Add two numbers.\"\"\"
    return a + b

server.add_tool(echo)
server.add_tool(add)
app = server.streamable_http_app(
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
"""

AGENTS = "aaestro-e2e-agents"
"""에이전트 네트워크 — ``--internal``. 이그레스 프록시를 켠 control plane 은 이 격리를 요구한다."""

# 컨테이너 안에서 HTTPS_PROXY 로 CONNECT 를 열고 터널로 GET 한다 — 상태 코드(또는 거부 코드)만 출력
TUNNEL = """
import base64, http.client, os, re, sys, urllib.parse
proxy = urllib.parse.urlsplit(os.environ["HTTPS_PROXY"])
user = urllib.parse.unquote(proxy.username) + ":" + urllib.parse.unquote(proxy.password)
auth = "Basic " + base64.b64encode(user.encode()).decode()
host, port = sys.argv[1].rsplit(":", 1)
conn = http.client.HTTPConnection(proxy.hostname, proxy.port, timeout=15)
conn.set_tunnel(host, int(port), headers={"Proxy-Authorization": auth})
try:
    conn.request("GET", "/healthz")
    print(conn.getresponse().status)
except OSError as err:
    found = re.search(r"(\\d{3})", str(err))
    print(found.group(1) if found else "ERR")
"""


def build_sidecar_image(tmp_path: Path) -> None:
    """사이드카는 이미지의 기본 명령으로 뜬다 — runtime 은 명령을 주지 않는다."""
    context = tmp_path / "sidecar-image"
    context.mkdir()
    (context / "server.py").write_text(SIDECAR_MCP, encoding="utf-8")
    (context / "Dockerfile").write_text(
        "FROM aaestro/agent-base:0.1.0\n"
        "COPY --chown=1000:1000 server.py /app/sidecar/server.py\n"
        'ENTRYPOINT ["python", "/app/sidecar/server.py"]\n',
        encoding="utf-8",
    )
    docker("build", "-t", SIDECAR_IMAGE, str(context), timeout=900)


def remove_sidecars() -> None:
    docker("rm", "-f", SIDECAR, check=False)
    docker("network", "rm", SIDECAR_NET, check=False)


def build_proxy_image() -> None:
    docker(
        "build", "-t", "aaestro/egress-proxy:0.1.0",
        "-f", str(REPO_ROOT / "deployments" / "docker" / "egress-proxy.Dockerfile"), str(REPO_ROOT),
        timeout=900,
    )  # fmt: skip


def agents_network() -> None:
    """외부 경로 없는 에이전트 네트워크를 새로 만든다 — 남은 것이 격리가 아닐 수 있다."""
    for name in (PROXY, MEMORY, FORWARDER, MCP_SERVER, *deployed_containers()):
        docker("rm", "-f", name, check=False)
    remove_sidecars()
    docker("network", "rm", AGENTS, check=False)
    docker("network", "create", "--internal", AGENTS)


def start_forwarder() -> None:
    """에이전트 네트워크 안의 ``control-plane`` — 호스트의 control plane 으로 잇는다."""
    docker("rm", "-f", FORWARDER, check=False)
    docker(
        "run", "-d", "--name", FORWARDER,
        "--network", NETWORK, "--add-host", "host.docker.internal:host-gateway",
        "--read-only", "--user", "1000:1000", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--entrypoint", "python", "aaestro/memory-service:0.1.0", "-c", FORWARD,
    )  # fmt: skip
    docker("network", "connect", "--alias", "control-plane", AGENTS, FORWARDER)


def start_fake_mcp() -> None:
    docker("rm", "-f", MCP_SERVER, check=False)
    docker(
        "run", "-d", "--name", MCP_SERVER,
        "--network", NETWORK, "--network-alias", "fake-mcp",
        "--read-only", "--user", "1000:1000", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--entrypoint", "python", "aaestro/agent-base:0.1.0", "-c", FAKE_MCP,
    )  # fmt: skip


def fake_mcp_ready() -> bool:
    script = (
        "import urllib.request,urllib.error\n"
        "try: urllib.request.urlopen('http://fake-mcp:8000/mcp', timeout=3)\n"
        "except urllib.error.HTTPError as e: print(e.code)\n"
        "except OSError: print('down')"
    )
    return docker("exec", MEMORY, "python", "-c", script, check=False) == "401"


def start_proxy(
    root: Path, mode: str = "enforce", *, extra_env: dict[str, str] | None = None
) -> None:
    docker("rm", "-f", PROXY, check=False)
    extra = [arg for key, value in (extra_env or {}).items() for arg in ("-e", f"{key}={value}")]
    docker(
        "run", "-d", "--name", PROXY,
        "--network", NETWORK,
        "--add-host", "host.docker.internal:host-gateway",
        "--read-only", "--user", "1000:1000", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "-e", f"AAESTRO_ACCESS_URL=http://host.docker.internal:{CONTROL_PORT}",
        "-e", f"AAESTRO_ACCESS_ENFORCER_TOKEN={ENFORCER_TOKEN}",
        "-e", f"AAESTRO_EGRESS_MODE={mode}",
        "-e", f"AAESTRO_EGRESS_PRIVATE_DESTINATIONS={TARGET},{UNDECLARED},{MCP_TARGET}",
        # 원격 MCP 종단 — 서버 주소는 선언에서, 자격은 허용한 이름만 (#282)
        "-v", f"{root}:/repo:ro", "-e", "AAESTRO_REPO_ROOT=/repo",
        "-e", f"AAESTRO_EGRESS_MCP_TOKENS={MCP_TOKEN_ENV}", "-e", f"{MCP_TOKEN_ENV}={MCP_TOKEN}",
        "-e", "AAESTRO_EGRESS_ANTHROPIC_UPSTREAM=http://fake-provider:8000",
        "-e", "AAESTRO_EGRESS_ALLOW_PLAINTEXT_UPSTREAM=true",  # 대역 provider 는 평문이다
        "-e", f"ANTHROPIC_API_KEY={PROXY_KEY}",
        *extra,
        "aaestro/egress-proxy:0.1.0",
    )  # fmt: skip
    docker("network", "connect", "--alias", "aaestro-egress", AGENTS, PROXY)


def proxy_ready() -> bool:
    return docker("inspect", "-f", "{{.State.Health.Status}}", PROXY, check=False) == "healthy"


def workspace(tmp_path: Path) -> Path:
    """저장소 선언을 복사하고 researcher 에게만 대역 provider 목적지를 선언한다."""
    root = tmp_path / "repo"
    for name in ("agents", "graphs", "groups", "modules"):
        shutil.copytree(REPO_ROOT / name, root / name, ignore=shutil.ignore_patterns("__pycache__"))
    for path in [root, *root.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    manifest_path = root / "agents" / "researcher" / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["spec"]["runtime"]["egress"] = [TARGET]
    manifest["spec"]["runtime"]["env_allowlist"] = [
        *manifest["spec"]["runtime"].get("env_allowlist", []),
        MCP_TOKEN_ENV,
    ]
    manifest["spec"]["mcp"] = {
        "servers": [
            {
                "name": "corp",
                "transport": "streamable-http",
                "url": f"http://{MCP_TARGET}/mcp",
                "auth": {"type": "bearer", "token_env": MCP_TOKEN_ENV},
            },
            # 에이전트 전용 사이드카 (#304) — 주소는 적지 않는다
            {
                "name": "tools",
                "transport": "streamable-http",
                "sidecar": {
                    "image": SIDECAR_IMAGE,
                    "resources": {"cpu": "0.5", "memory": "256Mi"},
                },
            },
        ]
    }
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    # 자격은 스코프에 선언돼야 해석된다 — research 그룹 비밀로 둔다
    group_path = root / "groups" / "research.yaml"
    group = yaml.safe_load(group_path.read_text(encoding="utf-8"))
    group["spec"]["secrets"] = [*group["spec"].get("secrets", []), MCP_TOKEN_ENV]
    group_path.write_text(yaml.safe_dump(group, sort_keys=False), encoding="utf-8")
    return root


@pytest.fixture
def plane(stack, tmp_path) -> Iterator[dict[str, Any]]:
    build_proxy_image()
    build_sidecar_image(tmp_path)
    config_dir = write_config(
        tmp_path,
        orchestrator={
            "control_host": "0.0.0.0",  # noqa: S104 — 프록시·Memory Service 컨테이너가 닿아야 한다
            "access_store": str(tmp_path / "access.db"),
            "access_enforcer_token": ENFORCER_TOKEN,
            "access_agent_url": "http://control-plane:8700",
        },
    )
    config = yaml.safe_load((config_dir / "e2e.yaml").read_text(encoding="utf-8"))
    config["runtime"]["network"] = AGENTS
    config["runtime"]["egress_proxy"] = {
        "connect_url": "http://aaestro-egress:8080",
        "providers_url": "http://aaestro-egress:8081",
        # 사이드카 네트워크에 붙일 프록시 컨테이너 (#304)
        "container": PROXY,
    }
    (config_dir / "e2e.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    tokens_path = tmp_path / "memory.json"
    tokens_path.write_text(json.dumps(memory_tokens()), encoding="utf-8")
    root = workspace(tmp_path)
    env = {
        "AAESTRO_MEMORY_URL": f"http://{MEMORY_ALIAS}:8090",
        "AAESTRO_REPO_ROOT": str(root),
        # 선언 검증이 자격을 해석할 수 있어야 한다 — 에이전트 env 로는 나가지 않는다
        MCP_TOKEN_ENV: MCP_TOKEN,
    }
    state = {"start": lambda: start_plane(config_dir, tokens_path, env=env), "root": root}
    agents_network()
    state["process"] = state["start"]()
    start_memory(also=(AGENTS,))
    start_forwarder()
    start_fake_mcp()
    start_proxy(root)
    try:
        until(plane_healthy, what="control plane health")
        until(memory_ready, what="registry-mode memory service health")
        until(fake_mcp_ready, what="fake remote mcp server")
        until(proxy_ready, what="egress proxy health")
        yield state
    finally:
        stop(state["process"])
        for name in (PROXY, MEMORY, FORWARDER, MCP_SERVER, *deployed_containers()):
            docker("rm", "-f", name, check=False)
        remove_sidecars()
        docker("network", "rm", AGENTS, check=False)


def tunnel(container: str, target: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["docker", "exec", container, "python", "-c", TUNNEL, target],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    out = result.stdout.strip()
    return out.splitlines()[-1] if out else "CRASH:" + result.stderr[-400:]


def container_env(container: str) -> dict[str, str]:
    lines = docker("inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", container)
    return dict(line.split("=", 1) for line in lines.splitlines() if "=" in line)


def provider_keys() -> list[str]:
    script = "import urllib.request,sys; sys.stdout.write(urllib.request.urlopen('http://fake-provider:8000/keys').read().decode())"
    return json.loads(docker("exec", MEMORY, "python", "-c", script))


def started_at(container: str) -> str:
    return docker("inspect", "-f", "{{.State.StartedAt}}", container)


def test_egress_is_decided_per_destination_through_the_proxy(plane):
    status, record = api("POST", "/v1/deployments", {"graph": "research-pipeline"})
    assert status == 201, record
    started = {name: started_at(name) for name in (RESEARCHER, PLANNER)}

    # --- 모델 API: 키는 프록시에만, 호출은 성공
    env = container_env(RESEARCHER)
    assert PROXY_KEY not in env.values() and "e2e-fake-key" not in env.values(), (
        "모델 키가 들어갔다"
    )
    assert env["ANTHROPIC_BASE_URL"] == "http://aaestro-egress:8081/anthropic"
    status, submitted = api(
        "POST", "/v1/runs", {"deployment_id": record["deployment_id"], "input": {"query": "e2e"}}
    )
    assert status == 202, submitted

    def finished() -> dict | None:
        _, current = api("GET", f"/v1/runs/{submitted['run_id']}")
        return current if current["status"] != "running" else None

    done = until(finished, what="run through the proxy to finish")
    assert done["status"] == "completed", done
    keys = provider_keys()
    assert PROXY_KEY in keys, "대역 provider 가 프록시가 붙인 키를 받지 못했다"
    assert env["AAESTRO_ACCESS_CREDENTIAL"] not in keys, "에이전트 신원이 provider 로 새어 나갔다"

    # --- CONNECT: 선언한 목적지는 통과, 선언하지 않은 에이전트는 거부
    assert tunnel(RESEARCHER, TARGET) == "200"
    assert tunnel(PLANNER, TARGET) == "403"

    # --- 회수: 떠 있는 에이전트의 다음 CONNECT 부터 거부, 되돌리면 재배포 없이 통과
    status, revoked = api(
        "POST",
        "/v1/access/revocations",
        {"agent": "researcher", "kind": "egress", "target": TARGET, "reason": "e2e"},
    )
    assert status == 201, revoked
    until(lambda: tunnel(RESEARCHER, TARGET) == "403", what="CONNECT refused after revocation")
    api("DELETE", f"/v1/access/rules/{revoked['rule_id']}")
    until(lambda: tunnel(RESEARCHER, TARGET) == "200", what="CONNECT allowed after lifting")

    # --- record 모드: 선언 밖 목적지를 기록하되 통과시킨다
    start_proxy(plane["root"], mode="record")
    until(proxy_ready, what="egress proxy health in record mode")
    until(
        lambda: tunnel(PLANNER, TARGET) == "200",
        what="record mode lets an undeclared CONNECT through",
    )
    logs = subprocess.run(  # noqa: S603
        ["docker", "logs", PROXY],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    assert "egress denied but recorded only" in logs.stdout + logs.stderr
    start_proxy(plane["root"], mode="enforce")
    until(proxy_ready, what="egress proxy health back in enforce mode")
    until(lambda: tunnel(RESEARCHER, TARGET) == "200", what="declared CONNECT after proxy restart")

    # --- 레지스트리 중단: 캐시에 있던 허용은 유지, 처음 묻는 목적지는 거부
    stop(plane["process"])
    until(lambda: not plane_healthy(), what="control plane stopped")
    assert tunnel(RESEARCHER, TARGET) == "200", "레지스트리 중단이 캐시된 허용을 끊었다"
    assert tunnel(RESEARCHER, UNDECLARED) == "503", "레지스트리 없이 처음 묻는 목적지가 허용됐다"

    assert {name: started_at(name) for name in started} == started, (
        "권한 변경이 컨테이너를 재시작했다"
    )


# 컨테이너 안에서 프록시를 거치지 않고 나가 본다 — 이름 해석, 스택 네트워크의 사설 IP, 공인 IP
DIRECT = """
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    socket.create_connection((host, port), timeout=5).close()
    print("OPEN")
except OSError as err:
    print(type(err).__name__)
"""


def direct(container: str, host: str, port: int) -> str:
    return run_in(container, DIRECT, host, str(port))


def address_on(container: str, network: str) -> str:
    return docker(
        "inspect", "-f", f"{{{{(index .NetworkSettings.Networks \"{network}\").IPAddress}}}}",
        container,
    )  # fmt: skip


def run_completes(deployment_id: str) -> dict:
    status, submitted = api(
        "POST", "/v1/runs", {"deployment_id": deployment_id, "input": {"query": "isolated"}}
    )
    assert status == 202, submitted

    def finished() -> dict | None:
        _, current = api("GET", f"/v1/runs/{submitted['run_id']}")
        return current if current["status"] != "running" else None

    return until(finished, what="run on the isolated network to finish")


def test_isolated_agents_have_no_way_out_but_the_proxy(plane):
    status, record = api("POST", "/v1/deployments", {"graph": "research-pipeline"})
    assert status == 201, record
    assert record["status"] == "ready", record
    assert docker("port", RESEARCHER, check=False) == "", "내부 네트워크 에이전트가 포트를 게시했다"
    assert docker("network", "inspect", "-f", "{{.Internal}}", AGENTS) == "true"

    # --- 프록시를 거치지 않은 외부 호출은 실패한다: 이름 해석, 직접 IP 둘 다
    provider_ip = address_on("aaestro-e2e-fake-provider-1", NETWORK)
    assert direct(RESEARCHER, "fake-provider", 8000) == "gaierror", "스택 네트워크의 이름이 풀렸다"
    assert direct(RESEARCHER, provider_ip, 8000) != "OPEN", "외부 망의 IP 로 직접 나갔다"
    assert direct(RESEARCHER, "1.1.1.1", 443) != "OPEN", "공인 IP 로 직접 나갔다"
    # --- 프록시 경유만 성공한다 — 같은 목적지를 선언대로
    assert tunnel(RESEARCHER, TARGET) == "200"

    # --- control plane 은 격리된 에이전트를 부른다 (health 는 배포가 ready 로 이미 증명)
    assert run_completes(record["deployment_id"])["status"] == "completed"

    # --- Memory Service 와 A2A peer 호출은 격리 뒤에도 동작한다
    longterm = "local:researcher:longterm"
    assert run_in(RESEARCHER, MEMORY_CALL, "append", "longterm", longterm) == "200"
    until(lambda: run_in(RESEARCHER, PEER_CALL, "iso") == "OK", what="A2A peer call", timeout_s=60)

    # --- 재부착: control plane 을 다시 띄워도 격리된 에이전트에 다시 붙어 부른다
    started = {name: started_at(name) for name in (RESEARCHER, PLANNER)}
    stop(plane["process"])
    plane["process"] = plane["start"]()
    until(plane_healthy, what="control plane back")

    def reattached() -> bool:
        _, current = api("GET", f"/v1/deployments/{record['deployment_id']}")
        return current["status"] == "ready"

    until(reattached, what="isolated deployment reattached")
    assert run_completes(record["deployment_id"])["status"] == "completed"
    assert {name: started_at(name) for name in started} == started, "재부착이 컨테이너를 갈았다"

    # --- 해체: 컨테이너가 사라진다
    status, _ = api("DELETE", f"/v1/deployments/{record['deployment_id']}")
    assert status in (200, 202, 204), status
    until(lambda: not deployed_containers(), what="isolated containers torn down")


CARD = """
import json, os, urllib.request
request = urllib.request.Request(
    "http://127.0.0.1:8080/v1/card",
    headers={"Authorization": "Bearer " + os.environ["AAESTRO_AGENT_TOKEN"]},
)
print(json.dumps([s["name"] for s in json.load(urllib.request.urlopen(request))["skills"]]))
"""


ANSWER = "@@"


class McpSession:
    """떠 있는 에이전트 컨테이너 안의 MCP 세션 하나 — 도구를 부를 때마다 같은 세션을 쓴다.

    stdout 은 스레드가 줄 단위로 큐에 옮긴다 — ``select`` 는 파이썬 버퍼에 이미 읽힌 줄을 보지 못해
    답이 와 있는데도 기다린다.
    """

    def __init__(self, container: str, server: str = "corp") -> None:
        self.process = subprocess.Popen(  # noqa: S603
            ["docker", "exec", "-i", container, "python", "-c", MCP_SESSION, server],  # noqa: S607
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.lines: queue.Queue[str] = queue.Queue()
        self.seen: list[str] = []
        threading.Thread(target=self._pump, daemon=True).start()
        ready = self._answer()
        assert ready == "READY", ready

    def call(self, tool: str, arguments: dict[str, Any]) -> str:
        self.process.stdin.write(f"{tool} {json.dumps(arguments)}\n")
        self.process.stdin.flush()
        return self._answer()

    def _pump(self) -> None:
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put("")

    def _answer(self, timeout_s: float = 60.0) -> str:
        """다음 답 한 줄 — 로그 줄은 건너뛰되 남겨 둔다. 답이 없으면 그 로그로 원인을 보인다."""
        deadline = time.monotonic() + timeout_s
        while (left := deadline - time.monotonic()) > 0:
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                break
            if not line:
                return "CLOSED:" + "".join(self.seen[-20:])
            if line.startswith(ANSWER):
                return line[len(ANSWER) :].strip()
            self.seen.append(line)
        self.process.kill()
        raise AssertionError("mcp session did not answer:\n" + "".join(self.seen[-20:]))

    def close(self) -> None:
        self.process.stdin.close()
        self.process.wait(timeout=30)


def succeeded(answer: str, expected: str) -> bool:
    """성공한 도구 결과에 기대한 값이 있는가 — 에러 사유에 같은 글자가 섞여도 통과하지 않게."""
    return answer.startswith("OK:") and expected in answer.removeprefix("OK:")


def test_remote_mcp_tools_are_decided_one_by_one_through_the_proxy(plane):
    status, record = api("POST", "/v1/deployments", {"graph": "research-pipeline"})
    assert status == 201, record
    started = started_at(RESEARCHER)

    # --- 자격은 프록시에만, 원격 서버는 프록시 종단으로
    env = container_env(RESEARCHER)
    assert MCP_TOKEN_ENV not in env and MCP_TOKEN not in env.values(), (
        "MCP 자격이 컨테이너에 들어갔다"
    )
    assert env["AAESTRO_MCP_PROXY_URL"] == "http://aaestro-egress:8081/mcp"
    # --- agentd 가 기동 때 세션을 열어 도구를 광고한다
    advertised = json.loads(run_in(RESEARCHER, CARD))
    assert {"mcp__corp__echo", "mcp__corp__add"} <= set(advertised), advertised

    session = McpSession(RESEARCHER)
    first = session.call("echo", {"text": "e2e"})
    assert succeeded(first, "e2e"), first
    added = session.call("add", {"a": 2, "b": 3})
    assert succeeded(added, "5"), added

    # --- 도구 하나 회수: 다음 호출부터 거부, 같은 서버의 다른 도구는 계속
    status, revoked = api(
        "POST",
        "/v1/access/revocations",
        {"agent": "researcher", "kind": "mcp_tool", "target": "corp/echo", "reason": "e2e"},
    )
    assert status == 201, revoked
    # 같은 세션의 다음 호출부터 — 세션을 다시 열지 않는다
    until(
        lambda: session.call("echo", {"text": "e2e"}).startswith("ERR:MCP_003:ACC_001"),
        what="revoked mcp tool refused in the open session",
        timeout_s=30,
    )
    added = session.call("add", {"a": 2, "b": 3})
    assert succeeded(added, "5"), f"회수가 같은 서버의 다른 도구까지 막았다: {added}"

    api("DELETE", f"/v1/access/rules/{revoked['rule_id']}")
    until(
        lambda: succeeded(session.call("echo", {"text": "e2e"}), "e2e"), what="lifted", timeout_s=30
    )
    session.close()
    assert started_at(RESEARCHER) == started, "권한 변경이 컨테이너를 재시작했다"


# 에이전트 컨테이너 안에서 사이드카에 **직접** 닿아 본다 — 프록시를 건너뛰는 길이 없어야 한다
DIRECT_SIDECAR = f"""
import socket
try:
    socket.create_connection(("{SIDECAR}", 8000), timeout=5).close()
    print("REACHED")
except OSError as err:
    print("BLOCKED:" + type(err).__name__)
"""


def networks_of(container: str) -> set[str]:
    return set(json.loads(docker("inspect", "-f", "{{json .NetworkSettings.Networks}}", container)))


def test_an_agents_sidecar_is_reached_only_through_the_proxy_decided_per_tool(plane):
    """#304 — 사이드카를 배포·광고·실행, 도구 하나를 회수해도 나머지는 계속, 해체하면 사라진다."""
    status, record = api("POST", "/v1/deployments", {"graph": "research-pipeline"})
    assert status == 201, record
    started = started_at(RESEARCHER)

    # --- 사이드카는 자기 네트워크에만, 그 네트워크에 붙는 것은 프록시뿐
    assert docker("inspect", "-f", "{{.State.Running}}", SIDECAR) == "true"
    assert networks_of(SIDECAR) == {SIDECAR_NET}
    assert docker("network", "inspect", "-f", "{{.Internal}}", SIDECAR_NET) == "true"
    assert SIDECAR_NET in networks_of(PROXY)
    assert networks_of(RESEARCHER) == {AGENTS}, "에이전트가 사이드카 네트워크에 붙었다"
    hardening = docker(
        "inspect", "-f",
        "{{.Config.User}} {{.HostConfig.ReadonlyRootfs}} {{json .HostConfig.CapDrop}} "
        "{{json .HostConfig.SecurityOpt}} {{json .HostConfig.PortBindings}}",
        SIDECAR,
    )  # fmt: skip
    assert hardening.split() == [
        "1000:1000", "true", '["ALL"]', '["no-new-privileges:true"]', "{}",
    ], hardening  # fmt: skip
    assert run_in(RESEARCHER, DIRECT_SIDECAR).startswith("BLOCKED"), "프록시를 건너뛰는 길이 있다"

    # --- agentd 가 기동 때 세션을 열어 도구를 광고한다
    advertised = json.loads(run_in(RESEARCHER, CARD))
    assert {"mcp__tools__echo", "mcp__tools__add"} <= set(advertised), advertised

    session = McpSession(RESEARCHER, "tools")
    assert succeeded(session.call("echo", {"text": "side"}), "side")
    assert succeeded(session.call("add", {"a": 4, "b": 5}), "9")

    # --- 도구 하나 회수: 다음 호출부터 거부, 같은 사이드카의 다른 도구는 계속
    status, revoked = api(
        "POST",
        "/v1/access/revocations",
        {"agent": "researcher", "kind": "mcp_tool", "target": "tools/echo", "reason": "e2e"},
    )
    assert status == 201, revoked
    until(
        lambda: session.call("echo", {"text": "side"}).startswith("ERR:MCP_003:ACC_001"),
        what="revoked sidecar tool refused in the open session",
        timeout_s=30,
    )
    added = session.call("add", {"a": 4, "b": 5})
    assert succeeded(added, "9"), f"회수가 같은 사이드카의 다른 도구까지 막았다: {added}"
    api("DELETE", f"/v1/access/rules/{revoked['rule_id']}")
    until(
        lambda: succeeded(session.call("echo", {"text": "side"}), "side"),
        what="lifted",
        timeout_s=30,
    )
    session.close()
    assert started_at(RESEARCHER) == started, "권한 변경이 컨테이너를 재시작했다"

    # --- 해체하면 사이드카와 그 네트워크도 사라진다
    status, stopped = api("DELETE", f"/v1/deployments/{record['deployment_id']}")
    assert status == 200, stopped
    assert docker("ps", "-aq", "--filter", f"name=^{SIDECAR}$") == ""
    assert docker("network", "ls", "-q", "--filter", f"name=^{SIDECAR_NET}$") == ""
    assert SIDECAR_NET not in networks_of(PROXY)
