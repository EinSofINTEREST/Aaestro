"""Unit tests for graph state resolution and merge rules."""

from __future__ import annotations

import pytest

from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.graphs.schemas import ResearchState
from aaestro.orchestrator.state import (
    extract_input,
    merge_output,
    resolve_state_schema,
    schema_defaults,
    state_fields,
    validate_state,
)
from aaestro.orchestrator.topology import NodeSpec


def make_node(**overrides: object) -> NodeSpec:
    """input_map/output_map 만 바꿔가며 쓰는 최소 노드."""
    base: dict[str, object] = {"id": "planner", "agent": "agents/planner@0.1.0"}
    base.update(overrides)
    return NodeSpec.model_validate(base)


def assert_graph_003(exc_info: pytest.ExceptionInfo[AaestroError]) -> None:
    """state 불일치/병합 실패는 GRAPH_003 으로 보고된다."""
    assert exc_info.value.code == "GRAPH_003"
    assert exc_info.value.category is ErrorCategory.GRAPH


# --- schema 해석 ------------------------------------------------------------


def test_resolve_state_schema_returns_model():
    assert resolve_state_schema("aaestro.graphs.schemas:ResearchState") is ResearchState


def test_resolve_state_schema_rejects_non_model():
    with pytest.raises(AaestroError) as exc_info:
        resolve_state_schema("aaestro.graphs.conditions:needs_research")

    assert_graph_003(exc_info)
    assert "not a pydantic model" in exc_info.value.message


def test_resolve_state_schema_rejects_unimportable():
    with pytest.raises(AaestroError) as exc_info:
        resolve_state_schema("no.such.module:State")

    assert exc_info.value.code == "GRAPH_001"


def test_state_fields_lists_declared_names():
    assert state_fields(ResearchState) == frozenset(
        {"query", "plan", "needs_research", "findings", "report"}
    )


# --- input 추출 -------------------------------------------------------------


def test_extract_input_reads_declared_state_keys():
    node = make_node(input_map={"query": "state.query"})

    assert extract_input(node, {"query": "q", "plan": "p"}) == {"query": "q"}


def test_extract_input_ignores_undeclared_state_keys():
    """input_map 에 없는 state 키는 태스크 입력으로 넘어가지 않는다."""
    node = make_node(input_map={"query": "state.query"})

    result = extract_input(node, {"query": "q", "secret": "s"})

    assert "secret" not in result


def test_extract_input_treats_non_state_source_as_literal():
    node = make_node(input_map={"mode": "fast", "depth": "state.plan"})

    assert extract_input(node, {"plan": 2}) == {"mode": "fast", "depth": 2}


def test_extract_input_supports_nested_paths():
    node = make_node(input_map={"city": "state.location.city"})

    assert extract_input(node, {"location": {"city": "seoul"}}) == {"city": "seoul"}


def test_extract_input_missing_field_raises_graph_003():
    node = make_node(input_map={"query": "state.absent"})

    with pytest.raises(AaestroError) as exc_info:
        extract_input(node, {"query": "q"})

    assert_graph_003(exc_info)
    assert exc_info.value.details["node_id"] == "planner"


def test_extract_input_with_empty_map_is_empty():
    assert extract_input(make_node(), {"query": "q"}) == {}


# --- output 병합 ------------------------------------------------------------


def test_merge_output_projects_declared_keys_only():
    """노드가 state 전체를 덮어쓰지 못한다 — 04 Graph Rules 4."""
    node = make_node(output_map={"plan": "output.plan"})

    update = merge_output(node, {"plan": "P", "scratch": "ignored"})

    assert update == {"plan": "P"}


def test_merge_output_without_map_produces_no_update():
    node = make_node()

    assert merge_output(node, {"plan": "P"}) == {}


def test_merge_output_accepts_bare_source_key():
    node = make_node(output_map={"plan": "plan"})

    assert merge_output(node, {"plan": "P"}) == {"plan": "P"}


def test_merge_output_supports_nested_source():
    node = make_node(output_map={"report": "output.result.text"})

    assert merge_output(node, {"result": {"text": "R"}}) == {"report": "R"}


def test_merge_output_missing_source_raises_graph_003():
    node = make_node(output_map={"plan": "output.absent"})

    with pytest.raises(AaestroError) as exc_info:
        merge_output(node, {"plan": "P"})

    assert_graph_003(exc_info)


def test_merge_output_rejects_undeclared_state_target():
    node = make_node(output_map={"unknown_field": "output.plan"})

    with pytest.raises(AaestroError) as exc_info:
        merge_output(node, {"plan": "P"}, schema=ResearchState)

    assert_graph_003(exc_info)
    assert "unknown state field" in exc_info.value.message


def test_merge_output_accepts_declared_state_target():
    node = make_node(output_map={"plan": "output.plan"})

    assert merge_output(node, {"plan": "P"}, schema=ResearchState) == {"plan": "P"}


# --- state 검증 -------------------------------------------------------------


def test_validate_state_returns_normalized_mapping():
    result = validate_state(ResearchState, {"query": "q"})

    assert result["query"] == "q"
    assert result["needs_research"] is True
    assert result["findings"] == []


def test_validate_state_rejects_schema_mismatch():
    with pytest.raises(AaestroError) as exc_info:
        validate_state(ResearchState, {"query": "q", "findings": "not-a-list"})

    assert_graph_003(exc_info)


def test_validate_state_rejects_missing_required_field():
    with pytest.raises(AaestroError) as exc_info:
        validate_state(ResearchState, {})

    assert_graph_003(exc_info)


def test_extract_input_passes_non_string_literals_through():
    node = make_node(input_map={"depth": 2, "verbose": True, "tags": ["a"]})

    assert extract_input(node, {}) == {"depth": 2, "verbose": True, "tags": ["a"]}


# --- schema 기본값 (검증과 런타임의 기준 일치) ---------------------------------


def test_schema_default_is_used_when_state_lacks_the_key():
    """LangGraph 채널은 값이 설정되기 전까지 키를 만들지 않는다.

    04 규칙은 input_map 이 "schema 에 존재" 하기만 하면 통과시키므로, 런타임이
    기본값을 무시하면 검증은 통과하고 실행만 실패한다.
    """
    node = NodeSpec.model_validate(
        {"id": "writer", "agent": "agents/writer@0.1.0", "input_map": {"plan": "state.plan"}}
    )

    extracted = extract_input(node, {"query": "q"}, schema=ResearchState)

    assert extracted == {"plan": None}


def test_schema_default_factory_is_evaluated():
    """default_factory 필드도 채워져야 한다 — 리스트/딕트가 흔하다."""
    node = NodeSpec.model_validate(
        {
            "id": "writer",
            "agent": "agents/writer@0.1.0",
            "input_map": {"findings": "state.findings"},
        }
    )

    extracted = extract_input(node, {"query": "q"}, schema=ResearchState)

    assert extracted == {"findings": []}


def test_state_value_wins_over_the_schema_default():
    """실제 값이 있으면 기본값으로 덮어쓰지 않는다."""
    node = NodeSpec.model_validate(
        {"id": "writer", "agent": "agents/writer@0.1.0", "input_map": {"plan": "state.plan"}}
    )

    extracted = extract_input(node, {"plan": "실제 계획"}, schema=ResearchState)

    assert extracted == {"plan": "실제 계획"}


def test_required_field_without_a_value_still_fails():
    """기본값이 없는 필수 필드는 여전히 GRAPH_003 이어야 한다 —
    없는 값을 조용히 만들어내면 노드가 빈 입력으로 실행된다."""
    node = NodeSpec.model_validate(
        {"id": "planner", "agent": "agents/planner@0.1.0", "input_map": {"query": "state.query"}}
    )

    with pytest.raises(AaestroError) as exc_info:
        extract_input(node, {}, schema=ResearchState)

    assert exc_info.value.code == "GRAPH_003"


def test_missing_field_without_a_schema_still_fails():
    """schema 를 주지 않으면 기존 동작(엄격)을 유지한다."""
    node = NodeSpec.model_validate(
        {"id": "writer", "agent": "agents/writer@0.1.0", "input_map": {"plan": "state.plan"}}
    )

    with pytest.raises(AaestroError) as exc_info:
        extract_input(node, {})

    assert exc_info.value.code == "GRAPH_003"


def test_schema_defaults_lists_only_optional_fields():
    defaults = schema_defaults(ResearchState)

    assert "query" not in defaults  # 필수 필드
    assert defaults["plan"] is None
    assert defaults["findings"] == []
