"""Circuit breaker state metrics.

Circuit breaker 상태를 게이지에 반영한다. ``core`` 는 관측 계층에 의존하지
않으므로, breaker 소유자가 이 관찰자를 주입한다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from aaestro.core.errors import CircuitState

if TYPE_CHECKING:
    from aaestro.observability.metrics import Metrics

# aaestro_circuit_state 의 값 규약 (05 Metrics Collection)
CIRCUIT_CLOSED: Final = 0.0
CIRCUIT_OPEN: Final = 1.0
CIRCUIT_HALF_OPEN: Final = 2.0

_CIRCUIT_VALUES: Final[dict[CircuitState, float]] = {
    CircuitState.CLOSED: CIRCUIT_CLOSED,
    CircuitState.OPEN: CIRCUIT_OPEN,
    CircuitState.HALF_OPEN: CIRCUIT_HALF_OPEN,
}


class CircuitTelemetry:
    """Tracks circuit breaker state transitions.

    circuit breaker 상태를 게이지로 반영합니다 — open 전환이 관측되지 않으면
    장애 확산을 사후에 재구성할 수 없습니다.
    """

    def __init__(self, metrics: Metrics, *, target: str) -> None:
        self._metrics = metrics
        self._target = target

    def observe(self, state: CircuitState) -> None:
        """``CircuitBreaker.on_transition`` 에 물리는 관찰자."""
        self._metrics.gauge("aaestro_circuit_state").labels(target=self._target).set(
            _CIRCUIT_VALUES[state]
        )
