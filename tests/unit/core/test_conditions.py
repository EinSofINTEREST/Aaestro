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


# --- 에러는 식을 싣지 않는다 (#386) -------------------------------------------------------------

SECRET_NAME = "sk_SECRET_123_marker"


@pytest.mark.parametrize(
    "expression",
    [
        SECRET_NAME,  # state 밖의 이름
        "ｓｋ＿ＳＥＣＲＥＴ＿１２３＿ｍａｒｋｅｒ",  # 전각 — NFKC 로 ASCII 이름이 된다
        f"{SECRET_NAME}.field == 1",
        f"state.x == '{SECRET_NAME}' +",  # 문법 오류
        f"len('{SECRET_NAME}')",
    ],
    ids=["name", "fullwidth-name", "attribute", "syntax-error", "call"],
)
def test_a_refused_condition_does_not_come_back_in_the_error(expression):
    """식은 선언의 값이다 — 메시지에도 details 에도 원인 체인에도 싣지 않는다."""
    with pytest.raises(AaestroError) as exc_info:
        parse_condition(expression)

    err = exc_info.value
    assert err.code == "GRAPH_001"
    assert "secret" not in (err.message + str(err.details)).lower()
    # SyntaxError 는 식 전체를 text 로 쥐고 있다 — 체인 · 문맥으로 잇지 않는다
    assert err.__cause__ is None
    assert err.__context__ is None


def test_an_evaluation_failure_does_not_carry_the_expression_or_the_state():
    predicate = parse_condition(f"state.plan > 3 or state.note == '{SECRET_NAME}'")

    with pytest.raises(AaestroError) as exc_info:
        predicate({"plan": SECRET_NAME, "note": ""})

    err = exc_info.value
    assert err.code == "GRAPH_003"
    assert err.message == "condition could not be evaluated"
    assert "secret" not in str(err.details).lower()
    assert "not supported between" in err.details["reason"]
