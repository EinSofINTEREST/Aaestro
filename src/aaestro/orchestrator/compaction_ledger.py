"""Outcome bookkeeping for state compaction.

압축 결과의 기록 — 로그 · 메트릭, 그리고 연속 실패한 필드의 재시도 간격.

실패한 요약을 매 경계 다시 두드리지 않는다: 원인이 이어지는 동안 (컨텍스트 초과 등) 호출마다
더 큰 프롬프트로 비용만 쓰기 때문이다. 간격은 경계 수로 늘린다 (1 · 2 · 4 · 8 · 16).
연속 실패 횟수는 프로세스 메모리에만 있다 — 재시작하면 0 부터 다시 센다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.orchestrator.compaction import estimate_tokens
from aaestro.orchestrator.telemetry import STATUS_COMPLETED, STATUS_FAILED

if TYPE_CHECKING:
    from aaestro.orchestrator.compaction import CompactionPolicy
    from aaestro.orchestrator.compaction_boundary import Boundary
    from aaestro.orchestrator.telemetry import OrchestratorTelemetry

log = structlog.get_logger(__name__)

MAX_BACKOFF_BOUNDARIES = 16
"""연속 실패한 필드를 다시 시도하기까지 기다리는 경계 수의 상한."""


class CompactionLedger:
    """Records each field's compaction outcome and decides when a failing field is retried.

    필드별 압축 결과를 남기고, 실패가 이어지는 필드의 다음 시도 시점을 정한다.
    """

    def __init__(self, *, graph: str, telemetry: OrchestratorTelemetry | None) -> None:
        self._graph = graph
        self._telemetry = telemetry
        self._failure_streak: dict[str, int] = {}
        self._boundaries_to_skip: dict[str, int] = {}

    def backing_off(self, name: str) -> bool:
        """연속 실패한 필드의 이번 경계를 건너뛸지 — 건너뛰면 남은 대기를 하나 줄인다."""
        remaining = self._boundaries_to_skip.get(name, 0)
        if remaining <= 0:
            return False
        self._boundaries_to_skip[name] = remaining - 1
        return True

    def succeeded(
        self,
        name: str,
        policy: CompactionPolicy,
        boundary: Boundary,
        *,
        before: list[Any],
        after: list[Any],
    ) -> None:
        self._failure_streak.pop(name, None)
        self._boundaries_to_skip.pop(name, None)
        if self._telemetry is not None:
            self._telemetry.state_compacted(
                field=name, strategy=policy.strategy, status=STATUS_COMPLETED
            )
        log.info(
            "state field compacted",
            graph=self._graph,
            state_field=name,
            strategy=policy.strategy,
            items_before=len(before),
            items_after=len(after),
            **boundary.log_fields(),
        )

    def failed(
        self, name: str, policy: CompactionPolicy, boundary: Boundary, err: Exception
    ) -> None:
        streak = self._failure_streak.get(name, 0) + 1
        self._failure_streak[name] = streak
        # 매 경계 다시 두드리지 않는다 — 실패가 이어지면 호출마다 더 큰 프롬프트로 비용만 쓴다.
        # 다음 시도까지의 간격은 1 · 2 · 4 … 경계 (상한 있음), 성공하면 처음으로 돌아간다
        retry_after = min(2 ** (streak - 1), MAX_BACKOFF_BOUNDARIES)
        self._boundaries_to_skip[name] = retry_after - 1
        details = err.details if isinstance(err, AaestroError) else {}
        if self._telemetry is not None:
            self._telemetry.state_compacted(
                field=name, strategy=policy.strategy, status=STATUS_FAILED
            )
        log.warning(
            "state compaction failed — state left as is",
            graph=self._graph,
            state_field=name,
            strategy=policy.strategy,
            failure_streak=streak,
            retry_after_boundaries=retry_after,
            error_code=err.code if isinstance(err, AaestroError) else ErrorCode.INTERNAL_001,
            # 원인을 버리지 않는다 — 요약 태스크와 그 하위 코드 (LLM_002 등). state 내용과
            # 에러 details 원문은 싣지 않는다
            task_id=details.get("task_id"),
            cause_code=details.get("error_code"),
            cause=type(err).__name__,
            **boundary.log_fields(),
        )

    def size(self, name: str, items: list[Any]) -> None:
        if self._telemetry is not None:
            self._telemetry.state_size(field=name, tokens=estimate_tokens(items))


__all__ = ["MAX_BACKOFF_BOUNDARIES", "CompactionLedger"]
