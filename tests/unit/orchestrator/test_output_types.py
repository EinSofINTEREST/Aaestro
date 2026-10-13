"""Output projection respects the state schema.

`merge_output` 은 키 존재만 검사하고 **타입은 검사하지 않았다** — 선언과 다른
타입이 state 에 들어가도 그대로 통과했다 (#202).

레퍼런스 mission 그래프에서 실제로 그랬다: `findings` 는 `list[str]` 선언인데
`str` 이 들어가고 있었고, E2E 는 `report` 가 채워진 것만 보고 통과했다.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, Field

from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.orchestrator.state import merge_output
from aaestro.orchestrator.topology import NodeSpec


class State(BaseModel):
    """검증 대상 — 실제 레퍼런스 state 와 같은 모양의 필드를 담는다."""

    model_config = ConfigDict(frozen=True)

    query: str
    plan: str | None = None
    needs_research: bool = True
    findings: list[str] = Field(default_factory=list)
    notified: int = 0


def node(**output_map: str) -> NodeSpec:
    return NodeSpec(id="researcher", agent="agents/researcher@0.1.0", output_map=output_map)


def test_a_matching_projection_passes():
    update = merge_output(
        node(findings="output.findings"),
        {"findings": ["a", "b"]},
        schema=State,
    )

    assert update == {"findings": ["a", "b"]}


def test_a_string_into_a_list_field_is_refused():
    """#202 — 이 검증이 없어 레퍼런스 그래프가 계약을 어긴 채 통과했다."""
    with pytest.raises(AaestroError) as excinfo:
        merge_output(node(findings="output.findings"), {"findings": "not a list"}, schema=State)

    assert excinfo.value.code == ErrorCode.GRAPH_003
    assert excinfo.value.details["field"] == "findings"


def test_a_bool_field_stores_a_bool():
    """조건 분기가 이 값을 읽는다 — 문자열이 남으면 어떤 값이든 참이 된다.

    pydantic 은 "yes" 를 bool 로 강제 변환한다. 검증만 하고 원값을 넣으면
    state 에 문자열이 남아, 강제 변환이 아무 것도 지키지 못한다.
    """
    update = merge_output(
        node(needs_research="output.needs_research"),
        {"needs_research": "yes"},
        schema=State,
    )

    assert update["needs_research"] is True


def test_an_uncoercible_bool_is_refused():
    with pytest.raises(AaestroError) as excinfo:
        merge_output(
            node(needs_research="output.needs_research"),
            {"needs_research": ["nope"]},
            schema=State,
        )

    assert excinfo.value.code == ErrorCode.GRAPH_003


def test_a_string_into_an_int_field_is_refused():
    with pytest.raises(AaestroError) as excinfo:
        merge_output(node(notified="output.notified"), {"notified": "many"}, schema=State)

    assert excinfo.value.code == ErrorCode.GRAPH_003


def test_a_partial_update_is_not_rejected_for_missing_fields():
    """노드는 state 일부만 돌려준다 — 모델 전체를 검증하면 필수 필드에 걸린다.

    `query` 는 필수인데 이 노드는 그것을 내지 않는다.
    """
    update = merge_output(node(plan="output.plan"), {"plan": "a plan"}, schema=State)

    assert update == {"plan": "a plan"}


def test_an_optional_field_accepts_none():
    """`str | None` 선언은 None 을 받아야 한다 — 아니면 미완성 상태를 못 만든다."""
    update = merge_output(node(plan="output.plan"), {"plan": None}, schema=State)

    assert update == {"plan": None}


def test_a_coercible_value_is_stored_as_the_declared_type():
    """pydantic 의 강제 변환 범위는 그대로 두되, **변환 결과**를 넣는다."""
    update = merge_output(node(notified="output.notified"), {"notified": "3"}, schema=State)

    assert update == {"notified": 3}
    assert isinstance(update["notified"], int)


def test_without_a_schema_nothing_is_checked():
    """schema 미주입 배선(그래프 밖 사용)은 그대로 동작해야 한다."""
    update = merge_output(node(findings="output.findings"), {"findings": "raw"}, schema=None)

    assert update == {"findings": "raw"}


def test_an_unknown_target_is_still_refused_first():
    """선언되지 않은 키는 타입 검사 이전에 걸러진다."""
    with pytest.raises(AaestroError) as excinfo:
        merge_output(node(absent="output.absent"), {"absent": 1}, schema=State)

    assert excinfo.value.code == ErrorCode.GRAPH_003
    assert "unknown state field" in excinfo.value.message


def test_the_error_names_the_field_and_the_problem():
    """어느 필드가 왜 틀렸는지 없으면 운영자가 고칠 수 없다."""
    with pytest.raises(AaestroError) as excinfo:
        merge_output(node(findings="output.findings"), {"findings": 42}, schema=State)

    assert excinfo.value.details["field"] == "findings"
    assert excinfo.value.details["problem"]
