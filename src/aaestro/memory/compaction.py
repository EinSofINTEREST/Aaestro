"""Compaction and retention.

무한 축적은 검색 품질과 비용을 함께 망가뜨린다 — space 는 선언된 정책으로
다이어트한다 (09 Compaction & Retention).

압축 자체는 **시스템 유지보수 service 그래프**가 수행한다. 이 모듈은 그 그래프가
쓰는 판정과 결과 반영을 담당하며, 요약 생성은 모델에 위임한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

from aaestro.memory.entry import MemoryEntry, MemorySource
from aaestro.modules.memoryset import MemoryKind

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from aaestro.modules.memoryset import CompactionSpec, RetentionSpec

log = structlog.get_logger(__name__)


@runtime_checkable
class Summarizer(Protocol):
    """Collapses raw entries into one summary.

    원본 항목들을 요약 하나로 압축하는 계약. 실제 요약은 유지보수 그래프의
    에이전트가 수행하고, 테스트는 결정적 대역으로 대체한다.
    """

    def summarize(self, entries: Sequence[MemoryEntry]) -> str:
        """요약 본문을 만든다."""
        ...


@dataclass(frozen=True)
class CompactionPlan:
    """What one compaction pass would do.

    한 번의 압축이 무엇을 할지. 실행 전에 판정을 드러내 유지보수 그래프가
    무엇을 압축하는지 로그로 남길 수 있게 한다.
    """

    space: str
    collapse: tuple[MemoryEntry, ...]
    """요약으로 접힐 원본."""

    keep: tuple[MemoryEntry, ...]
    """정책상 원문을 유지하는 항목."""

    @property
    def triggered(self) -> bool:
        """압축할 것이 있는지."""
        return bool(self.collapse)


def plan_compaction(
    space: str,
    entries: Sequence[MemoryEntry],
    spec: CompactionSpec,
    *,
    importance_floor: float = 0.8,
) -> CompactionPlan:
    """Decide what to collapse for one space.

    한 space 에서 무엇을 접을지 판정합니다.

    ``keep_kinds`` 와 높은 ``importance`` 는 원문을 유지합니다 — 압축은 비용을
    줄이려는 것이지 중요한 사실을 잃으려는 것이 아닙니다.

    Args:
        space: The space being compacted.
        entries: Every entry currently in the space.
        spec: The compaction policy.
        importance_floor: Entries at or above this keep their full text.

    Returns:
        The plan; ``triggered`` is False when the space is below its trigger.
    """
    if len(entries) < spec.trigger_entries:
        return CompactionPlan(space=space, collapse=(), keep=tuple(entries))

    keep: list[MemoryEntry] = []
    collapse: list[MemoryEntry] = []
    for entry in entries:
        if entry.kind in spec.keep_kinds or entry.importance >= importance_floor:
            keep.append(entry)
        else:
            collapse.append(entry)

    return CompactionPlan(space=space, collapse=tuple(collapse), keep=tuple(keep))


def build_summary(
    plan: CompactionPlan, summarizer: Summarizer, *, agent: str
) -> MemoryEntry | None:
    """Create the summary entry replacing collapsed originals.

    접힌 원본을 대체할 요약 항목을 만듭니다.

    요약의 ``source`` 는 압축을 수행한 에이전트를 가리킵니다 — 원본은 archive
    후 TTL 로 삭제되므로, 요약이 어디서 왔는지 추적할 수 있어야 합니다.

    Args:
        plan: The compaction plan.
        summarizer: Produces the summary text.
        agent: The maintenance agent performing compaction.

    Returns:
        The summary entry, or None when nothing was collapsed.
    """
    if not plan.triggered:
        return None

    return MemoryEntry(
        space=plan.space,
        kind=MemoryKind.SUMMARY,
        content=summarizer.summarize(plan.collapse),
        tags=("compaction",),
        source=MemorySource(agent=agent),
        # 요약은 원본 여럿을 대표하므로 원본보다 중요도가 낮아서는 안 된다
        importance=max((e.importance for e in plan.collapse), default=0.5),
    )


def expired_entries(
    entries: Sequence[MemoryEntry],
    retention: RetentionSpec,
    *,
    now: Callable[[], datetime] | None = None,
) -> tuple[MemoryEntry, ...]:
    """Select entries past their TTL.

    TTL 을 넘긴 항목을 고릅니다.

    ``keep_kinds`` 는 예외입니다 — 요약과 사실은 오래되었다는 이유만으로
    버리지 않습니다. 시간 판정은 주입 가능한 clock 을 씁니다.

    Args:
        entries: Candidate entries.
        retention: The retention policy.
        now: Clock; defaults to the current UTC time.

    Returns:
        The entries to purge; empty when no TTL is declared.
    """
    if retention.ttl_days is None:
        return ()

    clock = now or (lambda: datetime.now(UTC))
    cutoff = clock() - timedelta(days=retention.ttl_days)
    protected = (
        set(retention.compaction.keep_kinds)
        if retention.compaction is not None
        else {MemoryKind.FACT, MemoryKind.SUMMARY}
    )

    return tuple(
        entry for entry in entries if entry.created_at < cutoff and entry.kind not in protected
    )


__all__ = [
    "CompactionPlan",
    "Summarizer",
    "build_summary",
    "expired_entries",
    "plan_compaction",
]
