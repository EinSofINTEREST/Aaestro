"""The agent editor keeps the declarations its form does not show (#377).

폼이 다루지 않는 블록(`model.fallbacks` · `mcp` · `runtime.resources` …)은 편집기가 불러온
원본 위에서 보존돼야 한다. 순수 함수 `patchAgent` 를 **실제 브라우저의 모듈 로더**로 돌린다 —
저장소에는 노드 런타임이 없고, 브라우저 E2E 가 이미 JS 를 실행하는 유일한 경로다.
스택·control plane 은 필요 없다 (client.js 는 DOM 을 모른다).
"""

from __future__ import annotations

import copy

import pytest

from aaestro.core.manifest import AgentManifest
from aaestro.ui import UI_ROOT
from tests.e2e.test_stack import requires_docker
from tests.e2e.test_ui_browser import browser  # noqa: F401 — 같은 브라우저 fixture 를 쓴다

pytestmark = [pytest.mark.e2e, requires_docker]

ORIGIN = "http://ui.test"

DECLARATION = {
    "apiVersion": "aaestro/v1",
    "kind": "Agent",
    "metadata": {"name": "rich", "version": "0.2.0", "description": "before", "group": "research"},
    "spec": {
        "model": {
            "provider": "anthropic",
            "name": "claude-sonnet-5",
            "max_tokens": 4096,
            "effort": "high",
            "fallbacks": [
                {"provider": "anthropic", "name": "claude-haiku-4-5", "on": ["rate_limit"]}
            ],
            "retry": {"rate_limit": {"start_s": 10, "attempts": 5}},
            "circuit": {"max_failures": 5, "reset_s": 60},
        },
        "promptset": {"ref": "promptsets/researcher@0.1.0"},
        "skillsets": [
            {"ref": "skillsets/web-search@0.2.0"},
            {"ref": "skillsets/summarize@0.1.0"},
        ],
        "memory": {
            "spaces": [{"ref": "memorysets/agent-longterm@0.1.0", "as": "longterm", "mode": "ro"}]
        },
        "mcp": {
            "servers": [{"name": "fs", "transport": "stdio", "command": ["mcp-server-filesystem"]}]
        },
        "a2a": {"enabled": True, "capabilities": {"streaming": True}},
        "runtime": {
            "image": "aaestro/agent-base:0.1.0",
            "resources": {"cpu": "2.0", "memory": "2Gi"},
            "env_allowlist": ["ANTHROPIC_API_KEY"],
        },
    },
}
# 서버가 `GET /v1/agents/{name}` 으로 실제로 주는 모양 — `api_version`·`alias`·null 기본값 (#377)
ORIGINAL = AgentManifest.model_validate(DECLARATION).model_dump(mode="json")

# 폼이 원본 그대로를 보여 주고 설명만 바꾼 상태
UNTOUCHED_FORM = {
    "name": "rich",
    "version": "0.2.1",
    "description": "after",
    "group": "research",
    "provider": "anthropic",
    "model": "claude-sonnet-5",
    "promptset": "promptsets/researcher@0.1.0",
    "skillsets": ["skillsets/web-search@0.2.0", "skillsets/summarize@0.1.0"],
    "memorysets": ["memorysets/agent-longterm@0.1.0"],
    "image": "aaestro/agent-base:0.1.0",
    "envAllowlist": ["ANTHROPIC_API_KEY"],
    "entrypoint": "",
    "a2a": True,
}

PATCH = """
async ([original, form]) => {
  const { patchAgent } = await import("./client.js");
  const snapshot = JSON.stringify(original);
  const patched = patchAgent(original, form);
  return { patched, originalUntouched: JSON.stringify(original) === snapshot };
}
"""

WITH_SELECTED = """
async ([pairs, selected]) => {
  const { withSelected } = await import("./client.js");
  return withSelected(pairs, selected);
}
"""


@pytest.fixture
def page(browser):  # noqa: F811
    context = browser.new_context()
    page = context.new_page()
    client_js = (UI_ROOT / "client.js").read_text(encoding="utf-8")
    page.route(
        f"{ORIGIN}/client.js",
        lambda route: route.fulfill(body=client_js, content_type="text/javascript"),
    )
    page.route(
        f"{ORIGIN}/", lambda route: route.fulfill(body="<html></html>", content_type="text/html")
    )
    page.goto(f"{ORIGIN}/")
    yield page
    context.close()


def patch(page, original, form):
    result = page.evaluate(PATCH, [original, form])
    assert result["originalUntouched"]
    return result["patched"]


def valid(document: dict) -> AgentManifest:
    """저장 전 검증과 같은 경계 — 서버 스키마가 받아들이는 문서여야 한다."""
    return AgentManifest.model_validate(document)


def test_untouched_blocks_survive_a_description_edit(page):
    patched = patch(page, ORIGINAL, UNTOUCHED_FORM)

    expected = copy.deepcopy(DECLARATION)
    expected["metadata"]["version"] = "0.2.1"
    expected["metadata"]["description"] = "after"
    assert valid(patched) == valid(expected)
    assert "api_version" not in patched


def test_form_values_win_over_the_original(page):
    form = {
        **UNTOUCHED_FORM,
        "skillsets": [],
        "memorysets": [],
        "group": "",
        "image": "",
        "envAllowlist": [],
        "provider": "openai",
        "model": "gpt-x",
        "a2a": False,
    }

    manifest = valid(patch(page, ORIGINAL, form))

    assert manifest.spec.skillsets == ()
    assert manifest.spec.memory.spaces == ()
    assert manifest.metadata.group is None
    assert manifest.spec.runtime.image is None
    assert manifest.spec.runtime.env_allowlist == ()
    assert manifest.spec.runtime.resources == valid(DECLARATION).spec.runtime.resources
    assert (manifest.spec.model.provider, manifest.spec.model.name) == ("openai", "gpt-x")
    assert manifest.spec.model.fallbacks == valid(DECLARATION).spec.model.fallbacks
    assert manifest.spec.a2a.enabled is False
    assert manifest.spec.a2a.capabilities.streaming is True


def test_a_new_memory_space_beside_a_loaded_one_is_valid(page):
    """원본에서 온 `alias` 와 새로 만든 `as` 가 한 문서에 섞여도 유효하다."""
    form = {**UNTOUCHED_FORM, "memorysets": [*UNTOUCHED_FORM["memorysets"], "memorysets/x@0.1.0"]}

    spaces = valid(patch(page, ORIGINAL, form)).spec.memory.spaces

    assert [(s.alias, s.mode.value) for s in spaces] == [("longterm", "ro"), ("x", "rw")]


def test_a_new_agent_is_assembled_from_the_form_alone(page):
    form = {**UNTOUCHED_FORM, "name": "fresh", "a2a": False, "memorysets": [], "group": ""}

    patched = patch(page, None, form)

    assert patched["metadata"] == {"name": "fresh", "version": "0.2.1", "description": "after"}
    assert patched["spec"]["model"] == {"provider": "anthropic", "name": "claude-sonnet-5"}
    assert set(patched["spec"]) == {"model", "promptset", "skillsets", "runtime"}
    valid(patched)


def test_renaming_does_not_carry_the_hidden_declarations(page):
    """폼에 보이지 않는 선언(mcp · egress · volumes 는 권한이다)이 다른 이름으로 복제되지 않는다."""
    form = {**UNTOUCHED_FORM, "name": "copy"}

    renamed = patch(page, ORIGINAL, form)

    assert renamed == patch(page, None, form)
    manifest = valid(renamed)
    assert manifest.spec.mcp.servers == ()
    assert manifest.spec.model.fallbacks == ()
    assert manifest.spec.runtime.resources != valid(DECLARATION).spec.runtime.resources


def test_values_missing_from_the_catalog_stay_selectable(page):
    pairs = [["a@1", "a@1"], ["b@1", "b@1"]]

    merged = page.evaluate(WITH_SELECTED, [pairs, ["b@1", "gone@1", "gone@1", ""]])

    assert merged == [*pairs, ["gone@1", "gone@1 (카탈로그에 없음)"]]
