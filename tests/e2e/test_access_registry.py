"""Agent identities on the real stack (#277).

배포가 에이전트마다 신원을 발급해 컨테이너 env 로 넣고, 강제 지점이 그 신원으로 판정을 받으며,
신원은 control plane 재시작을 넘고, 해체하면 더 이상 통하지 않는다 — 대역 Docker 로는
"실제 컨테이너 env 에 들어갔는가" 와 "재시작 뒤 저장소에서 되살아나는가" 를 건너지 못한다.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from tests.e2e.conftest import (
    AGENTS,
    CONTROL_PORT,
    GRAPH,
    api,
    deployed_containers,
    memory_tokens,
    plane_healthy,
    plane_url,
    start_plane,
    stop,
    until,
    write_config,
)
from tests.e2e.test_stack import docker, requires_docker

pytestmark = [pytest.mark.e2e, requires_docker]

ENFORCER_TOKEN = "e2e-enforcer-token"  # noqa: S105
CREDENTIAL_ENV = "AAESTRO_ACCESS_CREDENTIAL"


@pytest.fixture
def plane(stack, tmp_path) -> Iterator[dict[str, Any]]:
    config_dir = write_config(
        tmp_path,
        orchestrator={
            "access_store": str(tmp_path / "access.db"),
            "access_enforcer_token": ENFORCER_TOKEN,
            "access_agent_url": f"http://host.docker.internal:{CONTROL_PORT}",
            "access_stewards": ["permission-agent"],
        },
    )
    tokens_path = tmp_path / "memory.json"
    tokens_path.write_text(json.dumps(memory_tokens()), encoding="utf-8")
    state = {
        "process": start_plane(config_dir, tokens_path),
        "config_dir": config_dir,
        "tokens_path": tokens_path,
    }
    try:
        until(plane_healthy, what="control plane health")
        yield state
    finally:
        stop(state["process"])
        for name in deployed_containers():
            docker("rm", "-f", name, check=False)


def identify(credential: str) -> str | None:
    """강제 지점 자격으로 판정을 받아, 레지스트리가 알아본 에이전트 이름을 돌려준다."""
    request = urllib.request.Request(  # noqa: S310 — 루프백 고정 URL
        f"{plane_url()}/v1/access/decisions",
        data=json.dumps(
            {"credential": credential, "kind": "egress", "target": "api.example.com"}
        ).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {ENFORCER_TOKEN}", "content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return json.loads(response.read())["agent"]


def credential_in(container: str) -> str:
    env = json.loads(docker("inspect", "--format", "{{json .Config.Env}}", container))
    prefix = f"{CREDENTIAL_ENV}="
    return next(item.removeprefix(prefix) for item in env if item.startswith(prefix))


def test_identities_are_injected_survive_a_restart_and_die_with_the_deployment(plane):
    status, record = api("POST", "/v1/deployments", {"graph": GRAPH})
    assert status == 201, record
    deployment_id = record["deployment_id"]

    # --- 발급·주입: 에이전트마다 다른 신원이 실제 컨테이너 env 에 들어간다
    credentials = {agent: credential_in(f"aaestro-{agent}-0") for agent in AGENTS}
    assert len(set(credentials.values())) == len(AGENTS), "에이전트끼리 신원을 공유했다"
    for agent, credential in credentials.items():
        assert identify(credential) == agent
    _, view = api("GET", f"/v1/deployments/{deployment_id}")
    assert not any(c in json.dumps(view) for c in credentials.values()), "배포 조회가 신원을 흘렸다"

    # --- control plane 재시작: 신원은 레지스트리 저장소에 남는다
    stop(plane["process"])
    plane["process"] = start_plane(plane["config_dir"], plane["tokens_path"])
    until(plane_healthy, what="control plane health after restart")
    assert all(identify(c) == agent for agent, c in credentials.items())

    # --- 해체: 신원은 더 이상 통하지 않는다
    status, stopped = api("DELETE", f"/v1/deployments/{deployment_id}")
    assert status == 200, stopped
    assert all(identify(c) is None for c in credentials.values())
