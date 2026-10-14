"""Prometheus metric registry.

프레임워크 표준 메트릭. 대시보드와 알림 규칙이 이 이름·라벨에 의존하므로,
변경은 곧 운영 자산을 깨뜨린다 — 스냅샷 테스트로 계약을 고정한다.

전역 registry 를 강제하지 않고 주입 가능하게 두어, 테스트가 프로세스 전역
상태를 오염시키지 않게 한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client import start_http_server as _start_http_server

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

if TYPE_CHECKING:
    from collections.abc import Iterator

DEFAULT_METRICS_PORT: Final = 9090
METRICS_PORT_ENV: Final = "AAESTRO_METRICS_PORT"

# 태스크 latency 는 p50/p95 관찰이 목적 — 초 단위 지수 버킷
_DURATION_BUCKETS: Final = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0)


@dataclass(frozen=True)
class MetricSpec:
    """A metric's declared contract.

    메트릭 계약 — 이름/타입/라벨. 스냅샷 테스트가 이 표현을 비교한다.
    """

    name: str
    kind: str
    labels: tuple[str, ...]
    documentation: str


METRIC_SPECS: Final[tuple[MetricSpec, ...]] = (
    # Task — 에이전트 단위 메트릭은 group 라벨 포함 (그룹별 집계/quota 감시용)
    MetricSpec(
        "aaestro_agent_tasks_total",
        "counter",
        ("agent", "group", "graph", "status"),
        "Agent tasks by terminal status",
    ),
    MetricSpec(
        "aaestro_agent_task_duration_seconds",
        "histogram",
        ("agent", "group", "graph"),
        "Agent task duration",
    ),
    # Model
    MetricSpec(
        "aaestro_model_requests_total",
        "counter",
        ("agent", "provider", "model", "status", "fallback_index"),
        "Model API requests by status",
    ),
    MetricSpec(
        "aaestro_model_tokens_total",
        "counter",
        ("agent", "model", "direction"),
        "Model tokens consumed by direction",
    ),
    # Decision model — 01 Decision Models
    MetricSpec(
        "aaestro_decision_calls_total",
        "counter",
        ("agent", "provider", "model", "use", "status"),
        "Decision model calls by use and status",
    ),
    MetricSpec(
        "aaestro_decision_bands_total",
        "counter",
        ("agent", "use", "question", "band"),
        "Decision outcomes by band (act / uncertain / reject)",
    ),
    MetricSpec(
        "aaestro_decision_duration_seconds",
        "histogram",
        ("agent", "provider"),
        "Decision model call latency",
    ),
    MetricSpec(
        "aaestro_decision_unavailable_total",
        "counter",
        ("agent", "provider", "reason"),
        "Decisions that fell back to the original path (timeout / error / circuit_open)",
    ),
    # Tool / protocol
    MetricSpec(
        "aaestro_tool_calls_total",
        "counter",
        ("agent", "source", "tool", "status"),
        "Tool calls by source and status",
    ),
    MetricSpec(
        "aaestro_mcp_tool_calls_total",
        "counter",
        ("agent", "server", "tool", "status"),
        "MCP tool calls by server and status",
    ),
    MetricSpec(
        "aaestro_a2a_calls_total",
        "counter",
        ("caller", "callee", "status"),
        "A2A peer calls by status",
    ),
    # Runtime
    MetricSpec("aaestro_containers_running", "gauge", ("agent",), "Running agent containers"),
    MetricSpec(
        "aaestro_container_restarts_total",
        "counter",
        ("agent", "reason"),
        "Container restarts by reason",
    ),
    MetricSpec("aaestro_agent_health", "gauge", ("agent",), "Agent health: 1 healthy, 0 unhealthy"),
    # Orchestrator
    MetricSpec("aaestro_runs_active", "gauge", ("graph", "mode"), "Active graph runs"),
    MetricSpec(
        "aaestro_runs_total", "counter", ("graph", "mode", "status"), "Graph runs by status"
    ),
    MetricSpec(
        "aaestro_node_duration_seconds", "histogram", ("graph", "node_id"), "Node execution time"
    ),
    MetricSpec(
        "aaestro_checkpoint_operations_total",
        "counter",
        ("operation", "status"),
        "Checkpoint operations by status",
    ),
    # Service run
    MetricSpec(
        "aaestro_service_iterations_total",
        "counter",
        ("graph", "status"),
        "Service run iterations by status",
    ),
    MetricSpec(
        "aaestro_service_idle_delay_seconds",
        "gauge",
        ("graph",),
        "Current service idle backoff delay",
    ),
    # Graph state compaction
    MetricSpec(
        "aaestro_state_compactions_total",
        "counter",
        ("graph", "field", "strategy", "status"),
        "Graph state field compactions by strategy and status",
    ),
    MetricSpec(
        "aaestro_state_field_tokens",
        "gauge",
        ("graph", "field"),
        "Estimated tokens of a state field that declares compaction",
    ),
    # Memory — operations 의 space 는 space id (별칭 아님), 해석되지 않은 접근은 "undeclared"
    MetricSpec(
        "aaestro_memory_operations_total",
        "counter",
        ("space", "op", "status"),
        "Memory operations by kind and status",
    ),
    MetricSpec(
        "aaestro_memory_search_duration_seconds", "histogram", ("space",), "Memory search latency"
    ),
    MetricSpec("aaestro_memory_entries", "gauge", ("space",), "Entries held per memory space"),
    MetricSpec(
        "aaestro_memory_index_lag_seconds", "gauge", ("space",), "Indexing queue lag per space"
    ),
    MetricSpec(
        "aaestro_memory_recall_injected_tokens",
        "gauge",
        ("agent",),
        "Tokens injected into prompts by auto-recall",
    ),
    MetricSpec(
        "aaestro_memory_recall_filtered_total",
        "counter",
        ("agent", "band"),
        "Recalled entries seen by the decision-model filter, by band",
    ),
    # Circuit breaker
    MetricSpec(
        "aaestro_circuit_state",
        "gauge",
        ("target",),
        "Circuit state: 0 closed, 1 open, 2 half-open",
    ),
    # Access control — 01 Access Control
    MetricSpec(
        "aaestro_access_decisions_total",
        "counter",
        ("resource", "decision", "source"),
        "Access decisions by resource, outcome, and where the answer came from",
    ),
    MetricSpec(
        "aaestro_access_grants_total",
        "counter",
        ("resource", "op", "decided_by"),
        "Grant, revoke, and refused-grant operations",
    ),
    MetricSpec(
        "aaestro_access_registry_reachable",
        "gauge",
        ("component",),
        "Whether an enforcement point can reach the access registry: 1 or 0",
    ),
    MetricSpec(
        "aaestro_access_cache_invalidations_total",
        "counter",
        ("component",),
        "Decision cache invalidations triggered by registry change notifications",
    ),
)
"""표준 메트릭 계약 — 05 의 Metrics Collection 과 1:1 대응."""


class Metrics:
    """Framework metrics bound to one registry.

    하나의 registry 에 묶인 프레임워크 메트릭. registry 를 주입하면 테스트가
    전역 상태를 건드리지 않고 격리된다.
    """

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self._registry = registry or CollectorRegistry()
        self._metrics: dict[str, Counter | Gauge | Histogram] = {}
        for spec in METRIC_SPECS:
            self._metrics[spec.name] = self._build(spec)

    def _build(self, spec: MetricSpec) -> Counter | Gauge | Histogram:
        """스펙대로 collector 를 만든다."""
        if spec.kind == "counter":
            return Counter(spec.name, spec.documentation, spec.labels, registry=self._registry)
        if spec.kind == "gauge":
            return Gauge(spec.name, spec.documentation, spec.labels, registry=self._registry)
        return Histogram(
            spec.name,
            spec.documentation,
            spec.labels,
            registry=self._registry,
            buckets=_DURATION_BUCKETS,
        )

    @property
    def registry(self) -> CollectorRegistry:
        """이 인스턴스가 쓰는 registry."""
        return self._registry

    def __getitem__(self, name: str) -> Counter | Gauge | Histogram:
        """이름으로 메트릭을 조회한다.

        Raises:
            KeyError: If the metric is not part of the standard contract.
        """
        return self._metrics[name]

    def gauge(self, name: str) -> Gauge:
        """Look up a gauge by name.

        게이지를 이름으로 조회합니다 — ``__getitem__`` 은 union 을 돌려주므로
        ``.set()`` 을 쓰는 호출부가 매번 캐스팅해야 합니다.

        Raises:
            KeyError: If the metric is not part of the standard contract.
            TypeError: If the metric exists but is not a gauge.
        """
        metric = self._metrics[name]
        if not isinstance(metric, Gauge):
            raise TypeError(f"metric is not a gauge: {name}")
        return metric

    def counter(self, name: str) -> Counter:
        """Look up a counter by name.

        카운터를 이름으로 조회합니다 — ``__getitem__`` 은 union 을 돌려주므로
        ``.inc()`` 를 쓰는 호출부가 매번 캐스팅해야 합니다.

        Raises:
            KeyError: If the metric is not part of the standard contract.
            TypeError: If the metric exists but is not a counter.
        """
        metric = self._metrics[name]
        if not isinstance(metric, Counter):
            raise TypeError(f"metric is not a counter: {name}")
        return metric

    def histogram(self, name: str) -> Histogram:
        """Look up a histogram by name.

        히스토그램을 이름으로 조회합니다 — ``.observe()`` 호출부의 캐스팅을 없앱니다.

        Raises:
            KeyError: If the metric is not part of the standard contract.
            TypeError: If the metric exists but is not a histogram.
        """
        metric = self._metrics[name]
        if not isinstance(metric, Histogram):
            raise TypeError(f"metric is not a histogram: {name}")
        return metric

    def names(self) -> frozenset[str]:
        """등록된 메트릭 이름 집합."""
        return frozenset(self._metrics)

    def __iter__(self) -> Iterator[str]:
        """등록된 메트릭 이름을 순회한다."""
        return iter(self._metrics)


def snapshot(specs: tuple[MetricSpec, ...] = METRIC_SPECS) -> dict[str, dict[str, object]]:
    """Render the metric contract as a comparable mapping.

    메트릭 계약을 비교 가능한 매핑으로 렌더링합니다 — 스냅샷 테스트가 이 결과를
    고정해, 대시보드·알림이 의존하는 이름/라벨의 의도치 않은 변경을 감지합니다.
    """
    return {spec.name: {"kind": spec.kind, "labels": list(spec.labels)} for spec in specs}


MAX_PORT: Final = 65535


def metrics_port_from_env(key: str) -> int:
    """The metrics port from the environment — ``CFG_001`` unless it is an integer in 0..65535.

    메트릭 포트 설정 읽기. 0 은 임시 포트 (테스트 · 충돌 회피). 잘못된 값은 ``ValueError``
    traceback 이 아니라 기동 가드가 받는 ``CFG_001`` 이다.
    """
    raw = os.environ.get(key, str(DEFAULT_METRICS_PORT))
    try:
        port = int(raw)
    except ValueError as err:
        raise _bad_port(f"invalid port: {raw!r}", key) from err
    if not 0 <= port <= MAX_PORT:
        raise _bad_port(f"port out of range: {raw!r}", key)
    return port


def _bad_port(message: str, key: str) -> AaestroError:
    return AaestroError(
        category=ErrorCategory.CONFIG,
        code=ErrorCode.CFG_001,
        message=message,
        details={"setting": key},
    )


def start_metrics_server(
    port: int = DEFAULT_METRICS_PORT, *, registry: CollectorRegistry | None = None
) -> None:
    """Expose metrics over HTTP.

    메트릭을 HTTP 로 노출합니다 (Prometheus scrape 대상).

    Args:
        port: Listen port.
        registry: Registry to expose; the process default when omitted.
    """
    if registry is None:
        _start_http_server(port)
        return
    _start_http_server(port, registry=registry)
