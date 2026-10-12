"""The control plane serves the operator UI (#245)."""

from __future__ import annotations

import httpx
import pytest

from aaestro.orchestrator.control import create_app
from aaestro.orchestrator.runstore import InMemoryRunStore
from aaestro.ui import UI_ROOT


@pytest.fixture
async def api():
    app = create_app(InMemoryRunStore(), token="secret")  # noqa: S106 — 테스트 토큰
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        yield client


async def test_the_root_redirects_to_the_ui(api):
    response = await api.get("/")

    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/ui/"


async def test_the_page_and_its_scripts_are_served_without_a_token(api):
    """화면은 비밀이 아니다 — 비밀은 화면이 부르는 API 에 있다."""
    index = await api.get("/ui/")
    app_js = await api.get("/ui/app.js")
    client_js = await api.get("/ui/client.js")
    css = await api.get("/ui/app.css")

    assert index.status_code == 200 and "<title>Aaestro</title>" in index.text
    assert app_js.status_code == 200 and 'from "./client.js"' in app_js.text
    assert client_js.status_code == 200 and "createClient" in client_js.text
    assert css.status_code == 200


async def test_the_api_behind_the_ui_still_requires_the_token(api):
    assert (await api.get("/v1/runs")).status_code == 401


def test_the_ui_talks_rest_only():
    """UI 코드에 파일 경로·Docker 호출이 없다 — REST 만 (#245 완료 조건).

    모듈 ref(`agents/planner@0.4.0`)는 경로가 아니라 카탈로그 식별자다 — 그것은 허용한다.
    """
    for name in ("app.js", "client.js"):
        source = (UI_ROOT / name).read_text(encoding="utf-8").lower()
        # `Dockerfile` 은 재료의 파일 **이름**이다 (#267) — 런타임 호출이 아니다
        assert "docker" not in source.replace("dockerfile", "")
        # 파일 경로와 노드 런타임 API 만 막는다 — API 응답의 `manifest.spec` 같은
        # 필드 접근까지 걸면 오탐이 난다 (경로가 아니라 JSON 이다)
        for marker in (
            ".yaml",
            "manifest.yaml",
            "/workspace",
            "/data/",
            "fs.",
            "child_process",
            "require(",
        ):
            assert marker not in source, (name, marker)


def test_the_client_covers_every_ui_facing_route():
    """라우트가 늘었는데 클라이언트에 없으면 화면이 그 기능을 못 쓴다."""
    source = (UI_ROOT / "client.js").read_text(encoding="utf-8")
    for path in (
        "/v1/agents",
        "/v1/graphs",
        "/v1/groups",
        "/v1/modules/",
        "/v1/validate",
        "/v1/deployments",
        "/v1/runs",
        "/drain",
        "/resume",
        "/materials",
        "/image",
    ):
        assert path in source, path


def test_the_unbuilt_warning_compares_the_recorded_tag_with_the_expected_one():
    """서버 게이트(#383)와 같은 판정이 화면에도 있어야 한다 — node 없이 소스에서 배선만 본다."""
    source = (UI_ROOT / "client.js").read_text(encoding="utf-8")
    start = source.index("export function unbuiltAgents")
    body = source[start : source.index("export function moduleRef")]

    assert "expected_image" in body
    assert "image.image" in body
