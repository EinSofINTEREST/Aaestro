"""AgentCard wiring at the daemon.

card 는 manifest 와 **실제 로드된 tool** 로부터 생성한다 — 손으로 쓰면 skill
목록이 빠지고 (03 AgentCard 1), peer 는 이 에이전트가 뭘 할 수 있는지 알 수
없다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from fastapi.testclient import TestClient

from aaestro.agentd.__main__ import build_app, build_executor, load_manifest

REPO_ROOT = Path(__file__).resolve().parents[3]
RESEARCHER = REPO_ROOT / "agents" / "researcher" / "manifest.yaml"


@pytest.fixture
async def served(monkeypatch):
    """표준 executor 로 세운 앱과 그 tool 목록."""
    monkeypatch.delenv("AAESTRO_EXECUTOR", raising=False)
    monkeypatch.setenv("AAESTRO_ROOT", str(REPO_ROOT))
    manifest = load_manifest(RESEARCHER)
    executor = await build_executor(manifest)
    app = build_app(manifest, executor, tools=executor.tool_schemas)
    with TestClient(app) as client:
        yield client, executor


async def test_card_advertises_the_loaded_skills(served):
    """수동 작성 카드에는 skill 이 하나도 없었다."""
    client, executor = served

    body = client.get("/v1/card").json()

    assert [skill["name"] for skill in body["skills"]] == [
        spec.name for spec in executor.tool_schemas
    ]


async def test_card_carries_declared_capabilities(served):
    client, _executor = served

    capabilities = client.get("/v1/card").json()["capabilities"]

    assert capabilities["streaming"] is True
    assert "push_notifications" in capabilities


async def test_well_known_matches_the_control_api(served):
    """두 곳을 따로 만들면 peer 가 보는 계약이 조용히 갈라진다."""
    client, _executor = served

    assert client.get(AGENT_CARD_WELL_KNOWN_PATH).json() == client.get("/v1/card").json()


async def test_advertised_skills_are_all_runnable(served):
    """부를 수 없는 능력을 광고하면 peer 가 헛걸음한다."""
    client, executor = served

    advertised = {skill["name"] for skill in client.get("/v1/card").json()["skills"]}

    assert advertised == {spec.name for spec in executor.tool_schemas}
    assert advertised <= set(executor.binding.tools._skills)
