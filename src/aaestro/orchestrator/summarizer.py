"""Calls the compactor agent that writes a state summary.

state 압축의 요약 호출. 요약을 쓰는 것은 그래프 선언이 지목한 에이전트이고, 오케스트레이터는
runtime 을 통해 그 에이전트를 한 번 부를 뿐이다 — 모델을 직접 만지지 않는다.

템플릿은 태스크의 ``node_id`` 로 고른다 (agentd 의 템플릿 선택 계약). 계약은 state 압축
전용이다: 변수 ``items`` (array) 를 받고 출력 키 ``summary`` 에 요약문을 낸다.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aaestro.core.agent import (
    CONTROL_OVERHEAD_S,
    TaskConfig,
    TaskRequest,
    TaskStatus,
    TraceContext,
)
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.orchestrator.builder import CANCEL_TIMEOUT_S, CancellableRuntime
from aaestro.orchestrator.compaction import (
    SUMMARIZE_INPUT_KEY,
    SUMMARIZE_OUTPUT_KEY,
    SUMMARY_MAX_CHARS,
    summary_items,
)

SUMMARY_TIMEOUT_S = 120.0
"""요약 호출의 상한 — 노드 timeout 과 따로 둔다. 요약은 iteration 사이에서 기다리는 일이라,
노드만큼 오래 기다리면 그동안 run 이 멈춘 것처럼 보이고 drain 도 그만큼 늦는다."""

if TYPE_CHECKING:
    from aaestro.orchestrator.builder import NodeRuntime
    from aaestro.orchestrator.topology import NodeSpec


def summary_error(code: ErrorCode, message: str, **details: Any) -> AaestroError:
    """요약 호출의 실패를 GRAPH 카테고리로 — 경계가 받아 WARN 으로 남긴다."""
    return AaestroError(category=ErrorCategory.GRAPH, code=code, message=message, details=details)


@dataclass(frozen=True)
class StateSummarizer:
    """Asks the declared compactor agent for one summary.

    선언된 요약 에이전트를 한 번 부른다.

    Attributes:
        runtime: Invokes the agent — 노드 실행과 같은 계약이다.
        graph: The graph this summary belongs to, for the trace label.
        timeout_s: The task's own limit; the wait adds ``control_overhead_s`` on top
            so agentd 의 ``TO_001`` 결과가 돌아올 틈이 있다.
    """

    runtime: NodeRuntime
    graph: str
    timeout_s: float
    control_overhead_s: float = CONTROL_OVERHEAD_S

    async def summarize(
        self, node: NodeSpec, older: list[Any], *, run_id: str, trace_id: str
    ) -> str:
        """Return the summary text for the folded items.

        접을 항목의 요약문을 돌려줍니다.

        Args:
            node: The compactor agent, with the template name as its id.
            older: The state items being folded — untrusted agent output.
            run_id: The run this belongs to.
            trace_id: The run's trace.

        Raises:
            AaestroError: GRAPH/``TO_003`` on timeout, ``GRAPH_002`` if the task
                failed, ``GRAPH_003`` if the output carries no summary text or one
                longer than ``SUMMARY_MAX_CHARS``.
        """
        task = TaskRequest(
            task_id=str(uuid.uuid4()),
            run_id=run_id,
            node_id=node.id,
            # state 항목은 에이전트 산출물이다 — 문자열로 굳히고 길이를 자른 뒤, 지시가 아니라
            # 템플릿 변수(데이터)로만 넘긴다
            input={SUMMARIZE_INPUT_KEY: summary_items(older)},
            config=TaskConfig(timeout_s=self.timeout_s),
            trace=TraceContext(trace_id=trace_id, graph=self.graph),
        )
        try:
            result = await asyncio.wait_for(
                self.runtime.invoke(node, task),
                timeout=self.timeout_s + self.control_overhead_s,
            )
        except TimeoutError as err:
            await self._cancel_quietly(node, task.task_id)
            raise summary_error(
                ErrorCode.TO_003, "state compactor timed out", task_id=task.task_id
            ) from err

        if result.status is not TaskStatus.COMPLETED:
            raise summary_error(
                ErrorCode.GRAPH_002,
                "state compactor task failed",
                task_id=task.task_id,
                error_code=result.error.code if result.error else None,
            )
        summary = result.output.get(SUMMARIZE_OUTPUT_KEY)
        if not isinstance(summary, str) or not summary.strip():
            raise summary_error(
                ErrorCode.GRAPH_003,
                "state compactor returned no summary text",
                task_id=task.task_id,
                output_key=SUMMARIZE_OUTPUT_KEY,
            )
        if len(summary) > SUMMARY_MAX_CHARS:
            # 잘라 넣지 않는다 — 무엇이 빠졌는지 모르는 요약이 원문 자리를 차지하게 된다
            raise summary_error(
                ErrorCode.GRAPH_003,
                "state compactor returned a summary over the length limit",
                task_id=task.task_id,
                summary_chars=len(summary),
                max_chars=SUMMARY_MAX_CHARS,
            )
        return summary

    async def _cancel_quietly(self, node: NodeSpec, task_id: str) -> None:
        """먼저 끊은 요약 태스크의 취소를 best-effort 로 알린다 — 알리지 않으면 비용만 쓴다."""
        if not isinstance(self.runtime, CancellableRuntime):
            return
        try:
            await asyncio.wait_for(self.runtime.cancel(node, task_id), CANCEL_TIMEOUT_S)
        except (AaestroError, TimeoutError):
            # 호출자가 곧 TO_003 을 WARN 으로 남긴다 — 취소 실패가 그 원인을 가리지 않게 한다
            return


__all__ = ["SUMMARY_TIMEOUT_S", "StateSummarizer", "summary_error"]
