"""The control plane serves the operator UI (#245)."""

from __future__ import annotations

import re

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


def test_the_agent_editor_saves_over_the_declaration_of_its_own_name():
    """저장 문서는 저장 대상 이름의 선언 위에서 만든다 (#377).

    동작은 페이지 전체를 띄우는 브라우저 E2E(`tests/e2e/test_ui_agent_page.py`)가 실행해 확인하고,
    여기서는 저장 경로가 그 원본 해석(`agentBase`)을 거치는지만 본다.
    """
    source = (UI_ROOT / "app.js").read_text(encoding="utf-8")
    submit = source.split('agentForm.addEventListener("submit"')[1].split("\n});")[0]

    assert "currentAgentDocument()" in submit
    assert "agentDocument(" not in submit.replace("currentAgentDocument(", "")


def test_the_unbuilt_warning_compares_the_recorded_tag_with_the_expected_one():
    """서버 게이트(#383)와 같은 판정이 화면에도 있어야 한다 — node 없이 소스에서 배선만 본다."""
    source = (UI_ROOT / "client.js").read_text(encoding="utf-8")
    start = source.index("export function unbuiltAgents")
    body = source[start : source.index("export function moduleRef")]

    assert "expected_image" in body
    assert "image.image" in body


def test_the_graph_editor_saves_over_the_declaration_of_its_own_name():
    """그래프 저장·검증 경로도 저장 대상 이름의 원본(`graphBase`)을 거친다 (#385).

    patchGraph 의 동작은 브라우저 E2E(`tests/e2e/test_ui_graph_preserve.py`)가 실행해 확인한다.
    """
    source = (UI_ROOT / "app.js").read_text(encoding="utf-8")
    submit = source.split('graphForm.addEventListener("submit"')[1].split("\n});")[0]
    validate = source.split("async function validateGraph(doc)")[1].split("\n}")[0]

    assert submit.count("currentGraphDocument()") == 1  # 검증한 문서를 그대로 저장한다
    assert "validateGraph(doc)" in submit
    assert "loadedGraph = structuredClone(doc)" in submit  # 저장한 문서가 다음 저장의 원본
    assert "currentGraphDocument()" in validate
    assert "graphDocument(" not in (submit + validate).replace("currentGraphDocument(", "")


# --- 이름 입력 pattern (#397) ---------------------------------------------------------------
# 브라우저는 pattern 을 `^(?:…)$` 로 감싸 `v` 플래그로 컴파일한다. Python `re` 에는 `v` 가 없어
# 구문 검사는 정적이다 (클래스 안 특수문자 · 클래스 밖 `\-`). 실제 컴파일은 브라우저 E2E 가 본다.


def _name_patterns() -> list[str]:
    html = (UI_ROOT / "index.html").read_text(encoding="utf-8")
    return re.findall(r'<input name="name"[^>]*\spattern="([^"]*)"', html)


def _class_bodies(pattern: str) -> list[str]:
    return re.findall(r"(?<!\\)\[((?:\\.|[^\]\\])*)\]", pattern)


def test_name_inputs_every_name_input_declares_a_pattern():
    # 그래프 · 에이전트 폼 둘 — 하나라도 빠지면 형식 검사가 조용히 꺼진다
    assert len(_name_patterns()) == 2


@pytest.mark.parametrize("pattern", _name_patterns())
def test_name_pattern_has_no_construct_invalid_under_the_v_flag(pattern):
    for body in _class_bodies(pattern):
        # v 플래그: 클래스 안의 이스케이프 안 된 ( ) [ ] { } / | - (범위 구문 밖) 은 무효
        assert not re.search(r"(?<!\\)[()\[\]{}/|]", body), body
        assert not re.search(r"(?<![\w\\])-|-(?![\w])", body), body
        assert not re.search(r"([&!#$%*+,.:;<=>?@^`~-])\1", body)  # 이중 문장부호
    outside = re.sub(r"\[(?:\\.|[^\]\\])*\]", "", pattern)
    assert "\\-" not in outside  # v/u 모드에서 클래스 밖 `\-` 는 구문 오류


NAME_CASES = [
    "a", "abc", "a1", "1a", "123", "a-b", "a-b-c", "a1-2b", "-a", "a-", "a--b", "-", "--",
    "", "A", "aB", "a_b", "a b", "a.b", "a-B", " a", "a\n", "é",
]  # fmt: skip


@pytest.mark.parametrize("pattern", _name_patterns())
@pytest.mark.parametrize("value", NAME_CASES)
def test_name_pattern_agrees_with_the_server_rule(pattern, value):
    from aaestro.core.manifest import NAME_PATTERN

    # HTML pattern 은 전체 일치 — Python 에서는 fullmatch 로 흉내 (`$` 의 개행 허용을 피한다)
    browser = re.fullmatch(pattern, value) is not None
    server = NAME_PATTERN.match(value) is not None and not value.endswith("\n")
    assert browser == server, value
