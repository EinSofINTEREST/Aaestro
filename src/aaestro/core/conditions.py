"""Declarative edge conditions.

그래프 YAML 안에서 쓰는 선언식 조건. 새 그래프마다 ``src/aaestro/graphs/conditions.py`` 에 코드를
더해야 했던 것을 없앤다 — 04 의 "새 목표 = 모듈 배선" 은 조건이 코드인 한 성립하지 않는다
(#316).

문법은 작게 둔다 — 조건은 state 를 읽고 분기만 판정하는 **순수 판정**이다 (04):

- ``state.<field>`` (``.<key>`` 로 중첩 조회) — 없으면 ``null``
- 리터럴: 숫자, 문자열, ``true`` / ``false`` / ``null``, 리터럴 목록 ``[...]``
- 비교: ``==`` ``!=`` ``<`` ``<=`` ``>`` ``>=`` ``in`` ``not in``
- 논리: ``and`` ``or`` ``not``, 괄호
- 값 하나만 쓰면 참/거짓으로 읽는다 (빈 목록·``0``·``null`` 은 거짓)

파이썬 ``eval`` 을 쓰지 않는다 — ``ast`` 로 읽고 허용한 노드만 직접 평가한다. 함수 호출, 속성
접근(``state`` 밖), 첨자, 이름은 전부 거부한다.
"""

from __future__ import annotations

import ast
import operator
import re
from collections.abc import Callable
from typing import Any

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

Predicate = Callable[[dict[str, Any]], bool]

STATE = "state"
IMPORT_REF = re.compile(r"^[\w.]+:[\w.]+$")
"""``module:attribute`` — 조건 함수 import ref (deprecated). 선언식은 콜론을 쓰지 않는다."""

_LITERAL_NAMES: dict[str, Any] = {"true": True, "false": False, "null": None}
_COMPARE: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda left, right: left in right,
    ast.NotIn: lambda left, right: left not in right,
}


def is_import_ref(condition: str) -> bool:
    """조건이 함수 import ref 인지 — 아니면 선언식이다."""
    return IMPORT_REF.match(condition) is not None


def parse_condition(expression: str) -> Predicate:
    """Compile a declarative condition into a predicate over the graph state.

    선언식 조건을 state 판정 함수로 만듭니다. 문법 밖의 식은 **읽는 시점에** 거부합니다 —
    run 도중에야 드러나면 그래프가 반쯤 돈 뒤 멈춥니다.

    Args:
        expression: The condition, e.g. ``state.needs_research and not state.plan``.

    Returns:
        A predicate returning the condition's truth for a state.

    Raises:
        AaestroError: GRAPH/``GRAPH_001`` if the expression is outside the grammar.
    """
    tree = _syntax_tree(expression)
    if tree is None:
        # except 블록 밖에서 던진다 — SyntaxError 는 식 전체를 ``text`` 로 쥐고 있어, 체인이나
        # 문맥으로 이어 두면 traceback 과 예외 수집기로 식이 나간다
        raise _invalid("not a valid expression")
    _check(tree.body)

    def predicate(state: dict[str, Any]) -> bool:
        try:
            return bool(_evaluate(tree.body, state))
        except TypeError as err:
            # 비교할 수 없는 값(null < 3 등) — 조용히 거짓으로 두면 다른 가지로 새어 나간다
            # 식은 싣지 않는다 — 그래프 선언에 있고, 여기 실으면 선언의 값이 에러로 나간다 (#386).
            # reason 은 파이썬이 쓴 문구다 (연산자와 타입 이름) — state 의 값은 들어가지 않는다
            raise AaestroError(
                category=ErrorCategory.GRAPH,
                code=ErrorCode.GRAPH_003,
                message="condition could not be evaluated",
                details={"reason": str(err)},
            ) from err

    return predicate


def _syntax_tree(expression: str) -> ast.Expression | None:
    """식의 구문 트리 — 문법 오류면 ``None``."""
    try:
        return ast.parse(expression.strip(), mode="eval")
    except SyntaxError:
        return None


def _invalid(reason: str) -> AaestroError:
    """문법 밖의 식 — 사유는 **고정 문구** 다. 식이나 그 안의 이름은 싣지 않는다 (#386).

    식은 선언의 값이다. 파이썬은 식별자를 NFKC 로 정규화하므로 (전각 ``ｓｋ`` 가 ``sk`` 로) 이름을
    메시지에 실으면 입력과 글자가 달라 가림도 비껴간다. 어느 edge 인지는 필드 경로가 말한다.
    """
    return AaestroError(
        category=ErrorCategory.GRAPH,
        code=ErrorCode.GRAPH_001,
        message=f"invalid condition: {reason}",
    )


def _check(node: ast.expr) -> None:
    """허용한 노드만 남았는지 — 평가 전에 한 번 전부 본다."""
    if isinstance(node, ast.BoolOp):
        for value in node.values:
            _check(value)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        _check(node.operand)
    elif isinstance(node, ast.Compare):
        if not all(type(op) in _COMPARE for op in node.ops):
            raise _invalid("unsupported comparison")
        for operand in (node.left, *node.comparators):
            _check(operand)
    elif isinstance(node, ast.Attribute):
        _state_path(node)
    elif isinstance(node, ast.Name):
        if node.id not in _LITERAL_NAMES:
            raise _invalid("unknown name — read fields as state.<field>")
    elif isinstance(node, ast.List | ast.Tuple):
        for element in node.elts:
            _check(element)
    elif not (isinstance(node, ast.Constant) and _is_literal(node.value)):
        # 노드 종류 이름 (``BinOp``, ``Call``) 은 파이썬 문법의 이름이지 입력이 아니다
        raise _invalid(f"'{type(node).__name__}' is not allowed")


def _is_literal(value: Any) -> bool:
    return value is None or isinstance(value, bool | int | float | str)


def _state_path(node: ast.Attribute) -> tuple[str, ...]:
    """``state.a.b`` → ``("a", "b")`` — ``state`` 로 시작하지 않는 속성 접근은 거부한다."""
    path: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        path.append(current.attr)
        current = current.value
    if not (isinstance(current, ast.Name) and current.id == STATE):
        raise _invalid("fields are read as state.<field>")
    return tuple(reversed(path))


def _evaluate(node: ast.expr, state: dict[str, Any]) -> Any:
    if isinstance(node, ast.BoolOp):
        results = (bool(_evaluate(value, state)) for value in node.values)
        return all(results) if isinstance(node.op, ast.And) else any(results)
    if isinstance(node, ast.UnaryOp):
        return not _evaluate(node.operand, state)
    if isinstance(node, ast.Compare):
        return _compare(node, state)
    if isinstance(node, ast.Attribute):
        return _lookup(state, _state_path(node))
    if isinstance(node, ast.Name):
        return _LITERAL_NAMES[node.id]
    if isinstance(node, ast.List | ast.Tuple):
        return [_evaluate(element, state) for element in node.elts]
    assert isinstance(node, ast.Constant)  # noqa: S101 — _check 가 보장
    return node.value


def _compare(node: ast.Compare, state: dict[str, Any]) -> bool:
    left = _evaluate(node.left, state)
    for op, comparator in zip(node.ops, node.comparators, strict=True):
        right = _evaluate(comparator, state)
        if not _COMPARE[type(op)](left, right):
            return False
        left = right
    return True


def _lookup(state: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = state
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


__all__ = ["IMPORT_REF", "Predicate", "is_import_ref", "parse_condition"]
