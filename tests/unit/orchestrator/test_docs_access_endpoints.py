"""The documented access endpoints are the ones the control plane serves (#283).

문서가 곧 운영자의 절차다 — 없는 엔드포인트를 적어 두면 긴급 회수가 404 에서 멈춘다. 문서의 제목에서
경로를 뽑아 **실제 앱에 호출해** 확인하고, 앱에만 있고 문서에 없는 경로도 잡는다.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from aaestro.access.registry import AccessRegistry
from aaestro.access.store import InMemoryAccessStore
from aaestro.catalog import Catalog
from aaestro.orchestrator.control import create_app
from aaestro.orchestrator.runstore import InMemoryRunStore
from tests.fixtures.access import STEWARD, access_workspace

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTROL = "control-token"
HEADING = re.compile(r"^### `(GET|POST|DELETE) (/v1/access/[^`?]+)[^`]*`", re.MULTILINE)
SAMPLES = {"{name}": "worker", "{rule_id}": "rule-does-not-exist"}


def documented(page: Path) -> set[tuple[str, str]]:
    section = page.read_text(encoding="utf-8").split("## Access registry")[-1]
    return {(method, path) for method, path in HEADING.findall(section)}


@pytest.fixture
def registry(tmp_path: Path) -> AccessRegistry:
    catalog = Catalog.under(access_workspace(tmp_path))
    return AccessRegistry(
        store=InMemoryAccessStore(), catalog=catalog, stewards=frozenset({STEWARD})
    )


@pytest.fixture
def app(registry):
    return create_app(
        InMemoryRunStore(),
        catalog=registry.catalog,
        token=CONTROL,
        access=registry,
        enforcer_token="enforcer",  # noqa: S106 — 테스트 값
    )


@pytest.fixture
async def api(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        yield client


def test_the_documented_endpoints_are_exactly_the_ones_that_exist(app):
    served = {
        (method.upper(), path)
        for path, methods in app.openapi()["paths"].items()
        for method in methods
        if path.startswith("/v1/access/")
    }

    assert documented(REPO_ROOT / "docs" / "en" / "api.md") == served


def test_the_korean_page_documents_the_same_endpoints():
    english = documented(REPO_ROOT / "docs" / "en" / "api.md")
    korean = documented(REPO_ROOT / "docs" / "ko" / "api.md")

    assert korean == english


DOCUMENTED = sorted(documented(REPO_ROOT / "docs" / "en" / "api.md"))


@pytest.mark.parametrize(("method", "path"), DOCUMENTED)
async def test_every_documented_endpoint_answers(api, method, path):
    """실제로 부른다 — 라우트가 있고, 없는 자원에는 경로의 404 가 아니라 자기 코드로 답한다."""
    for template, sample in SAMPLES.items():
        path = path.replace(template, sample)

    response = await api.request(
        method, path, json={}, headers={"Authorization": f"Bearer {CONTROL}"}
    )

    assert response.status_code != 405, f"{method} {path} 를 그 메서드로 받지 않는다"
    if response.status_code == 404:
        assert response.json()["error"]["code"] == "NF_001", "문서의 경로가 앱에 없다"


@pytest.mark.parametrize("language", ["en", "ko"])
async def test_both_pages_document_every_field_of_the_operator_view(api, language):
    """응답 필드가 문서에 없으면 운영자는 화면이 무엇을 보여 주는지 모른다 — 두 언어 모두 (#283)."""
    answered = await api.get(
        "/v1/access/agents/worker", headers={"Authorization": f"Bearer {CONTROL}"}
    )
    page = (REPO_ROOT / "docs" / language / "api.md").read_text(encoding="utf-8")
    section = page.split("/v1/access/agents/{name}")[1].split("/v1/access/revocations")[0]

    # 설명(`field`)이든 예시 응답("field")이든, 문서에 그 필드가 보이면 된다
    missing = [
        field
        for field in answered.json()
        if f"`{field}`" not in section and f'"{field}"' not in section
    ]

    assert missing == [], f"docs/{language}/api.md 가 설명하지 않는 응답 필드: {missing}"
