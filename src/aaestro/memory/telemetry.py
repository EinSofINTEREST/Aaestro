"""Memory index metric collection.

검색 지연 / space 크기 / 인덱싱 지연 집계. 주입하지 않으면 아무 것도 기록하지
않는다 — 메모리 계층은 메트릭 없이도 온전히 동작한다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from aaestro.observability.metrics import Metrics

OP_SEARCH: Final = "search"
OP_RECALL: Final = "recall"
STATUS_OK: Final = "ok"


class IndexTelemetry:
    """Records the memory index metric contract.

    인덱스 계층의 표준 메트릭을 기록합니다. space 는 호출마다 달라지므로
    기록 시점에 받습니다.
    """

    def __init__(self, metrics: Metrics) -> None:
        self._metrics = metrics

    def operation(self, *, space: str, op: str, status: str = STATUS_OK) -> None:
        """검색/회상 연산을 감사 카운터에 남긴다.

        ``MemoryService._record`` 는 저장소 경유 연산(append/read/latest)만
        지나간다 — search/recall 은 인덱스를 직접 쓰므로 여기서 센다.
        09 는 세 계열을 모두 집계 대상으로 둔다.
        """
        self._metrics.counter("aaestro_memory_operations_total").labels(
            space=space, op=op, status=status
        ).inc()

    def search_finished(self, *, space: str, duration_s: float) -> None:
        """space 하나에 대한 검색 지연 — 느려지면 recall 예산이 태스크를 잡아먹는다."""
        self._metrics.histogram("aaestro_memory_search_duration_seconds").labels(
            space=space
        ).observe(duration_s)

    def entries(self, *, space: str, count: int) -> None:
        """space 가 담고 있는 항목 수 — 무한 성장은 검색 품질과 비용을 함께 망친다."""
        self._metrics.gauge("aaestro_memory_entries").labels(space=space).set(count)

    def index_lag(self, *, space: str, seconds: float) -> None:
        """가장 오래된 미색인 항목의 나이.

        09 는 eventual consistency 를 계약으로 두되 목표 지연(``index_lag_target_s``)
        을 둔다 — 그 목표를 지키는지 보려면 개수가 아니라 **시간**이어야 한다.
        """
        self._metrics.gauge("aaestro_memory_index_lag_seconds").labels(space=space).set(seconds)


__all__ = ["IndexTelemetry"]
