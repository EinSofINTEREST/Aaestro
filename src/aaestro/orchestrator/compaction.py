"""Graph state compaction policies and their pure strategies.

그래프 state 의 필드별 압축 정책 선언과, 그 정책을 리스트에 적용하는 순수 함수.
여기에는 I/O 가 없다 — 요약 호출 · 원문 보존 · 경계 적용은 ``compactor.py`` 가 한다.

상주 그래프의 리스트 필드는 iteration 마다 자라고, 그 state 가 매 노드의 프롬프트와
checkpoint 로 들어간다. 정책은 그래프 선언에 두고, 적용은 경계 한 곳에서만 한다.
"""

from __future__ import annotations

import json
import math
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

SUMMARIZE_INPUT_KEY: Final = "items"
"""요약 템플릿이 접을 항목을 받는 변수 (array) — state 압축 전용 계약이다.

memory space 압축용 ``compactor`` 템플릿을 빌려 쓰지 않는다: 그 문구는 space 별 요약을
요구해 모델이 요약문이 아닌 구조를 낼 수 있다.
"""

SUMMARIZE_OUTPUT_KEY: Final = "summary"
"""요약 템플릿이 요약문(비어 있지 않은 문자열)을 내는 출력 키."""

SUMMARY_ITEM_MAX_CHARS: Final = 4000
"""요약 프롬프트에 싣는 항목 하나의 길이 상한 — 항목 하나가 프롬프트를 지배하지 못하게 한다.

잘리는 것은 프롬프트에 싣는 사본뿐이다. 원문은 artifact 에 온전히 남는다.
"""

TRUNCATION_MARK: Final = " …[truncated]"

SUMMARY_PROMPT_MAX_CHARS: Final = 60_000
"""한 번의 요약 호출에 싣는 항목 사본의 총 길이 상한 — 밀린 양이 많아도 호출 하나가 모델
컨텍스트를 넘지 않게 한다. 넘는 만큼은 다음 경계들에서 오래된 것부터 이어서 접는다.

항목당 상한의 여러 배여야 한다: 한 번에 원문 두 건도 못 싣는 상한이면 접을수록 늘어난다.
"""

SUMMARY_MAX_CHARS: Final = 8_000
"""요약문 길이 상한 — 넘는 출력은 쓸 수 없는 출력이다. 잘라 넣지 않는다: 요약이 접은 것보다
길면 압축이 아니고, 잘린 요약은 무엇이 빠졌는지 알 수 없다."""

SUMMARY_KEY: Final = "summary"
ARTIFACT_REF_KEY: Final = "artifact_ref"
REPLACED_KEY: Final = "replaced"

MIN_ORIGINALS_TO_FOLD: Final = 2
"""요약으로 접을 원문의 최소 건수 — 한 건을 요약 한 건으로 바꾸면 모델 호출만 쓰고 줄지 않는다."""


class _Policy(BaseModel):
    """정책 공통 설정 — 모르는 키와 전략에 맞지 않는 키를 거절한다."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class KeepLastPolicy(_Policy):
    """Keep only the most recent ``n`` items.

    가장 최근 ``n`` 건만 남긴다 — 순서는 유지한다.
    """

    strategy: Literal["keep_last"]
    n: int = Field(gt=0, strict=True)


class KeepFirstPolicy(_Policy):
    """Keep only the first ``n`` items.

    처음 ``n`` 건만 남긴다 — 뒤에 붙는 항목은 버린다.
    """

    strategy: Literal["keep_first"]
    n: int = Field(gt=0, strict=True)


class DedupePolicy(_Policy):
    """Drop repeated items, keeping the first occurrence.

    중복을 지우고 첫 등장 순서를 유지한다.
    """

    strategy: Literal["dedupe"]


class SummarizePolicy(_Policy):
    """Fold older items into one summary once the field grows past a token budget.

    추정 토큰이 ``over_tokens`` 를 넘으면 오래된 항목을 요약 한 건으로 접고, 최근
    ``keep_last`` 건은 원문으로 둔다. 요약은 ``compactor`` 에이전트가 ``template``
    템플릿으로 쓴다.
    """

    strategy: Literal["summarize"]
    over_tokens: int = Field(gt=0, strict=True)
    keep_last: int = Field(gt=0, strict=True)
    compactor: str = Field(pattern=r"^agents/[^/@\s]+@[^/@\s]+$")
    template: str = Field(min_length=1)


CompactionPolicy = Annotated[
    KeepLastPolicy | KeepFirstPolicy | DedupePolicy | SummarizePolicy,
    Field(discriminator="strategy"),
]


def estimate_tokens(items: list[Any]) -> int:
    """Estimate how many tokens a list field costs in a prompt.

    리스트 필드가 프롬프트에서 차지할 토큰 추정치 — 회상 예산과 같은 상수로 센다.
    올림한다: 추정은 넘치는 쪽이 아니라 남는 쪽으로 틀려야 한다.
    """
    # 추정할 때만 가져온다 — 이 모듈은 토폴로지가 import 하므로, 모듈 수준에서 가져오면 선언을
    # 읽는 것만으로 memory 계층이 딸려 온다
    from aaestro.memory.recall import CHARS_PER_TOKEN

    return sum(math.ceil(len(_as_text(item)) / CHARS_PER_TOKEN) for item in items)


def _as_text(item: Any) -> str:
    """항목의 결정적 텍스트 표현 — 키 순서에 따라 추정치와 중복 판정이 흔들리면 안 된다."""
    if isinstance(item, str):
        return item
    return json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)


def _identity(item: Any) -> str:
    """중복 판정 키 — dict · list 는 해시할 수 없으므로 타입이 드러나는 정규 표현으로 본다.

    문자열 ``"1"`` 과 숫자 ``1`` 이 같은 항목으로 접히지 않도록 JSON 표현을 쓴다.
    """
    return json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)


def dedupe(items: list[Any]) -> list[Any]:
    """Drop repeats while keeping first-seen order.

    첫 등장 순서를 유지하며 중복을 지운다 — 해시할 수 없는 항목도 결정적으로 다룬다.
    """
    seen: set[str] = set()
    kept: list[Any] = []
    for item in items:
        key = _identity(item)
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
    return kept


def apply_deterministic(
    policy: KeepLastPolicy | KeepFirstPolicy | DedupePolicy, items: list[Any]
) -> list[Any]:
    """Apply a strategy that needs no model call.

    모델 호출이 필요 없는 전략을 적용합니다. 이미 정책을 만족하는 리스트는 같은 내용으로
    돌아옵니다 — 같은 경계를 다시 지나도 결과가 달라지지 않습니다.
    """
    if isinstance(policy, KeepLastPolicy):
        return items[-policy.n :]
    if isinstance(policy, KeepFirstPolicy):
        return items[: policy.n]
    return dedupe(items)


def has_summary_shape(item: Any) -> bool:
    """요약 항목의 **모양** 인지 — 모양만으로는 압축이 만든 것인지 알 수 없다.

    에이전트도 같은 모양의 항목을 낼 수 있다. 앞선 요약으로 대접할지는 호출 측이 위치와
    참조로 정한다 (``StateCompactor``).
    """
    return isinstance(item, dict) and set(item) == {SUMMARY_KEY, ARTIFACT_REF_KEY, REPLACED_KEY}


def split_for_summary(
    policy: SummarizePolicy, items: list[Any], *, prior_summary: bool = False
) -> tuple[list[Any], list[Any]]:
    """Split a field into the items to fold now and the items to keep.

    지금 요약으로 접을 항목과 남길 항목을 나눕니다. 접을 것이 없으면 첫 값이 빈
    리스트입니다 — 예산 아래이거나, 아직 접을 만큼 쌓이지 않은 경우입니다.

    Args:
        policy: The field's summarize policy.
        items: The field as it stands.
        prior_summary: Whether ``items[0]`` is the summary an earlier compaction of this
            field left — 그 한 건만 원문으로 세지 않습니다. 그 밖의 항목은 모양이 요약
            같아도 전부 원문입니다.

    **진동을 막는 규칙**: 예산을 넘어도, 접을 원문이 ``max(2, keep_last)`` 건 미만이면
    건너뜁니다 — 요약은 많아야 ``keep_last`` 건마다 한 번입니다.

    **한 번에 접는 양의 상한**: 오래된 것부터 ``SUMMARY_PROMPT_MAX_CHARS`` 만큼만 접습니다.
    남은 오래된 항목은 원문 그대로 남아 다음 경계에서 이어서 접힙니다.
    """
    if estimate_tokens(items) <= policy.over_tokens:
        return [], items
    older = items[: -policy.keep_last]
    originals = len(older) - (1 if prior_summary and older else 0)
    if originals < max(MIN_ORIGINALS_TO_FOLD, policy.keep_last):
        return [], items
    end = _chunk_end(older)
    return items[:end], items[end:]


def _chunk_end(older: list[Any]) -> int:
    """프롬프트 총 상한 안에 드는, 오래된 쪽부터의 항목 수."""
    total = 0
    for index, item in enumerate(older):
        total += len(_capped(_as_text(item)))
        if total > SUMMARY_PROMPT_MAX_CHARS:
            return index
    return len(older)


def summary_items(older: list[Any]) -> list[str]:
    """Prepare the folded items for the summary prompt.

    접을 항목을 요약 프롬프트에 실을 모양으로 만듭니다. state 항목은 에이전트 산출물이라
    신뢰할 수 없습니다 — 구조를 문자열로 고정하고 (객체·리스트가 프롬프트 구조로 읽히지
    않게) 항목마다 길이를 자릅니다.
    """
    return [_capped(_as_text(item)) for item in older]


def _capped(text: str) -> str:
    if len(text) <= SUMMARY_ITEM_MAX_CHARS:
        return text
    return text[: SUMMARY_ITEM_MAX_CHARS - len(TRUNCATION_MARK)] + TRUNCATION_MARK


def summary_entry(summary: str, *, artifact_ref: str, replaced: int) -> dict[str, Any]:
    """The state item that stands in for the folded originals.

    접힌 원문 자리에 들어가는 항목 — 원문은 ``artifact_ref`` 로 되찾는다.
    """
    return {SUMMARY_KEY: summary, ARTIFACT_REF_KEY: artifact_ref, REPLACED_KEY: replaced}


__all__ = [
    "ARTIFACT_REF_KEY",
    "REPLACED_KEY",
    "SUMMARIZE_INPUT_KEY",
    "SUMMARIZE_OUTPUT_KEY",
    "SUMMARY_ITEM_MAX_CHARS",
    "SUMMARY_MAX_CHARS",
    "SUMMARY_PROMPT_MAX_CHARS",
    "SUMMARY_KEY",
    "CompactionPolicy",
    "DedupePolicy",
    "KeepFirstPolicy",
    "KeepLastPolicy",
    "SummarizePolicy",
    "apply_deterministic",
    "dedupe",
    "estimate_tokens",
    "has_summary_shape",
    "split_for_summary",
    "summary_items",
    "summary_entry",
]
