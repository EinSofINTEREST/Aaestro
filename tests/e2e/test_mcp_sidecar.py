"""An agent's MCP sidecar without the egress proxy (#304).

프록시가 없는 배포에는 판정 지점이 없다 — 지킬 것은 03 Placement 1 의 1:1 배치다. 사이드카는 소유
에이전트의 사이드카 네트워크에만 있고, 소유 에이전트만 그 네트워크에 더 붙어 이름으로 닿는다. 같은
그래프의 다른 에이전트는 닿지 못한다. 프록시를 거치는 배포는 ``test_egress.py`` 가 본다.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.e2e.conftest import (
    GRAPH,
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
from tests.e2e.test_a2a_access import run_in
from tests.e2e.test_egress import (
    CARD,
    DIRECT_SIDECAR,
    PLANNER,
    RESEARCHER,
    SIDECAR,
    SIDECAR_IMAGE,
    SIDECAR_NET,
    McpSession,
    build_sidecar_image,
    networks_of,
    remove_sidecars,
    succeeded,
)
from tests.e2e.test_stack import docker, requires_docker

pytestmark = [pytest.mark.e2e, requires_docker]


def workspace(tmp_path: Path) -> Path:
    """저장소 선언을 복사하고 researcher 에게만 사이드카를 선언한다."""
    root = tmp_path / "repo"
    for name in ("agents", "graphs", "groups", "modules"):
        shutil.copytree(REPO_ROOT / name, root / name, ignore=shutil.ignore_patterns("__pycache__"))
    for path in [root, *root.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    manifest_path = root / "agents" / "researcher" / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["spec"]["mcp"] = {
        "servers": [
            {"name": "tools", "transport": "streamable-http", "sidecar": {"image": SIDECAR_IMAGE}}
        ]
    }
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return root


@pytest.fixture
def plane(stack, tmp_path) -> Iterator[dict[str, Any]]:
    build_sidecar_image(tmp_path)
    remove_sidecars()
    config_dir = write_config(tmp_path)
    tokens_path = tmp_path / "memory.json"
    tokens_path.write_text(json.dumps(memory_tokens()), encoding="utf-8")
    process = start_plane(
        config_dir, tokens_path, env={"AAESTRO_REPO_ROOT": str(workspace(tmp_path))}
    )
    try:
        until(plane_healthy, what="control plane health")
        yield {"process": process}
    finally:
        stop(process)
        for name in deployed_containers():
            docker("rm", "-f", name, check=False)
        remove_sidecars()


def test_without_the_proxy_only_the_owner_joins_its_sidecar_network(plane):
    status, record = api("POST", "/v1/deployments", {"graph": GRAPH})
    assert status == 201, record

    assert networks_of(SIDECAR) == {SIDECAR_NET}
    assert docker("network", "inspect", "-f", "{{.Internal}}", SIDECAR_NET) == "true"
    assert networks_of(RESEARCHER) == {NETWORK, SIDECAR_NET}
    assert networks_of(PLANNER) == {NETWORK}
    # 소유 에이전트만 닿는다 — 같은 그래프의 다른 에이전트는 이름조차 풀지 못한다
    assert run_in(RESEARCHER, DIRECT_SIDECAR) == "REACHED"
    assert run_in(PLANNER, DIRECT_SIDECAR).startswith("BLOCKED")

    advertised = json.loads(run_in(RESEARCHER, CARD))
    assert {"mcp__tools__echo", "mcp__tools__add"} <= set(advertised), advertised
    session = McpSession(RESEARCHER, "tools")
    assert succeeded(session.call("add", {"a": 20, "b": 22}), "42")
    session.close()

    status, stopped = api("DELETE", f"/v1/deployments/{record['deployment_id']}")
    assert status == 200, stopped
    assert docker("ps", "-aq", "--filter", f"name=^{SIDECAR}$") == ""
    assert docker("network", "ls", "-q", "--filter", f"name=^{SIDECAR_NET}$") == ""


# 컨테이너 안에서 자기 Control API 를 부르고 **곧바로** health 를 읽는다 — 배포된 에이전트의 포트는
# 호스트에 열려 있지 않다. 한 exec 안에서 읽어야 runtime 이 컨테이너를 바꾸기 전의 상태를 본다
INVOKE_THEN_HEALTH = """
import json, os, sys, urllib.request
body = {"task_id": sys.argv[1], "run_id": "direct-" + sys.argv[1], "node_id": None,
        "input": {"query": sys.argv[2]}, "config": {}, "trace": {"trace_id": sys.argv[1]}}
request = urllib.request.Request(
    "http://127.0.0.1:8080/v1/invoke", data=json.dumps(body).encode(),
    headers={"Authorization": "Bearer " + os.environ["AAESTRO_AGENT_TOKEN"],
             "content-type": "application/json"})
result = json.loads(urllib.request.urlopen(request, timeout=90).read())
health = json.loads(urllib.request.urlopen("http://127.0.0.1:8080/v1/health", timeout=5).read())
print((result.get("error") or {}).get("code") or result["status"], health["status"])
"""
HEALTH = """
import json, urllib.request
body = urllib.request.urlopen("http://127.0.0.1:8080/v1/health", timeout=5).read()
print(json.loads(body)["status"])
"""


def container_id(name: str) -> str:
    return docker("inspect", "-f", "{{.Id}}", name, check=False)


def test_a_session_that_cannot_reconnect_makes_the_agent_unhealthy_and_restarted(plane):
    """재연결을 소진한 필수 MCP 세션을 가진 에이전트가 Ready 로 남으면 안 된다 (#311)."""
    status, record = api("POST", "/v1/deployments", {"graph": GRAPH})
    assert status == 201, record
    assert run_in(RESEARCHER, HEALTH) == "healthy"
    before = container_id(RESEARCHER)

    docker("stop", SIDECAR)
    # 대역 모델이 사이드카 도구를 부르게 한다 — 세션은 도구 호출에서 단절을 알아채고
    # 재연결을 시도한다
    outcome, health = run_in(
        RESEARCHER,
        INVOKE_THEN_HEALTH,
        "e2e-mcp-down",
        'use tool mcp__tools__echo with {"text": "hi"}',
    ).split()

    assert outcome.startswith("MCP_"), outcome
    assert health == "unhealthy"
    # runtime 의 health 루프가 Unhealthy 를 보고 컨테이너를 바꾼다 (02 Lifecycle 6)
    until(
        lambda: container_id(RESEARCHER) not in ("", before),
        what="the unhealthy agent replaced by the runtime",
        timeout_s=90,
    )
