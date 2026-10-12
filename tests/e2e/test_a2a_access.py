"""A2A calls authorized by the callee on every request (#281).

떠 있는 researcher 컨테이너 **안에서** agentd 가 받은 배선 그대로 planner 를 부른다. 운영자가 연결을
회수하면 컨테이너를 건드리지 않고 다음 호출이 `A2A_004` 여야 하고, 호출자 쪽 검사를 우회해도
피호출자가 거부해야 한다.

control plane 은 호스트에서 돈다. 에이전트는 내부 네트워크에만 있으므로, 같은 네트워크에
`control-plane` 이라는 이름으로 호스트에 전달만 하는 작은 포워더를 세운다 — 테스트 기반시설일 뿐
프레임워크에는 호스트로 나가는 경로를 더하지 않는다.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from typing import Any

import pytest

from tests.e2e.conftest import (
    CONTROL_PORT,
    NETWORK,
    api,
    deployed_containers,
    memory_tokens,
    plane_healthy,
    start_plane,
    stop,
    until,
    write_config,
)
from tests.e2e.test_memory_access import MEMORY, MEMORY_ALIAS, memory_ready, start_memory
from tests.e2e.test_stack import docker, requires_docker

pytestmark = [pytest.mark.e2e, requires_docker]

ENFORCER_TOKEN = "e2e-enforcer-token"  # noqa: S105
FORWARDER = "aaestro-e2e-control-forwarder"
FORWARD = f"""
import asyncio

async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()

async def handle(reader, writer):
    upstream_reader, upstream_writer = await asyncio.open_connection(
        "host.docker.internal", {CONTROL_PORT}
    )
    await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))

async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", 8700)
    async with server:
        await server.serve_forever()

asyncio.run(main())
"""

# 컨테이너 안에서 agentd 의 조립 그대로 peer 클라이언트를 만들어 부른다
CALL = """
import asyncio, os, sys
import yaml
from aaestro.agentd.a2a_server import build_peer_client
from aaestro.core.agent import TaskConfig, TaskRequest, TraceContext
from aaestro.core.errors import AaestroError
from aaestro.core.manifest import AgentManifest

async def main():
    with open(os.environ["AAESTRO_MANIFEST"], encoding="utf-8") as handle:
        manifest = AgentManifest.model_validate(yaml.safe_load(handle))
    client = build_peer_client(manifest)
    task = TaskRequest(task_id="e2e-a2a-" + sys.argv[1], run_id="e2e", node_id="planner",
                       input={"query": "what first"}, config=TaskConfig(),
                       trace=TraceContext(trace_id="tr"))
    try:
        await client.call("planner", task)
        print("OK")
    except AaestroError as err:
        print("DETAIL:" + str(err) + " " + str(err.details)[:400], file=sys.stderr)
        print("ERR:" + str(err.code))

asyncio.run(main())
"""

# 호출자 쪽 검사를 건너뛰고 transport 로 직접 보낸다 — 공유 서명 키로 만든 edge token 만 싣는다
BYPASS = """
import asyncio, os
from aaestro.core.agent import TaskConfig, TaskRequest, TraceContext
from aaestro.core.errors import AaestroError
from aaestro.protocols.a2a.allowlist import Edge, issue_token
from aaestro.protocols.a2a.sdk import SdkPeerTransport

async def main():
    peers = dict(entry.split("=", 1) for entry in os.environ["AAESTRO_A2A_PEERS"].split(","))
    transport = SdkPeerTransport(agent="researcher",
                                 addresses={"planner": "http://" + peers["planner"]})
    task = TaskRequest(task_id="e2e-bypass", run_id="e2e", node_id=None, input={},
                       config=TaskConfig(), trace=TraceContext(trace_id="tr"))
    token = issue_token(b"guessed-or-leaked", Edge("researcher", "planner"))
    try:
        await transport.send(callee="planner", task=task, token=token, headers={})
        print("OK")
    except AaestroError as err:
        print("ERR:" + str(err.code))
    finally:
        await transport.close()

asyncio.run(main())
"""


@pytest.fixture
def plane(stack, tmp_path) -> Iterator[dict[str, Any]]:
    docker("rm", "-f", FORWARDER, check=False)
    docker(
        "run", "-d", "--name", FORWARDER,
        "--network", NETWORK, "--network-alias", "control-plane",
        "--add-host", "host.docker.internal:host-gateway",
        "--read-only", "--user", "1000:1000", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--entrypoint", "python", "aaestro/memory-service:0.1.0", "-c", FORWARD,
    )  # fmt: skip
    config_dir = write_config(
        tmp_path,
        orchestrator={
            "control_host": "0.0.0.0",  # noqa: S104 — 포워더가 호스트로 닿아야 한다
            "access_store": str(tmp_path / "access.db"),
            "access_enforcer_token": ENFORCER_TOKEN,
            "access_agent_url": "http://control-plane:8700",
        },
    )
    tokens_path = tmp_path / "memory.json"
    tokens_path.write_text(json.dumps(memory_tokens()), encoding="utf-8")
    # 레지스트리를 켜면 에이전트는 신원을 메모리 토큰으로 내민다 — Memory Service 도
    # 레지스트리 모드여야 태스크가 기억을 불러올 수 있다 (둘은 함께 켠다)
    env = {"AAESTRO_MEMORY_URL": f"http://{MEMORY_ALIAS}:8090"}
    state = {"process": start_plane(config_dir, tokens_path, env=env)}
    start_memory()
    try:
        until(plane_healthy, what="control plane health")
        until(memory_ready, what="registry-mode memory service health")
        yield state
    finally:
        stop(state["process"])
        docker("rm", "-f", FORWARDER, check=False)
        docker("rm", "-f", MEMORY, check=False)
        for name in deployed_containers():
            docker("rm", "-f", name, check=False)


def run_in(container: str, script: str, *args: str) -> str:
    """스크립트의 마지막 출력 줄 — 스크립트가 죽었으면 stderr 꼬리를 돌려줘 원인이 보이게."""
    result = subprocess.run(  # noqa: S603
        ["docker", "exec", container, "python", "-c", script, *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    out = result.stdout.strip()
    if result.stderr.strip():
        run_in.last_stderr = result.stderr.strip()[-800:]  # type: ignore[attr-defined]
    return out.splitlines()[-1] if out else "CRASH:" + result.stderr.strip()[-600:]


def started_at(container: str) -> str:
    return docker("inspect", "-f", "{{.State.StartedAt}}", container)


def test_a2a_connections_are_decided_by_the_callee_on_every_call(plane):
    status, record = api("POST", "/v1/deployments", {"graph": "research-pipeline"})
    assert status == 201, record
    researcher, planner = "aaestro-researcher-0", "aaestro-planner-0"
    started = {name: started_at(name) for name in (researcher, planner)}
    env = docker("inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", researcher)
    assert "AAESTRO_A2A_SECRET=" not in env, "레지스트리 모드에 공유 서명 키가 들어갔다"

    calls = iter(range(1000))

    def call() -> str:
        return run_in(researcher, CALL, str(next(calls)))

    seen: list[str] = []

    def answered(expected: str) -> bool:
        seen.append(call())
        return seen[-1] == expected

    try:
        until(lambda: answered("OK"), what="declared researcher -> planner call", timeout_s=60)
    except AssertionError:
        logs = subprocess.run(  # noqa: S603
            ["docker", "logs", "--tail", "30", planner],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        detail = getattr(run_in, "last_stderr", "")
        planner_log = (logs.stdout + logs.stderr)[-4000:]
        raise AssertionError(
            f"last answers {seen[-3:]}\ncaller: {detail}\nplanner log:\n{planner_log}"
        ) from None

    # --- 회수: 다음 호출이 A2A_004, 되돌리면 재배포 없이 성공
    status, revoked = api(
        "POST",
        "/v1/access/revocations",
        {"agent": "researcher", "kind": "a2a", "target": "planner", "reason": "e2e"},
    )
    assert status == 201, revoked
    until(lambda: call() == "ERR:A2A_004", what="call refused after revocation", timeout_s=30)
    status, _ = api("DELETE", f"/v1/access/rules/{revoked['rule_id']}")
    assert status == 200
    until(lambda: call() == "OK", what="call allowed after lifting", timeout_s=30)

    # --- 호출자 쪽 검사를 우회해도 피호출자가 거부한다
    assert run_in(researcher, BYPASS) == "ERR:A2A_004", "edge token 만으로 피호출자를 통과했다"

    assert {name: started_at(name) for name in started} == started, (
        "권한 변경이 컨테이너를 재시작했다"
    )
