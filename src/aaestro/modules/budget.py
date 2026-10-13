"""Input weights and budgets for prompt rendering.

프롬프트에 들어가는 변수 값의 무게를 재고, 상한까지 자른다. 입력 탓으로는 잘리지 실패하지
않는다 — 잘린 값으로도 템플릿이 돌도록 타입과 모양을 지킨다 (04 Promptset Rules 2).
"""

from __future__ import annotations

from typing import Any

DEFAULT_VARIABLE_LIMIT = 64_000
"""변수 하나가 프롬프트에서 차지할 수 있는 크기 상한 — ``weigh`` 가 재는, 한 번 출력될 때의 길이."""

INPUT_BUDGET_SHARE = (3, 4)
"""렌더 상한 가운데 값이 쓸 수 있는 몫 (분자, 분모) — 나머지는 템플릿의 글 자리다.

변수들의 무게 합 (입력 예산) 과 값 출력의 누적 길이 (출력 예산) 가 같은 몫을 쓴다: 값을 한
번씩 출력하는 템플릿은 입력 예산 안에서 잘린 값을 생략 없이 전부 싣는다.
"""

MAX_NESTING_DEPTH = 64
"""변수 값에서 컨테이너가 겹칠 수 있는 깊이 — 그 너머는 재귀 한도에 닿기 전에 거절한다."""

ELEMENT_COST = 2
"""컨테이너 원소 하나의 비용 — repr 로 출력될 때 붙는 구분자 (``, `` · ``: ``)."""

_CUTTABLE = (str, list, tuple, dict)
"""잘라도 타입이 유지되는 값 — 숫자 · 불리언 · None 은 통째로 들어가거나 빠진다."""

_DROPPED = object()
"""남은 예산에 들어가지 않아 버린 스칼라 — 값을 다른 타입으로 바꾸지 않으려고 원소째 뺀다."""


class NestingTooDeepError(ValueError):
    """변수 값의 컨테이너가 ``MAX_NESTING_DEPTH`` 보다 깊게 겹쳐 있다."""


class UnprintableValueError(ValueError):
    """변수 값에 문자열로 바꿀 수 없는 수가 있다 (파이썬의 자릿수 한도를 넘는 int)."""


def weigh(value: Any, depth: int = 0) -> int:
    """Length a value takes in the prompt when written once.

    값을 한 번 출력했을 때 프롬프트에서 차지하는 길이 — repr 길이에 해당한다. 문자열은 컨테이너째
    출력될 때의 repr 길이로 (제어 문자 · 비표시 문자는 여러 글자로 풀린다), 컨테이너는 괄호에
    원소마다 구분자를 더해 센다. 원소를 따로 출력해 붙는 경계 표기는 여기서 세지 않는다 — 그
    부풀림은 출력 예산이 받는다 (``Boundary``).

    Raises:
        NestingTooDeepError: If containers nest deeper than ``MAX_NESTING_DEPTH``.
        UnprintableValueError: If a number cannot be converted to text.
    """
    if isinstance(value, str):
        return len(repr(value))
    if not isinstance(value, list | tuple | dict):
        return _printed_length(value)
    if depth >= MAX_NESTING_DEPTH:
        raise NestingTooDeepError(f"value nests deeper than {MAX_NESTING_DEPTH}")
    if isinstance(value, dict):
        return 2 + sum(
            2 * ELEMENT_COST + weigh(key, depth + 1) + weigh(item, depth + 1)
            for key, item in value.items()
        )
    return 2 + sum(ELEMENT_COST + weigh(item, depth + 1) for item in value)


def _printed_length(value: Any) -> int:
    """스칼라를 출력했을 때의 길이 — 숫자도 프롬프트에서 자리를 차지한다."""
    try:
        return len(str(value))
    except ValueError as err:
        raise UnprintableValueError("number has too many digits to print") from err


def clip(value: Any, limit: int) -> Any:
    """Cut a value down to ``limit`` by the same costs ``weigh`` counts.

    값을 상한까지 잘라 냅니다. 타입과 모양은 유지합니다 — 잘린 값으로도 템플릿이 돌아야 합니다:
    문자열은 앞부분을 남기고, 리스트는 예산이 다한 자리에서 끊습니다. 객체는 키를 먼저 지키고
    값을 자릅니다 (``d.title`` 이 사라지면 템플릿이 실패한다). 키조차 들어가지 않는 항목은
    빼고, 리스트 안의 객체가 키를 다 지키지 못하면 그 원소부터 끊습니다. 최상위 스칼라는 자를
    수도 뺄 수도 없어 그대로 둡니다 (길이는 파이썬의 자릿수 한도로 묶여 있다).
    """
    if not isinstance(value, _CUTTABLE):
        return value
    return _clip(value, [limit], nested=False)


def _clip(value: Any, budget: list[int], *, nested: bool) -> Any:
    """``budget[0]`` 을 깎아 가며 자른다 — 재귀 호출이 남은 예산을 공유한다."""
    if isinstance(value, str):
        # 글자마다 repr 길이가 다르다 (제어 문자는 네 글자) — 들어가는 가장 긴 앞부분을 찾는다
        low, high = 0, min(len(value), max(budget[0], 0))
        while low < high:
            middle = (low + high + 1) // 2
            if len(repr(value[:middle])) <= budget[0]:
                low = middle
            else:
                high = middle - 1
        budget[0] -= len(repr(value[:low]))
        return value[:low]
    if isinstance(value, dict):
        return _clip_entries(value, budget, nested=nested)
    if isinstance(value, list | tuple):
        budget[0] -= 2
        items: list[Any] = []
        for item in value:
            if budget[0] < ELEMENT_COST:
                break
            budget[0] -= ELEMENT_COST
            kept = _clip(item, budget, nested=True)
            # 글자 하나 남지 않게 잘린 문자열은 입력에 없던 빈 원소다 — 싣지 않고 거기서 끊는다
            if kept is _DROPPED or (kept == "" and item != ""):
                break
            items.append(kept)
        return items
    size = _printed_length(value)
    if size > budget[0]:
        return _DROPPED
    budget[0] -= size
    return value


def _clip_entries(value: dict[Any, Any], budget: list[int], *, nested: bool) -> Any:
    """객체를 자른다 — 먼저 키의 자리를 잡고, 남은 예산으로 값을 자른다."""
    budget[0] -= 2
    reserved: list[tuple[Any, Any]] = []
    for key, item in value.items():
        # 자를 수 있는 값은 빈 값의 자리 (2) 만 먼저 잡는다. 숫자는 통째로 들어가야 한다
        floor = 2 if isinstance(item, _CUTTABLE) else _printed_length(item)
        cost = 2 * ELEMENT_COST + weigh(key) + floor
        if cost <= budget[0]:
            budget[0] -= cost
            reserved.append((key, item))
        elif nested:
            return _DROPPED
    entries: dict[Any, Any] = {}
    for key, item in reserved:
        if not isinstance(item, _CUTTABLE):
            entries[key] = item
            continue
        budget[0] += 2
        kept = _clip(item, budget, nested=True)
        if kept is not _DROPPED:
            entries[key] = kept
        elif nested:
            return _DROPPED
    return entries


__all__ = [
    "DEFAULT_VARIABLE_LIMIT",
    "ELEMENT_COST",
    "INPUT_BUDGET_SHARE",
    "MAX_NESTING_DEPTH",
    "NestingTooDeepError",
    "UnprintableValueError",
    "clip",
    "weigh",
]
