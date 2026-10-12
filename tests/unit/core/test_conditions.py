"""Declarative edge conditions (#316).

조건이 코드(``aaestro.graphs.conditions``)에만 있으면 새 그래프마다 ``src/`` 를 고쳐야 한다.
선언식은 YAML 안에서 끝나되 순수 판정이어야 한다 — 함수 호출·임의 이름·속성 접근은 읽는
시점에 거부한다.
"""

from __future__ import annotations

import pytest

from aaestro.core.conditions import is_import_ref, parse_condition
from aaestro.core.errors import AaestroError

STATE = {
    "needs_research": True,
    "approved": False,
    "plan": None,
    "findings": ["a", "b"],
    "count": 3,
    "mode": "deep",
    "meta": {"stage": "draft"},
}


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("state.needs_research", True),
        ("not state.needs_research", False),
        ("state.approved", False),
        ("state.findings", True),
        ("state.plan", False),
        ("state.missing", False),
        ("state.plan == null", True),
        ("state.count >= 3", True),
        ("state.count > 3", False),
        ("1 < state.count < 5", True),
        ('state.mode == "deep"', True),
        ('state.mode in ["quick", "deep"]', True),
        ('state.mode not in ["quick"]', True),
        ('"a" in state.findings', True),
        ('state.meta.stage == "draft"', True),
        ("state.meta.missing.deeper == null", True),
        ("state.needs_research and not state.approved", True),
        ("state.approved or state.count == 3", True),
        ("(state.approved or state.plan) and state.needs_research", False),
        ("state.approved == false", True),
    ],
)
def test_a_condition_reads_the_state(expression, expected):
    assert parse_condition(expression)(STATE) is expected


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('true')",  # 함수 호출
        "len(state.findings) > 0",  # 함수 호출 — 판정은 선언으로만
        "state.findings[0]",  # 첨자
        "approved",  # state 밖의 이름
        "os.environ",  # state 밖의 속성
        "state.count + 1 > 3",  # 산술
        "state.count is None",  # is 비교
        "lambda: True",
        "state.",  # 문법 오류
        "[x for x in state.findings]",
    ],
)
def test_anything_outside_the_grammar_is_refused_when_read(expression):
    """run 도중에야 드러나면 그래프가 반쯤 돈 뒤 멈춘다 — 읽는 시점에 GRAPH_001."""
    with pytest.raises(AaestroError) as exc_info:
        parse_condition(expression)

    assert exc_info.value.code == "GRAPH_001"


def test_an_uncomparable_value_is_an_error_not_a_silent_false():
    """조용히 거짓이면 다른 가지로 새어 나간다."""
    predicate = parse_condition("state.plan > 3")

    with pytest.raises(AaestroError) as exc_info:
        predicate(STATE)

    assert exc_info.value.code == "GRAPH_003"


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ("aaestro.graphs.conditions:needs_research", True),
        ("state.needs_research", False),
        ("state.meta.stage == 'x:y'", False),
    ],
)
def test_import_refs_and_expressions_are_told_apart(condition, expected):
    assert is_import_ref(condition) is expected
