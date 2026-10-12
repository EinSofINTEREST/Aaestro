"""Unit tests for the Prometheus metric registry.

메트릭 이름과 라벨은 대시보드·알림 규칙이 의존하는 계약이다 — 스냅샷으로
고정해 의도치 않은 변경을 감지한다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from aaestro.observability.metrics import (
    DEFAULT_METRICS_PORT,
    METRIC_SPECS,
    Metrics,
    snapshot,
)

RULES_DIR = Path(".claude/rules")


def documented_metric_names() -> set[str]:
    """룰셋에 등장하는 aaestro_* 메트릭 이름을 모은다."""
    names: set[str] = set()
    for doc in RULES_DIR.glob("*.md"):
        names.update(re.findall(r"aaestro_[a-z0-9_]+", doc.read_text(encoding="utf-8")))
    return names


# --- 계약 대조 --------------------------------------------------------------


def test_every_documented_metric_is_registered():
    """룰셋에 선언된 메트릭이 하나도 빠지지 않아야 한다."""
    registered = {spec.name for spec in METRIC_SPECS}

    assert documented_metric_names() - registered == set()


def test_no_undocumented_metrics_are_registered():
    """문서에 없는 메트릭을 몰래 늘리지 않는다."""
    registered = {spec.name for spec in METRIC_SPECS}

    assert registered - documented_metric_names() == set()


def test_metric_names_are_unique():
    names = [spec.name for spec in METRIC_SPECS]

    assert len(names) == len(set(names))


def test_all_metric_names_are_prefixed():
    assert all(spec.name.startswith("aaestro_") for spec in METRIC_SPECS)


def test_counter_names_end_with_total():
    """Prometheus 관례 — counter 는 _total 접미사."""
    counters = [s.name for s in METRIC_SPECS if s.kind == "counter"]

    assert all(name.endswith("_total") for name in counters)


def test_histogram_names_carry_a_unit():
    histograms = [s.name for s in METRIC_SPECS if s.kind == "histogram"]

    assert all(name.endswith("_seconds") for name in histograms)


@pytest.mark.parametrize("spec", METRIC_SPECS, ids=lambda s: s.name)
def test_every_metric_documents_itself(spec):
    assert spec.documentation
    assert spec.kind in {"counter", "gauge", "histogram"}


@pytest.mark.parametrize("spec", METRIC_SPECS, ids=lambda s: s.name)
def test_label_names_are_snake_case(spec):
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", label) for label in spec.labels)


# --- 스냅샷 ----------------------------------------------------------------


def test_metric_contract_snapshot():
    """이름/타입/라벨 계약 고정.

    이 스냅샷이 깨지면 대시보드 패널과 알림 PromQL 이 함께 깨진다 —
    의도한 변경인지 반드시 확인하고 운영 자산을 같은 PR 에서 갱신해야 한다.
    """
    assert snapshot() == {
        "aaestro_agent_tasks_total": {
            "kind": "counter",
            "labels": ["agent", "group", "graph", "status"],
        },
        "aaestro_agent_task_duration_seconds": {
            "kind": "histogram",
            "labels": ["agent", "group", "graph"],
        },
        "aaestro_model_requests_total": {
            "kind": "counter",
            "labels": ["agent", "provider", "model", "status", "fallback_index"],
        },
        "aaestro_model_tokens_total": {
            "kind": "counter",
            "labels": ["agent", "model", "direction"],
        },
        "aaestro_tool_calls_total": {
            "kind": "counter",
            "labels": ["agent", "source", "tool", "status"],
        },
        "aaestro_mcp_tool_calls_total": {
            "kind": "counter",
            "labels": ["agent", "server", "tool", "status"],
        },
        "aaestro_a2a_calls_total": {
            "kind": "counter",
            "labels": ["caller", "callee", "status"],
        },
        "aaestro_containers_running": {"kind": "gauge", "labels": ["agent"]},
        "aaestro_container_restarts_total": {
            "kind": "counter",
            "labels": ["agent", "reason"],
        },
        "aaestro_agent_health": {"kind": "gauge", "labels": ["agent"]},
        "aaestro_runs_active": {"kind": "gauge", "labels": ["graph", "mode"]},
        "aaestro_runs_total": {"kind": "counter", "labels": ["graph", "mode", "status"]},
        "aaestro_node_duration_seconds": {
            "kind": "histogram",
            "labels": ["graph", "node_id"],
        },
        "aaestro_checkpoint_operations_total": {
            "kind": "counter",
            "labels": ["operation", "status"],
        },
        "aaestro_service_iterations_total": {
            "kind": "counter",
            "labels": ["graph", "status"],
        },
        "aaestro_service_idle_delay_seconds": {"kind": "gauge", "labels": ["graph"]},
        "aaestro_memory_operations_total": {
            "kind": "counter",
            "labels": ["space", "op", "status"],
        },
        "aaestro_memory_search_duration_seconds": {"kind": "histogram", "labels": ["space"]},
        "aaestro_memory_entries": {"kind": "gauge", "labels": ["space"]},
        "aaestro_memory_index_lag_seconds": {"kind": "gauge", "labels": ["space"]},
        "aaestro_memory_recall_injected_tokens": {"kind": "gauge", "labels": ["agent"]},
        "aaestro_memory_recall_filtered_total": {"kind": "counter", "labels": ["agent", "band"]},
        "aaestro_decision_calls_total": {
            "kind": "counter",
            "labels": ["agent", "provider", "model", "use", "status"],
        },
        "aaestro_decision_bands_total": {
            "kind": "counter",
            "labels": ["agent", "use", "question", "band"],
        },
        "aaestro_decision_duration_seconds": {
            "kind": "histogram",
            "labels": ["agent", "provider"],
        },
        "aaestro_decision_unavailable_total": {
            "kind": "counter",
            "labels": ["agent", "provider", "reason"],
        },
        "aaestro_circuit_state": {"kind": "gauge", "labels": ["target"]},
        "aaestro_access_decisions_total": {
            "kind": "counter",
            "labels": ["resource", "decision", "source"],
        },
        "aaestro_access_grants_total": {
            "kind": "counter",
            "labels": ["resource", "op", "decided_by"],
        },
        "aaestro_access_registry_reachable": {"kind": "gauge", "labels": ["component"]},
        "aaestro_access_cache_invalidations_total": {"kind": "counter", "labels": ["component"]},
    }


# --- registry 동작 ----------------------------------------------------------


def test_metrics_register_into_the_injected_registry():
    """주입한 registry 만 쓰므로 테스트가 전역 상태를 오염시키지 않는다."""
    registry = CollectorRegistry()

    metrics = Metrics(registry)

    assert metrics.registry is registry
    assert metrics.names() == {spec.name for spec in METRIC_SPECS}


def test_two_instances_do_not_collide():
    """같은 이름을 두 registry 에 등록해도 충돌하지 않는다."""
    Metrics(CollectorRegistry())
    Metrics(CollectorRegistry())


def test_counter_increments_are_exposed():
    metrics = Metrics(CollectorRegistry())

    metrics["aaestro_agent_tasks_total"].labels(
        agent="researcher", group="research", graph="pipeline", status="completed"
    ).inc()

    exposed = generate_latest(metrics.registry).decode()
    assert 'aaestro_agent_tasks_total{agent="researcher"' in exposed
    assert 'status="completed"} 1.0' in exposed


def test_gauge_set_is_exposed():
    metrics = Metrics(CollectorRegistry())

    metrics["aaestro_agent_health"].labels(agent="researcher").set(1)

    assert (
        'aaestro_agent_health{agent="researcher"} 1.0' in generate_latest(metrics.registry).decode()
    )


def test_histogram_observation_is_exposed():
    metrics = Metrics(CollectorRegistry())

    metrics["aaestro_node_duration_seconds"].labels(graph="g", node_id="planner").observe(0.5)

    exposed = generate_latest(metrics.registry).decode()
    assert "aaestro_node_duration_seconds_count" in exposed
    assert "aaestro_node_duration_seconds_bucket" in exposed


def test_histogram_buckets_cover_task_latency_range():
    """태스크 latency 는 초 단위로 넓게 퍼지므로 300s 까지 관찰 가능해야 한다."""
    metrics = Metrics(CollectorRegistry())
    metrics["aaestro_agent_task_duration_seconds"].labels(agent="a", group="g", graph="p").observe(
        120
    )

    exposed = generate_latest(metrics.registry).decode()
    assert 'le="300.0"' in exposed


def test_wrong_label_set_is_rejected():
    """라벨 계약을 어기면 즉시 실패해야 한다 — 조용한 카디널리티 폭발 방지."""
    metrics = Metrics(CollectorRegistry())

    with pytest.raises(ValueError, match="[Ii]ncorrect label names"):
        metrics["aaestro_agent_health"].labels(wrong="x")


def test_unknown_metric_lookup_raises():
    with pytest.raises(KeyError):
        Metrics(CollectorRegistry())["aaestro_not_a_metric"]


def test_metrics_is_iterable():
    metrics = Metrics(CollectorRegistry())

    assert set(metrics) == metrics.names()


def test_default_port_matches_the_ruleset():
    assert DEFAULT_METRICS_PORT == 9090
