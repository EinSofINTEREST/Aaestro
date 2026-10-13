"""Orchestration metric collection.

Run / node / iteration / checkpoint 메트릭 집계. 주입하지 않으면 아무 것도
기록하지 않는다 — 오케스트레이터는 메트릭 없이도 온전히 동작한다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from prometheus_client import Gauge

if TYPE_CHECKING:
    from aaestro.observability.metrics import Metrics
    from aaestro.orchestrator.topology import GraphMode

STATUS_COMPLETED: Final = "completed"
STATUS_FAILED: Final = "failed"

OPERATION_SAVE: Final = "save"
OPERATION_LOAD: Final = "load"


class OrchestratorTelemetry:
    """Records the run/node/iteration metric contract.

    한 그래프의 run 계측. ``graph``/``mode`` 는 run 마다 바뀌지 않으므로
    생성 시 고정하고, 나머지 라벨만 기록 시점에 받습니다.
    """

    def __init__(self, metrics: Metrics, *, graph: str, mode: GraphMode) -> None:
        self._metrics = metrics
        self._graph = graph
        self._mode = mode.value

    def _active(self) -> Gauge:
        """active run 게이지 — graph/mode 라벨이 고정된 자식."""
        return self._metrics.gauge("aaestro_runs_active").labels(graph=self._graph, mode=self._mode)

    def run_started(self) -> None:
        """run 슬롯 확보 — active 게이지를 올린다."""
        self._active().inc()

    def run_finished(self, *, status: str) -> None:
        """run 종료 — active 게이지를 내리고 상태별 카운터를 올린다."""
        self._active().dec()
        self._metrics.counter("aaestro_runs_total").labels(
            graph=self._graph, mode=self._mode, status=status
        ).inc()

    def node_finished(self, *, node_id: str, duration_s: float) -> None:
        """노드 실행 시간 — 실패한 노드도 관측 대상이다."""
        self._metrics.histogram("aaestro_node_duration_seconds").labels(
            graph=self._graph, node_id=node_id
        ).observe(duration_s)

    def iteration_finished(self, *, status: str) -> None:
        """Service iteration 한 회차 — ServiceRunStalled 알림이 이 값에 의존한다."""
        self._metrics.counter("aaestro_service_iterations_total").labels(
            graph=self._graph, status=status
        ).inc()

    def idle_delay(self, seconds: float) -> None:
        """현재 idle backoff — 상주 그래프가 얼마나 쉬고 있는지 보여준다."""
        self._metrics.gauge("aaestro_service_idle_delay_seconds").labels(graph=self._graph).set(
            seconds
        )

    def state_compacted(self, *, field: str, strategy: str, status: str) -> None:
        """state 필드 압축 한 번 — 실패도 센다 (연속 실패는 state 가 계속 자란다는 뜻이다)."""
        self._metrics.counter("aaestro_state_compactions_total").labels(
            graph=self._graph, field=field, strategy=strategy, status=status
        ).inc()

    def state_size(self, *, field: str, tokens: int) -> None:
        """압축을 선언한 state 필드의 추정 토큰 — 경계마다 갱신한다."""
        self._metrics.gauge("aaestro_state_field_tokens").labels(
            graph=self._graph, field=field
        ).set(tokens)


class CheckpointTelemetry:
    """Records checkpoint operation outcomes.

    Checkpoint 저장/복원 결과를 집계합니다 — ``CheckpointFailures`` 알림이
    이 카운터에 의존합니다 (실패하면 run 복구가 위태롭습니다).
    """

    def __init__(self, metrics: Metrics) -> None:
        self._metrics = metrics

    def operation(self, *, operation: str, status: str) -> None:
        """저장/복원 한 번의 결과를 남긴다."""
        self._metrics.counter("aaestro_checkpoint_operations_total").labels(
            operation=operation, status=status
        ).inc()
