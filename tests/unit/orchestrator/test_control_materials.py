"""Build-material routes (#264)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml

from aaestro.authoring import Author
from aaestro.catalog import Catalog
from aaestro.core.errors import ErrorCode
from aaestro.materials import InMemoryMaterialStore
from aaestro.orchestrator.control import create_app
from aaestro.orchestrator.runstore import InMemoryRunStore

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    for name in ("graphs", "groups"):
        (tmp_path / name).mkdir()
    (tmp_path / "modules" / "promptsets" / "solo" / "0.1.0").mkdir(parents=True)
    (tmp_path / "modules" / "promptsets" / "solo" / "0.1.0" / "promptset.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "aaestro/v1",
                "kind": "Promptset",
                "metadata": {"name": "solo", "version": "0.1.0"},
                "spec": {"engine": "jinja2", "templates": {"default": {"file": "t.j2"}}},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "agents" / "custom").mkdir(parents=True)
    (tmp_path / "agents" / "custom" / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "aaestro/v1",
                "kind": "Agent",
                "metadata": {"name": "custom", "version": "0.1.0"},
                "spec": {
                    "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                    "promptset": {"ref": "promptsets/solo@0.1.0"},
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "groups" / "global.yaml").write_text(
        (REPO_ROOT / "groups" / "global.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
async def api(workspace):
    author = Author(catalog=Catalog.under(workspace), materials=InMemoryMaterialStore())
    app = create_app(InMemoryRunStore(), catalog=Catalog.under(workspace), author=author)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://cp") as client:
        yield client


async def test_materials_round_trip_through_the_api(api):
    files = {"Dockerfile": "FROM aaestro/agent-base:0.1.0", "src/agent.py": "MARK = 1"}

    put = await api.put("/v1/agents/custom/materials", json={"files": files})
    got = await api.get("/v1/agents/custom/materials")

    assert put.status_code == 200
    assert got.json()["files"] == files
    assert got.json()["version"] == "0.1.0"


async def test_an_agent_without_materials_reports_an_empty_set(api):
    response = await api.get("/v1/agents/custom/materials")

    assert response.status_code == 200
    assert response.json()["files"] == {}


async def test_a_path_outside_the_build_context_is_400(api):
    response = await api.put("/v1/agents/custom/materials", json={"files": {"../x.py": "x"}})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.VAL_002


async def test_a_bad_body_is_400(api):
    response = await api.put("/v1/agents/custom/materials", json={"files": "not-a-map"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.VAL_002


async def test_changing_materials_without_a_version_bump_is_400(api):
    await api.put("/v1/agents/custom/materials", json={"files": {"src/a.py": "one"}})

    response = await api.put("/v1/agents/custom/materials", json={"files": {"src/a.py": "two"}})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.MOD_002


async def test_delete_removes_the_materials_only(api):
    await api.put("/v1/agents/custom/materials", json={"files": {"src/a.py": "one"}})

    deleted = await api.delete("/v1/agents/custom/materials")

    assert deleted.status_code == 204
    assert (await api.get("/v1/agents/custom/materials")).json()["files"] == {}
    assert (await api.get("/v1/agents/custom")).status_code == 200


async def test_an_unknown_agent_is_404(api):
    assert (await api.get("/v1/agents/nobody/materials")).status_code == 404


async def test_without_a_store_the_routes_report_a_config_problem(workspace):
    """재료 표면을 열지 않은 조립에서 조용히 성공하면 빌드가 빈 재료로 돈다."""
    author = Author(catalog=Catalog.under(workspace))
    app = create_app(InMemoryRunStore(), catalog=Catalog.under(workspace), author=author)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://cp") as client:
        response = await client.get("/v1/agents/custom/materials")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.CFG_001
