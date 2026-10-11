"""A deployment's ``model_overrides`` reaches the provider call (#359).

#358 이 노드 · 배포 override 를 ``TaskConfig.model`` 로 싣고, #359 가 agentd 가 그것을 첫 호출부터
쓰게 했다. 단위 테스트는 양쪽을 따로 본다 — 배포 → run → 컨테이너 → provider 요청의 ``model``
필드까지 이어지는지는 실 스택에서만 증명된다.
"""

from __future__ import annotations

import json

import pytest

from tests.e2e.conftest import AGENTS, GRAPH, api, until
from tests.e2e.test_stack import COMPOSE_FILE, docker, requires_docker

pytestmark = [pytest.mark.e2e, requires_docker]

OVERRIDE = "claude-e2e-override"


def models_seen() -> list[str]:
    """대역 provider 가 받은 요청의 ``model`` — 스택 안에서 물어야 한다."""
    raw = docker(
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "exec",
        "-T",
        "agent-researcher",
        "python",
        "-c",
        "import urllib.request,sys;"
        "sys.stdout.write(urllib.request.urlopen('http://fake-provider:8000/models').read().decode())",
    )
    return list(json.loads(raw))


def test_a_deployment_model_override_is_the_model_the_provider_receives(plane):
    before = len(models_seen())

    status, record = api(
        "POST",
        "/v1/deployments",
        {"graph": GRAPH, "model_overrides": {"planner": {"name": OVERRIDE}}},
    )
    assert status == 201, record
    assert sorted(a["name"] for a in record["agents"]) == sorted(AGENTS)

    status, submitted = api(
        "POST", "/v1/runs", {"deployment_id": record["deployment_id"], "input": {"query": "e2e"}}
    )
    assert status == 202, submitted

    def finished() -> dict | None:
        _, current = api("GET", f"/v1/runs/{submitted['run_id']}")
        return current if current["status"] != "running" else None

    done = until(finished, what="run to finish")
    assert done["status"] == "completed", done

    seen = models_seen()[before:]
    assert OVERRIDE in seen, f"provider never received the override: {seen}"
    # override 는 planner 노드 하나에만 걸었다 — 나머지 노드는 manifest 기본 모델을 쓴다
    assert any(model != OVERRIDE for model in seen), seen
