"""Memory permissions change under running agents (#278).

떠 있는 에이전트 컨테이너 **안에서** Memory Service 를 부른다 — agentd 가 받은 주소와 신원 그대로.
운영자가 레지스트리에서 회수·강등하면 컨테이너를 건드리지 않고 다음 요청부터 바뀌어야 하고,
Memory Service 를 재시작해도 신원은 통해야 하며, 레지스트리가 멈추면 캐시 규칙대로 동작해야 한다.

Memory Service 는 compose 의 토큰 모드 서비스와 따로, **레지스트리 모드**로 하나 더 세운다.
control plane 은 호스트에서 돌므로 컨테이너가 `host-gateway` 로 닿도록 모든 주소에 바인드한다
(그래서 control 토큰과 강제 지점 토큰이 둘 다 필요하다).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

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
from tests.e2e.test_stack import docker, requires_docker

pytestmark = [pytest.mark.e2e, requires_docker]

ENFORCER_TOKEN = "e2e-enforcer-token"  # noqa: S105
MEMORY = "aaestro-e2e-memory-registry"
MEMORY_ALIAS = "memory-registry"
AGENT = "aaestro-researcher-0"
LONGTERM = "local:researcher:longterm"
KNOWLEDGE = "group:research:knowledge"
ORG = "global:global:org"

# 컨테이너 안에서 agentd 가 받은 env 로 Memory Service 를 부른다 — 상태 코드만 출력한다
CALL = """
import json, os, sys, urllib.error, urllib.request
op, alias, _space_id = sys.argv[1:4]
body = {"space": alias}
if op == "append":
    # 내용만 싣는다 — id · 시각 · 출처의 신원은 서비스가 발급한다 (#456)
    body["entry"] = {"kind": "fact", "content": "e2e"}
request = urllib.request.Request(
    os.environ["AAESTRO_MEMORY_URL"] + "/v1/" + op,
    data=json.dumps(body).encode(),
    headers={"Authorization": "Bearer " + os.environ["AAESTRO_MEMORY_TOKEN"],
             "content-type": "application/json"},
)
try:
    print(urllib.request.urlopen(request, timeout=10).status)
except urllib.error.HTTPError as err:
    print(err.code)
"""


def start_memory(network: str = NETWORK, *, also: tuple[str, ...] = ()) -> None:
    """레지스트리 모드 Memory Service — ``also`` 는 함께 붙일 네트워크 (격리된 에이전트 망 등)."""
    docker("rm", "-f", MEMORY, check=False)
    docker(
        "run", "-d", "--name", MEMORY,
        "--network", network, "--network-alias", MEMORY_ALIAS,
        "--add-host", "host.docker.internal:host-gateway",
        "--read-only", "--tmpfs", "/tmp:size=16m",  # noqa: S108 — 컨테이너 안 tmpfs
        "--user", "1000:1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "-v", f"{REPO_ROOT}:/repo:ro",
        "-e", "AAESTRO_EMBEDDING_BASE_URL=http://fake-provider:8000",
        "-e", f"AAESTRO_ACCESS_URL=http://host.docker.internal:{CONTROL_PORT}",
        "-e", f"AAESTRO_ACCESS_ENFORCER_TOKEN={ENFORCER_TOKEN}",
        "aaestro/memory-service:0.1.0",
    )  # fmt: skip
    for extra in also:
        docker("network", "connect", "--alias", MEMORY_ALIAS, extra, MEMORY)


def memory_ready() -> bool:
    return docker("inspect", "-f", "{{.State.Health.Status}}", MEMORY, check=False) == "healthy"


@pytest.fixture
def plane(stack, tmp_path) -> Iterator[dict[str, Any]]:
    config_dir = write_config(
        tmp_path,
        orchestrator={
            "control_host": "0.0.0.0",  # noqa: S104 — 레지스트리 모드 Memory Service 가 닿아야 한다
            "access_store": str(tmp_path / "access.db"),
            "access_enforcer_token": ENFORCER_TOKEN,
            "access_agent_url": f"http://host.docker.internal:{CONTROL_PORT}",
        },
    )
    tokens_path = tmp_path / "memory.json"
    tokens_path.write_text(json.dumps(memory_tokens()), encoding="utf-8")
    env = {"AAESTRO_MEMORY_URL": f"http://{MEMORY_ALIAS}:8090"}
    state = {"start": lambda: start_plane(config_dir, tokens_path, env=env)}
    state["process"] = state["start"]()
    start_memory()
    try:
        until(plane_healthy, what="control plane health")
        until(memory_ready, what="registry-mode memory service health")
        yield state
    finally:
        stop(state["process"])
        docker("rm", "-f", MEMORY, check=False)
        for name in deployed_containers():
            docker("rm", "-f", name, check=False)


def call(op: str, alias: str, space: str) -> int:
    return int(docker("exec", AGENT, "python", "-c", CALL, op, alias, space))


def started_at() -> str:
    return docker("inspect", "-f", "{{.State.StartedAt}}", AGENT)


def becomes(expected: int, op: str, alias: str, space: str, *, what: str) -> None:
    until(lambda: call(op, alias, space) == expected, what=what, timeout_s=30)


def test_memory_permissions_change_under_a_running_agent(plane):
    status, record = api("POST", "/v1/deployments", {"graph": "research-pipeline"})
    assert status == 201, record
    started = started_at()
    assert call("append", "longterm", LONGTERM) == 200, "선언된 local space 에 쓰지 못했다"
    assert call("append", "knowledge", KNOWLEDGE) == 200, "선언된 group space 에 쓰지 못했다"

    # --- 회수: 떠 있는 에이전트의 다음 append 가 거부된다
    status, revoked = api(
        "POST",
        "/v1/access/revocations",
        {"agent": "researcher", "kind": "memory", "target": LONGTERM, "reason": "e2e"},
    )
    assert status == 201, revoked
    becomes(401, "append", "longterm", LONGTERM, what="append refused after revocation")
    assert started_at() == started, "권한 변경이 컨테이너를 재시작했다"
    status, _ = api("DELETE", f"/v1/access/rules/{revoked['rule_id']}")
    becomes(200, "append", "longterm", LONGTERM, what="append allowed after lifting")

    # --- rw → ro 강등: 쓰기 거부, 읽기 유지
    status, demoted = api(
        "POST",
        "/v1/access/revocations",
        {
            "agent": "researcher",
            "kind": "memory",
            "target": KNOWLEDGE,
            "mode": "rw",
            "reason": "ro",
        },
    )
    assert status == 201, demoted
    becomes(401, "append", "knowledge", KNOWLEDGE, what="write refused after demotion")
    assert call("read", "knowledge", KNOWLEDGE) == 200, "강등이 읽기까지 막았다"
    assert started_at() == started

    # --- Memory Service 재시작: 떠 있는 에이전트의 신원은 그대로 통한다
    docker("restart", MEMORY)
    until(memory_ready, what="memory service health after restart")
    becomes(200, "append", "longterm", LONGTERM, what="append after memory service restart")

    # --- 레지스트리 중단: 캐시에 있는 허용은 유지, 처음 묻는 space 는 거부
    assert call("append", "longterm", LONGTERM) == 200  # 이 판정을 캐시에 올린다
    stop(plane["process"])
    until(lambda: not plane_healthy(), what="control plane stopped")
    assert call("append", "longterm", LONGTERM) == 200, "레지스트리 중단이 캐시된 허용을 끊었다"
    assert call("read", "org", ORG) == 401, "레지스트리 없이 처음 묻는 space 가 허용됐다"
    assert started_at() == started

    plane["process"] = plane["start"]()
    until(plane_healthy, what="control plane health after restart")
    becomes(200, "read", "org", ORG, what="new space decided once the registry is back")
