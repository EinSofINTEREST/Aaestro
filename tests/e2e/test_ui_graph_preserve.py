"""The graph editor keeps the declarations its form does not show (#385).

노드의 `model` · `retry` · `timeout_s` · 서브그래프(`graph`), `spec.memory`, `state.checkpointer`,
`service.idle.multiplier` 는 폼에 칸이 없다. 순수 함수 `patchGraph` 를 **실제 브라우저의 모듈
로더**로 돌려 불러온 원본 위에서 보존되는지 본다 (#377 의 에이전트 테스트와 같은 방식).
스택·control plane 은 필요 없다 (client.js 는 DOM 을 모른다).
"""

from __future__ import annotations

import copy

import pytest

from aaestro.core.topology import GraphTopology
from aaestro.ui import UI_ROOT
from tests.e2e.test_stack import requires_docker
from tests.e2e.test_ui_browser import browser  # noqa: F401 — 같은 브라우저 fixture 를 쓴다

pytestmark = [pytest.mark.e2e, requires_docker]

ORIGIN = "http://ui.test"

DECLARATION = {
    "apiVersion": "aaestro/v1",
    "kind": "Graph",
    "metadata": {"name": "rich", "version": "0.3.0", "description": "before"},
    "spec": {
        "mode": "service",
        "goal": "watch",
        "state": {
            "fields": {"query": {"type": "string", "required": True}},
            "checkpointer": "postgres",
        },
        "memory": {
            "spaces": [{"ref": "memorysets/run-scratch@0.1.0", "as": "scratch"}],
        },
        "service": {
            "idle": {"min_delay_s": 30, "max_delay_s": 600, "multiplier": 3.0},
            "max_failure_streak": 7,
        },
        "nodes": [
            {
                "id": "writer",
                "agent": "agents/writer@0.4.0",
                "input_map": {"query": "state.query"},
                "output_map": {"draft": "output.draft"},
                "retry": 2,
                "timeout_s": 90,
                "model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
            },
            {"id": "sub", "graph": "graphs/sub-review@1.0.0", "retry": 1},
            {
                "id": "extra",
                "agent": "agents/planner@0.4.0",
                "retry": 4,
                # 키의 유무가 의미다 — null 은 "아래 계층 값을 지운다" 이다
                "model": {
                    "max_tokens": None,
                    "effort": None,
                    "fallbacks": None,
                    "retry": None,
                    "circuit": None,
                },
            },
        ],
        "edges": [
            {"from": "START", "to": "writer"},
            {"from": "writer", "to": "sub", "condition": "state.query", "max_iterations": 2},
            {"from": "sub", "to": "extra"},
            {"from": "extra", "to": "END"},
        ],
        "connections": [{"caller": "writer", "callee": "extra"}],
    },
}
# 서버가 `GET /v1/graphs/{name}` 으로 실제로 주는 모양 — `api_version` · `source`/`target` ·
# `schema_ref`/`declared_fields` · null 기본값 (#385)
ORIGINAL = GraphTopology.model_validate(DECLARATION).model_dump(mode="json")

# 폼이 원본 그대로를 보여 주고 설명만 바꾼 상태 (서브그래프 노드의 에이전트 칸은 비어 있다)
UNTOUCHED_FORM = {
    "name": "rich",
    "version": "0.3.1",
    "description": "after",
    "mode": "service",
    "goal": "watch",
    "state": {"fields": {"query": {"type": "string", "required": True}}},
    "nodes": [
        {
            "id": "writer",
            "agent": "agents/writer@0.4.0",
            "inputMap": {"query": "state.query"},
            "outputMap": {"draft": "output.draft"},
        },
        {"id": "sub", "agent": "", "inputMap": {}, "outputMap": {}},
        {"id": "extra", "agent": "agents/planner@0.4.0", "inputMap": {}, "outputMap": {}},
    ],
    "edges": [
        {"from": "START", "to": "writer", "condition": "", "maxIterations": ""},
        {"from": "writer", "to": "sub", "condition": "state.query", "maxIterations": 2},
        {"from": "sub", "to": "extra", "condition": "", "maxIterations": ""},
        {"from": "extra", "to": "END", "condition": "", "maxIterations": ""},
    ],
    "connections": [{"caller": "writer", "callee": "extra"}],
    "service": {"minDelay": 30, "maxDelay": 600, "failureStreak": 7},
}

PATCH = """
async ([original, form]) => {
  const { patchGraph } = await import("./client.js");
  const snapshot = JSON.stringify(original);
  const patched = patchGraph(original, form);
  return { patched, originalUntouched: JSON.stringify(original) === snapshot };
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


def valid(document: dict) -> GraphTopology:
    """저장 전 검증과 같은 경계 — 서버 스키마가 받아들이는 문서여야 한다."""
    return GraphTopology.model_validate(document)


def test_untouched_declarations_survive_a_description_edit(page):
    patched = patch(page, ORIGINAL, UNTOUCHED_FORM)

    expected = copy.deepcopy(DECLARATION)
    expected["metadata"]["version"] = "0.3.1"
    expected["metadata"]["description"] = "after"
    assert valid(patched) == valid(expected)
    assert "api_version" not in patched


def test_the_response_shape_alone_is_what_the_server_would_reject():
    """조회 응답을 그대로 PUT 하면 거절된다 — 편집기가 별칭으로 되돌려야 하는 이유."""
    with pytest.raises(ValueError, match="apiVersion"):
        valid(ORIGINAL)


def test_form_values_win_over_the_original(page):
    form = copy.deepcopy(UNTOUCHED_FORM)
    form["mode"] = "mission"
    form["goal"] = "done"
    form["nodes"][0]["inputMap"] = {}
    form["nodes"][0]["agent"] = "agents/writer@0.5.0"
    form["edges"][1]["condition"] = ""
    form["edges"][1]["maxIterations"] = ""
    form["connections"] = []

    topology = valid(patch(page, ORIGINAL, form))

    assert topology.spec.service is None
    assert topology.spec.goal == "done"
    writer = topology.spec.nodes[0]
    assert (writer.agent, writer.input_map) == ("agents/writer@0.5.0", {})
    assert (writer.retry, writer.timeout_s) == (2, 90)
    assert topology.spec.edges[1].condition is None
    assert topology.spec.edges[1].max_iterations is None
    assert topology.spec.connections == ()
    assert topology.spec.memory == valid(DECLARATION).spec.memory


def test_nodes_edges_and_connections_removed_in_the_form_stay_removed(page):
    form = copy.deepcopy(UNTOUCHED_FORM)
    form["nodes"] = form["nodes"][:2]
    form["edges"] = [form["edges"][0], form["edges"][1], {**form["edges"][3], "from": "sub"}]
    form["edges"][2]["to"] = "END"
    form["connections"] = []

    topology = valid(patch(page, ORIGINAL, form))

    assert [n.id for n in topology.spec.nodes] == ["writer", "sub"]
    assert len(topology.spec.edges) == 3
    assert topology.spec.connections == ()


def test_a_node_renamed_in_the_form_does_not_inherit_the_old_declaration(page):
    form = copy.deepcopy(UNTOUCHED_FORM)
    form["nodes"][2]["id"] = "renamed"
    form["edges"][2]["to"] = "renamed"
    form["edges"][3]["from"] = "renamed"
    form["connections"] = [{"caller": "writer", "callee": "renamed"}]

    nodes = {n.id: n for n in valid(patch(page, ORIGINAL, form)).spec.nodes}

    assert nodes["renamed"].retry == 0
    assert nodes["writer"].retry == 2


def test_picking_an_agent_for_a_subgraph_node_replaces_the_subgraph(page):
    form = copy.deepcopy(UNTOUCHED_FORM)
    form["nodes"][1]["agent"] = "agents/planner@0.4.0"

    sub = {n.id: n for n in valid(patch(page, ORIGINAL, form)).spec.nodes}["sub"]

    assert (sub.agent, sub.graph) == ("agents/planner@0.4.0", None)


def test_renaming_does_not_carry_the_hidden_declarations(page):
    """폼에 보이지 않는 선언이 다른 이름의 새 그래프로 복제되지 않는다."""
    # 서브그래프 노드의 대상(`graph`)도 숨은 선언이라 복제되지 않는다 — 에이전트를 고르게 한다
    nodes = [dict(n) for n in UNTOUCHED_FORM["nodes"]]
    nodes[1]["agent"] = "agents/planner@0.4.0"
    form = {**UNTOUCHED_FORM, "name": "copy", "nodes": nodes}

    renamed = patch(page, ORIGINAL, form)

    assert renamed == patch(page, None, form)
    topology = valid(renamed)
    assert topology.spec.memory.spaces == ()
    assert topology.spec.state.checkpointer == "default"
    assert topology.spec.service.idle.multiplier == 2.0
    assert [n.retry for n in topology.spec.nodes] == [0, 0, 0]
    assert topology.spec.nodes[0].model is None


def test_a_new_graph_is_assembled_from_the_form_alone(page):
    form = {
        **UNTOUCHED_FORM,
        "name": "fresh",
        "mode": "mission",
        "nodes": [UNTOUCHED_FORM["nodes"][0], UNTOUCHED_FORM["nodes"][2]],
        "edges": [
            UNTOUCHED_FORM["edges"][0],
            {"from": "writer", "to": "extra", "condition": "", "maxIterations": ""},
            UNTOUCHED_FORM["edges"][3],
        ],
        "connections": [],
    }

    patched = patch(page, None, form)

    assert patched["metadata"] == {"name": "fresh", "version": "0.3.1", "description": "after"}
    assert set(patched["spec"]) == {"mode", "goal", "state", "nodes", "edges", "connections"}
    assert patched["spec"]["nodes"][0] == {
        "id": "writer",
        "agent": "agents/writer@0.4.0",
        "input_map": {"query": "state.query"},
        "output_map": {"draft": "output.draft"},
    }
    valid(patched)


def test_explicit_nulls_inside_hidden_declarations_survive_as_keys(page):
    """pydantic 동등 비교는 `model_fields_set` 을 보지 않는다 — 저장 본문의 키를 직접 본다."""
    patched = patch(page, ORIGINAL, UNTOUCHED_FORM)

    extra = next(n for n in patched["spec"]["nodes"] if n["id"] == "extra")
    assert extra["model"] == {
        "max_tokens": None,
        "effort": None,
        "fallbacks": None,
        "retry": None,
        "circuit": None,
    }
    assert valid(patched).spec.nodes[2].model.model_fields_set == {
        "max_tokens",
        "effort",
        "fallbacks",
        "retry",
        "circuit",
    }


def test_the_state_fields_json_goes_through_exactly_as_edited(page):
    fields = {
        "a": {"type": "string", "default": ""},
        "b": {"type": "object", "default": {"k": "", "n": None}},
    }
    form = {**UNTOUCHED_FORM, "state": {"fields": fields}}

    patched = patch(page, ORIGINAL, form)

    assert patched["spec"]["state"]["fields"] == fields


def test_the_response_defaults_are_not_sent_back_as_nulls(page):
    patched = patch(page, ORIGINAL, UNTOUCHED_FORM)

    writer = patched["spec"]["nodes"][0]
    sub = patched["spec"]["nodes"][1]
    assert "agent" not in sub and "timeout_s" not in sub and "model" not in sub
    assert writer["timeout_s"] == 90
    assert "api_version" not in patched


def test_values_cleared_in_the_form_stay_cleared(page):
    form = copy.deepcopy(UNTOUCHED_FORM)
    form["description"] = ""
    form["nodes"][0]["outputMap"] = {}
    form["nodes"][2]["agent"] = ""

    patched = patch(page, ORIGINAL, form)

    assert "description" not in patched["metadata"]
    nodes = {n["id"]: n for n in patched["spec"]["nodes"]}
    assert "output_map" not in nodes["writer"]
    assert "agent" not in nodes["extra"]


def test_service_form_values_win_over_the_original(page):
    form = copy.deepcopy(UNTOUCHED_FORM)
    form["service"] = {"minDelay": 5, "maxDelay": 50, "failureStreak": 2}

    service = patch(page, ORIGINAL, form)["spec"]["service"]

    assert service == {
        "idle": {"min_delay_s": 5, "max_delay_s": 50, "multiplier": 3.0},
        "max_failure_streak": 2,
    }
