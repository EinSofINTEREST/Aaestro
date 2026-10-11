"""Task-loop metric collection.

에이전트 실행 루프의 메트릭 집계. Executor 본문이 계측 코드로 덮이지 않도록
별도 콜라보레이터로 분리한다 — 주입하지 않으면 아무 것도 기록하지 않는다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from malkuth.core.tools import is_mcp_tool

if TYPE_CHECKING:
    from malkuth.core.agent import ModelUsage
    from malkuth.observability.metrics import Metrics

STATUS_COMPLETED: Final = "completed"
STATUS_FAILED: Final = "failed"
STATUS_RATE_LIMITED: Final = "rate_limited"
"""05 는 rate limit 을 failed 와 분리하라고 규정한다 — 재시도 전략이 다르고,
ModelRateLimited 알림이 **이 값으로 필터**하기 때문이다. 뭉개면 알림이 영원히
침묵한다."""

SOURCE_MCP: Final = "mcp"
SOURCE_SKILLSET: Final = "skillset"

DIRECTION_INPUT: Final = "input"
DIRECTION_OUTPUT: Final = "output"


def tool_source(name: str) -> str:
    """tool 의 출처 — 네임스페이스 접두사로 판별한다 (05 표준 라벨)."""
    return SOURCE_MCP if is_mcp_tool(name) else SOURCE_SKILLSET


DIRECT_GRAPH = "direct"
"""그래프 밖 태스크의 ``graph`` 라벨 — 빈 문자열은 미기록과 구분되지 않는다."""


class ExecutorTelemetry:
    """Records the agent-execution metric contract.

    태스크·모델·tool 경로의 표준 메트릭을 기록합니다. ``agent`` 와 ``group`` 은
    배치 시점에 정해지므로 생성 시 받고, ``graph`` 는 **태스크마다 다르므로**
    (같은 에이전트가 그래프 노드로도 direct 요청으로도 불립니다) 기록 시점에
    받습니다.
    """

    def __init__(
        self,
        metrics: Metrics,
        *,
        agent: str,
        group: str = "",
        provider: str = "",
        model: str = "",
    ) -> None:
        self._metrics = metrics
        self._agent = agent
        self._group = group
        self._provider = provider
        self._model = model

    def task_finished(self, *, status: str, duration_s: float, graph: str | None = None) -> None:
        """태스크 종료 — 상태별 카운터와 latency 히스토그램.

        ``graph`` 가 없는 태스크(direct 요청)는 ``"direct"`` 로 표시합니다 —
        빈 문자열이면 "그래프 없음"과 "라벨을 못 채움"이 구분되지 않습니다.
        """
        label = graph or DIRECT_GRAPH
        self._metrics.counter("malkuth_agent_tasks_total").labels(
            agent=self._agent, group=self._group, graph=label, status=status
        ).inc()
        self._metrics.histogram("malkuth_agent_task_duration_seconds").labels(
            agent=self._agent, group=self._group, graph=label
        ).observe(duration_s)

    def model_called(
        self,
        *,
        status: str,
        usage: ModelUsage | None = None,
        provider: str | None = None,
        model: str | None = None,
        fallback_index: int = 0,
    ) -> None:
        """모델 호출 한 시도 — 요청 카운터와 방향별 토큰.

        ``provider`` / ``model`` 은 **실제로 부른** 모델이다 (fallback 이면 그 모델). 생략하면
        생성 시 받은 기본 라벨을 쓴다. ``fallback_index`` 0 이 primary 다 (#359).
        """
        provider = provider if provider is not None else self._provider
        model = model if model is not None else self._model
        self._metrics.counter("malkuth_model_requests_total").labels(
            agent=self._agent,
            provider=provider,
            model=model,
            status=status,
            fallback_index=str(fallback_index),
        ).inc()
        if usage is None:
            return
        tokens = self._metrics.counter("malkuth_model_tokens_total")
        for direction, count in (
            (DIRECTION_INPUT, usage.input_tokens),
            (DIRECTION_OUTPUT, usage.output_tokens),
        ):
            if count:
                tokens.labels(agent=self._agent, model=model, direction=direction).inc(count)

    def tool_called(self, *, tool: str, status: str) -> None:
        """tool 호출 — skillset/mcp 를 source 라벨로 구분한다."""
        self._metrics.counter("malkuth_tool_calls_total").labels(
            agent=self._agent, source=tool_source(tool), tool=tool, status=status
        ).inc()
