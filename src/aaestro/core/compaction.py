"""Graph state compaction policies — the declaration schema.

그래프 state 의 필드별 압축 정책 선언 (pydantic). 토폴로지 (``core.topology``) 가 참조하므로
core 에 둔다 — 적용 (순수 전략 · 요약 호출) 은 ``orchestrator/compaction.py`` ·
``orchestrator/compactor.py`` 다. 여기에는 I/O 도 orchestrator 의존도 없다 (01 Directory Purposes).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


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


__all__ = [
    "CompactionPolicy",
    "DedupePolicy",
    "KeepFirstPolicy",
    "KeepLastPolicy",
    "SummarizePolicy",
]
