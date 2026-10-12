"""Unit tests for compaction and retention.

무한 축적은 검색 품질과 비용을 함께 망가뜨린다. 요약 생성은 fake 로 대체하고,
시간 판정은 주입한다 — 실 모델 호출 금지, 실제 시간 의존 금지.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aaestro.memory.compaction import (
    Summarizer,
    build_summary,
    expired_entries,
    plan_compaction,
)
from aaestro.memory.entry import MemoryEntry, MemorySource
from aaestro.modules.memoryset import (
    CompactionSpec,
    MemoryKind,
    RetentionSpec,
)

SPACE = "local:researcher:longterm"
NOW = datetime(2026, 8, 26, tzinfo=UTC)


class FakeSummarizer:
    """압축 대상 개수를 요약으로 돌려주는 대역 — 실 모델을 부르지 않는다."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def summarize(self, entries) -> str:
        self.calls.append(len(entries))
        return f"{len(entries)}개 관찰 요약"


def entry(content: str, **overrides) -> MemoryEntry:
    base = {
        "space": SPACE,
        "kind": MemoryKind.OBSERVATION,
        "content": content,
        "source": MemorySource(agent="researcher"),
        "created_at": NOW,
    }
    base.update(overrides)
    return MemoryEntry(**base)


def spec(**overrides) -> CompactionSpec:
    return CompactionSpec(**{"trigger_entries": 3, **overrides})


def clock(moment: datetime = NOW):
    return lambda: moment


# --- 계약 ---------------------------------------------------------------------


def test_fake_summarizer_satisfies_the_contract():
    assert isinstance(FakeSummarizer(), Summarizer)


# --- trigger ------------------------------------------------------------------


def test_below_trigger_nothing_is_collapsed():
    """압축은 비용을 줄이려는 것이지 작은 space 를 건드리려는 게 아니다."""
    plan = plan_compaction(SPACE, [entry("a"), entry("b")], spec(trigger_entries=5))

    assert plan.triggered is False
    assert len(plan.keep) == 2


def test_reaching_the_trigger_collapses_raw_entries():
    entries = [entry(f"관찰 {i}") for i in range(4)]

    plan = plan_compaction(SPACE, entries, spec(trigger_entries=3))

    assert plan.triggered is True
    assert len(plan.collapse) == 4


def test_keep_kinds_survive_compaction():
    """요약과 사실은 접지 않는다 — 그것이 압축의 결과물이기 때문이다."""
    entries = [
        entry("관찰", kind=MemoryKind.OBSERVATION),
        entry("사실", kind=MemoryKind.FACT),
        entry("요약", kind=MemoryKind.SUMMARY),
    ]

    plan = plan_compaction(SPACE, entries, spec(trigger_entries=3))

    assert [e.content for e in plan.collapse] == ["관찰"]
    assert {e.content for e in plan.keep} == {"사실", "요약"}


def test_high_importance_entries_keep_their_full_text():
    """중요한 관찰까지 접으면 압축이 사실을 잃는 작업이 된다."""
    entries = [
        entry("평범한 관찰", importance=0.3),
        entry("중요한 관찰", importance=0.9),
        entry("또 다른 관찰", importance=0.2),
    ]

    plan = plan_compaction(SPACE, entries, spec(trigger_entries=3))

    assert "중요한 관찰" in {e.content for e in plan.keep}
    assert "중요한 관찰" not in {e.content for e in plan.collapse}


def test_importance_floor_is_configurable():
    entries = [entry(f"관찰 {i}", importance=0.6) for i in range(3)]

    plan = plan_compaction(SPACE, entries, spec(trigger_entries=3), importance_floor=0.5)

    assert plan.collapse == ()


# --- 요약 생성 ----------------------------------------------------------------


def test_summary_replaces_the_collapsed_originals():
    entries = [entry(f"관찰 {i}") for i in range(4)]
    plan = plan_compaction(SPACE, entries, spec(trigger_entries=3))
    summarizer = FakeSummarizer()

    summary = build_summary(plan, summarizer, agent="maintainer")

    assert summary is not None
    assert summary.kind is MemoryKind.SUMMARY
    assert summarizer.calls == [4]


def test_summary_records_the_maintenance_agent():
    """원본은 archive 후 삭제되므로 요약의 출처를 추적할 수 있어야 한다."""
    plan = plan_compaction(SPACE, [entry(f"o{i}") for i in range(3)], spec())

    summary = build_summary(plan, FakeSummarizer(), agent="maintainer")

    assert summary is not None
    assert summary.source.agent == "maintainer"


def test_summary_inherits_the_highest_importance():
    """요약은 원본 여럿을 대표하므로 원본보다 낮아서는 안 된다."""
    entries = [entry("a", importance=0.2), entry("b", importance=0.7), entry("c", importance=0.1)]
    plan = plan_compaction(SPACE, entries, spec(trigger_entries=3), importance_floor=0.9)

    summary = build_summary(plan, FakeSummarizer(), agent="maintainer")

    assert summary is not None
    assert summary.importance == 0.7


def test_nothing_to_collapse_produces_no_summary():
    plan = plan_compaction(SPACE, [entry("a")], spec(trigger_entries=5))

    assert build_summary(plan, FakeSummarizer(), agent="maintainer") is None


def test_summary_belongs_to_the_same_space():
    plan = plan_compaction(SPACE, [entry(f"o{i}") for i in range(3)], spec())

    summary = build_summary(plan, FakeSummarizer(), agent="maintainer")

    assert summary is not None
    assert summary.space == SPACE


# --- retention ----------------------------------------------------------------


def test_entries_past_the_ttl_are_selected():
    old = entry("오래된 관찰", created_at=NOW - timedelta(days=400))
    recent = entry("최근 관찰", created_at=NOW - timedelta(days=1))

    expired = expired_entries([old, recent], RetentionSpec(ttl_days=365), now=clock())

    assert [e.content for e in expired] == ["오래된 관찰"]


def test_keep_kinds_survive_the_ttl():
    """요약과 사실은 오래되었다는 이유만으로 버리지 않는다."""
    entries = [
        entry("오래된 관찰", created_at=NOW - timedelta(days=400)),
        entry("오래된 사실", kind=MemoryKind.FACT, created_at=NOW - timedelta(days=400)),
        entry("오래된 요약", kind=MemoryKind.SUMMARY, created_at=NOW - timedelta(days=400)),
    ]

    expired = expired_entries(entries, RetentionSpec(ttl_days=365), now=clock())

    assert [e.content for e in expired] == ["오래된 관찰"]


def test_custom_keep_kinds_are_honoured():
    entries = [
        entry("오래된 메시지", kind=MemoryKind.MESSAGE, created_at=NOW - timedelta(days=400))
    ]
    retention = RetentionSpec(
        ttl_days=365,
        compaction=CompactionSpec(trigger_entries=10, keep_kinds=(MemoryKind.MESSAGE,)),
    )

    assert expired_entries(entries, retention, now=clock()) == ()


def test_no_ttl_expires_nothing():
    """TTL 미선언 space 는 시간만으로 삭제하지 않는다."""
    old = entry("아주 오래됨", created_at=NOW - timedelta(days=10_000))

    assert expired_entries([old], RetentionSpec(), now=clock()) == ()


def test_boundary_entry_is_not_expired():
    """경계에 정확히 걸친 항목을 버리면 하루 차이로 기억이 사라진다."""
    boundary = entry("경계", created_at=NOW - timedelta(days=365))

    expired = expired_entries([boundary], RetentionSpec(ttl_days=365), now=clock())

    assert expired == ()
