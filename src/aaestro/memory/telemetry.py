"""Memory index metric collection.

검색 지연 / space 크기 / 인덱싱 지연 집계. 주입하지 않으면 아무 것도 기록하지
않는다 — 메모리 계층은 메트릭 없이도 온전히 동작한다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from aaestro.core.manifest import MEMORY_ALIAS_CHARS

if TYPE_CHECKING:
    from aaestro.observability.metrics import Metrics

OP_SEARCH: Final = "search"
OP_RECALL: Final = "recall"
STATUS_OK: Final = "ok"
STATUS_FAILED: Final = "failed"
UNDECLARED_SPACE_LABEL: Final = "undeclared"
"""space 로 해석되지 않은 접근의 메트릭 ``space`` 라벨.

호출자가 고른 별칭을 라벨로 쓰면 시계열 수를 호출자가 정한다 — 해석되지 않은 것은 한 값으로 센다."""


# 별칭은 마지막 조각이다 — run id 는 ``:`` 를 품을 수 있다.
# access/baselines.py 의 RUN_SPACE_ID 와 같은 모양 — agentd 는 access 를 import 하지 못한다
_RUN_SPACE_ID = re.compile(rf"run:(?P<run>.+):(?P<alias>{MEMORY_ALIAS_CHARS})")


def space_label(space_id: str) -> str:
    """메트릭 `space` 라벨 — run space 는 run 을 뺀 ``run:<alias>`` 다 (09 메트릭).

    run id 가 라벨에 들면 run 마다 새 시계열이 생겨 카디널리티가 끝없이 자란다.
    """
    match = _RUN_SPACE_ID.fullmatch(space_id)
    return f"run:{match['alias']}" if match else space_id


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
            space=space_label(space), op=op, status=status
        ).inc()

    def search_finished(self, *, space: str, duration_s: float) -> None:
        """space 하나에 대한 검색 지연 — 느려지면 recall 예산이 태스크를 잡아먹는다."""
        self._metrics.histogram("aaestro_memory_search_duration_seconds").labels(
            space=space_label(space)
        ).observe(duration_s)

    def space_gauges(self, *, sizes: Mapping[str, int], lags: Mapping[str, float]) -> None:
        """space 별 항목 수와 인덱싱 지연 — 키는 원 space id 다.

        라벨로 접히는 space 들은 여기서 한 번에 모은다: 항목 수는 합, 지연은 최댓값 —
        접힌 라벨에 ``set`` 을 되풀이하면 마지막 space 만 남는다. 지연은 가장 오래된 미색인
        항목의 나이다 (09 의 목표 지연 ``index_lag_target_s`` 와 같은 단위 — 시간).
        """
        total: dict[str, int] = {}
        worst: dict[str, float] = {}
        for space, count in sizes.items():
            label = space_label(space)
            total[label] = total.get(label, 0) + count
            worst[label] = max(worst.get(label, 0.0), lags.get(space, 0.0))
        for label, count in total.items():
            self._metrics.gauge("aaestro_memory_entries").labels(space=label).set(count)
            self._metrics.gauge("aaestro_memory_index_lag_seconds").labels(space=label).set(
                worst[label]
            )


__all__ = ["UNDECLARED_SPACE_LABEL", "IndexTelemetry", "space_label"]
