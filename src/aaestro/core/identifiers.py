"""Caller-chosen identifiers — one rule for every boundary that accepts them.

호출자가 고른 식별자 (A2A 본문의 task · run · node id, 메모리 출처의 문맥 · 태그, steer 의 발행자)
를 **성질로** 검증하는 하나의 규칙 (03 A2A Client Rules 8). 경계마다 따로 두면 한쪽만 고쳐져 같은
값이 창구에 따라 다른 답을 받는다. 값 목록이 아니라 성질로 막는다 — 사유에는 값을 싣지 않는다.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

MAX_IDENTIFIER_CHARS: Final = 128
"""식별자 길이의 기본 상한 — 프레임워크가 만드는 값은 UUID(36자)나 ``run-<uuid>`` 수준이다.

식별자는 로그 · 메트릭 · 메모리 출처 · 멱등 캐시 키로 그대로 흘러가므로 상한이 없으면 호출자가 그
크기를 정한다."""


class IdentifierProblem(StrEnum):
    """Why a value is not an acceptable identifier — the closed set of reasons.

    식별자로 받을 수 없는 사유. 값이 곧 거절 사유의 이름이다 (값 자체는 어디에도 싣지 않는다).
    """

    NOT_STRING = "not_string"
    EMPTY = "empty"
    TOO_LONG = "too_long"
    UNPRINTABLE = "unprintable_characters"
    SURROUNDING_WHITESPACE = "surrounding_whitespace"


def identifier_problem(
    value: object, *, max_chars: int = MAX_IDENTIFIER_CHARS, trimmed: bool = True
) -> IdentifierProblem | None:
    """Return why ``value`` is not an identifier, or None when it is one.

    식별자가 아닌 이유를 돌려준다 — 식별자면 None.

    순서는 고정이다: 문자열 → 비어 있음 → 길이 → 출력 가능 → 앞뒤 공백.
    제어 문자는 로그 줄을 위조하고, 앞뒤 공백은 같아 보이는 다른 키를 만든다. 보이지 않지만 출력
    가능한 문자 (U+FE0F 등) 는 둔다 — 줄을 나누지 못한다.

    Args:
        value: The caller's value — anything; 문자열이 아니면 ``NOT_STRING``.
        max_chars: Length cap in characters.
        trimmed: 앞뒤 공백을 거절할지. 키로 쓰이지 않는 표시용 이름 (steer 발행자) 만 False.

    Returns:
        The first problem found, or None.
    """
    if not isinstance(value, str):
        # 숫자 · 객체를 str() 로 접으면 서로 다른 입력이 같은 id 가 된다
        return IdentifierProblem.NOT_STRING
    # 앞뒤 공백을 받는 쪽에서는 공백만인 값도 비어 있는 것이다 — 아니면 보이지 않는 이름이 통과한다.
    # 거절하는 쪽에서는 공백만인 값이 앞뒤 공백 사유로 걸린다
    if not value or (not trimmed and not value.strip()):
        return IdentifierProblem.EMPTY
    if len(value) > max_chars:
        return IdentifierProblem.TOO_LONG
    if not value.isprintable():
        return IdentifierProblem.UNPRINTABLE
    if trimmed and value != value.strip():
        return IdentifierProblem.SURROUNDING_WHITESPACE
    return None


__all__ = ["MAX_IDENTIFIER_CHARS", "IdentifierProblem", "identifier_problem"]
