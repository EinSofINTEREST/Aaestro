"""메모리 메트릭 `space` 라벨 — run space 는 run 을 뺀 ``run:<alias>`` 로 접힌다 (#689).

run 마다 새 시계열이 생기면 카디널리티가 끝없이 자란다. 접힌 라벨의 게이지가 서로 덮어쓰지
않는지, 검색 · 회상 경로가 같은 라벨을 쓰는지 본다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import CollectorRegistry

from aaestro.access.baselines import RUN_SPACE_ID
from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.memory.entry import MemoryEntry, MemorySource
from aaestro.memory.index import IndexRegistry, SpaceIndex
from aaestro.memory.recall import AutoRecall, Recall
from aaestro.memory.store import SqliteMemoryStore
from aaestro.memory.telemetry import space_label
from aaestro.modules.memoryset import ChunkSpec, MemoryKind, RecallSpec
from aaestro.observability.metrics import Metrics

RUN_SPACES = ("run:r1:scratch", "run:r2:scratch", "run:r3:notes")
LOCAL = "local:researcher:longterm"


def entry(space: str, content: str = "sidecar notes", **overrides) -> MemoryEntry:
    base = {
        "space": space,
        "kind": MemoryKind.FACT,
        "content": content,
        "source": MemorySource(agent="researcher"),
    }
    base.update(overrides)
    return MemoryEntry(**base)


def samples(metrics: Metrics, family: str) -> dict[tuple, float]:
    """family 의 (이름, 라벨 값들) → 값 — _count · _total 등 접미사를 합친 표본."""
    found: dict[tuple, float] = {}
    for metric in metrics.registry.collect():
        if metric.name != family:
            continue
        for sample in metric.samples:
            key = (sample.name, tuple(sorted(sample.labels.items())))
            found[key] = sample.value
    return found


def space_labels(metrics: Metrics, family: str) -> set[str]:
    return {dict(labels)["space"] for _name, labels in samples(metrics, family)}


def test_gauges_sum_entries_per_label():
    metrics = Metrics(registry=CollectorRegistry())
    registry = IndexRegistry(metrics=metrics)
    spec = ChunkSpec()
    for space, content in [
        ("run:r1:scratch", "a"),
        ("run:r1:scratch", "b"),
        ("run:r2:scratch", "c"),
        ("run:r3:notes", "d"),
        (LOCAL, "e"),
    ]:
        registry.submit(entry(space, content), spec)

    registry.drain()  # 색인하고 게이지를 반영한다

    entries = samples(metrics, "aaestro_memory_entries")
    # 같은 별칭의 run 둘은 합산 — 덮어쓰면 마지막 run 의 항목 수만 남는다
    assert entries[("aaestro_memory_entries", (("space", "run:scratch"),))] == 3
    assert entries[("aaestro_memory_entries", (("space", "run:notes"),))] == 1
    assert entries[("aaestro_memory_entries", (("space", LOCAL),))] == 1
    assert space_labels(metrics, "aaestro_memory_entries") == {"run:scratch", "run:notes", LOCAL}


def test_index_lag_takes_the_oldest_across_folded_spaces():
    metrics = Metrics(registry=CollectorRegistry())
    registry = IndexRegistry(metrics=metrics)
    spec = ChunkSpec()
    now = datetime.now(UTC)
    registry.submit(entry("run:r1:scratch", created_at=now - timedelta(seconds=90)), spec)
    registry.submit(entry("run:r2:scratch", created_at=now - timedelta(seconds=10)), spec)
    registry.submit(entry("run:r3:notes", created_at=now - timedelta(seconds=5)), spec)

    registry.indexes = {s: SpaceIndex(space=s) for s in RUN_SPACES}
    registry._observe(now=now)

    lag = samples(metrics, "aaestro_memory_index_lag_seconds")
    assert lag[("aaestro_memory_index_lag_seconds", (("space", "run:scratch"),))] == 90
    assert lag[("aaestro_memory_index_lag_seconds", (("space", "run:notes"),))] == 5
    assert space_labels(metrics, "aaestro_memory_index_lag_seconds") == {
        "run:scratch",
        "run:notes",
    }


def recall_over(metrics: Metrics, spaces: tuple[str, ...]) -> Recall:
    store = SqliteMemoryStore()
    indexes = {}
    for space in spaces:
        item = entry(space)
        store.append(item)
        indexes[space] = SpaceIndex(space=space)
        indexes[space].add(item, ChunkSpec())
    return Recall(indexes=indexes, store=store, metrics=metrics)


def test_search_and_recall_fold_run_spaces_into_one_label_per_alias():
    metrics = Metrics(registry=CollectorRegistry())
    recall = recall_over(metrics, RUN_SPACES)
    auto = AutoRecall(recall=recall, policy=RecallSpec())

    auto.for_task("sidecar notes", spaces=list(RUN_SPACES))

    ops = samples(metrics, "aaestro_memory_operations")
    assert (
        ops[
            (
                "aaestro_memory_operations_total",
                (("op", "search"), ("space", "run:scratch"), ("status", "ok")),
            )
        ]
        == 2
    )
    assert (
        ops[
            (
                "aaestro_memory_operations_total",
                (("op", "recall"), ("space", "run:scratch"), ("status", "ok")),
            )
        ]
        == 2
    )
    assert space_labels(metrics, "aaestro_memory_operations") == {"run:scratch", "run:notes"}
    duration = space_labels(metrics, "aaestro_memory_search_duration_seconds")
    assert duration == {"run:scratch", "run:notes"}


def test_failed_search_and_recall_use_the_folded_label(monkeypatch):
    metrics = Metrics(registry=CollectorRegistry())
    recall = recall_over(metrics, RUN_SPACES)

    def broken(*_args, **_kwargs):
        raise AaestroError(
            category=ErrorCategory.MEMORY, code="MEM_004", message="index lookup failed"
        )

    monkeypatch.setattr(SpaceIndex, "search_vector", broken)
    auto = AutoRecall(recall=recall, policy=RecallSpec())

    with pytest.raises(AaestroError):
        auto.for_task("sidecar notes", spaces=list(RUN_SPACES))

    ops = samples(metrics, "aaestro_memory_operations")
    assert space_labels(metrics, "aaestro_memory_operations") == {"run:scratch", "run:notes"}
    assert {dict(labels)["status"] for _n, labels in ops} == {"failed"}
    assert (
        ops[
            (
                "aaestro_memory_operations_total",
                (("op", "search"), ("space", "run:scratch"), ("status", "failed")),
            )
        ]
        == 2
    )


@pytest.mark.parametrize(
    "space_id",
    [
        "run:r1:scratch",
        "run:x:resumed:scratch",  # run id 가 ':' 를 품는다
        "run:r:al ias",
        "run:r:al*",
        "run:r:alias\n",
        "run:r:",
        "run::alias",
        "run:alias",
        "local:a:longterm",
        "undeclared",
    ],
)
def test_space_label_agrees_with_the_run_space_id_pattern(space_id):
    """access 의 RUN_SPACE_ID 와 같은 입력에서 같은 판정 — 복제가 어긋나지 않는다."""
    match = RUN_SPACE_ID.fullmatch(space_id)
    expected = f"run:{match['alias']}" if match else space_id

    assert space_label(space_id) == expected
