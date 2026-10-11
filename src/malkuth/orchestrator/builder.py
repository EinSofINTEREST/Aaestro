"""Graph config to LangGraph StateGraph.

검증된 토폴로지를 LangGraph ``StateGraph`` 로 빌드한다. 노드는 runtime 호출
래퍼이며, 오케스트레이터는 Docker/프로토콜을 직접 만지지 않는다.

에이전트 실패가 graph state 를 오염시키지 않도록, 노드 실패는 ``GRAPH_002`` 로
변환되어 state 병합 없이 전파된다.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Hashable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, runtime_checkable

import structlog
from langgraph.graph import END as LG_END
from langgraph.graph import START as LG_START
from langgraph.graph import StateGraph
from pydantic import ValidationError

from malkuth.core.agent import (
    CONTROL_OVERHEAD_S,
    TaskConfig,
    TaskRequest,
    TaskResult,
    TaskStatus,
    TraceContext,
)
from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError, RetryPolicy
from malkuth.orchestrator.state import extract_input, merge_output, resolve_graph_state
from malkuth.orchestrator.telemetry import OrchestratorTelemetry
from malkuth.orchestrator.topology import (
    END,
    START,
    GraphTopology,
    NodeSpec,
    resolve_condition,
)
from malkuth.resilience import retrying

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langgraph.checkpoint.base import BaseCheckpointSaver
    from pydantic import BaseModel

    from malkuth.core.manifest import ModelConfig
    from malkuth.observability.metrics import Metrics

    # 노드 → 그 노드 태스크의 해석된 모델. None 이면 agentd 가 manifest 기본값을 쓴다
    ModelResolver = Callable[[NodeSpec], ModelConfig | None]

log = structlog.get_logger(__name__)

_ITERATION_KEY = "_iterations"
CANCEL_TIMEOUT_S = 5.0
"""먼저 끊은 태스크의 취소 요청을 기다리는 상한 — 취소는 best-effort 다."""


NODE_RETRY = RetryPolicy(
    max_attempts=1,
    initial_delay_s=1.0,
    max_delay_s=30.0,
    retryable_categories=(
        ErrorCategory.GRAPH,
        ErrorCategory.NETWORK,
        ErrorCategory.TIMEOUT,
    ),
)
"""노드 재시도의 기준 정책 — 횟수는 토폴로지의 ``retry`` 가 정한다.

카테고리는 **노드 실행이 실제로 내는 것**이다: 노드 timeout 은 GRAPH/TO_003,
런타임 도달 실패는 NETWORK 다. 계약 위반(GRAPH_003)은 retryable=False 라
정책이 집지 않는다.
"""


class _ReservedChannels(TypedDict, total=False):
    """프레임워크 예약 채널 — 그래프 state 계약과 별개로 항상 존재한다."""

    _iterations: dict[str, int]
    _run_id: str
    _trace_id: str


def build_channel_schema(schema: type[BaseModel]) -> type:
    """Build the LangGraph channel schema for a graph state model.

    그래프 state 모델로부터 LangGraph 채널 스키마를 만듭니다.
    LangGraph 는 선언된 채널만 보존하므로, state 스키마의 필드와 프레임워크
    예약 채널을 합쳐 채널 집합을 구성합니다. 값 검증은 ``state.py`` 의 병합
    규칙이 담당하므로 채널 타입 자체는 열어 둡니다.

    Args:
        schema: The graph state model declared by the topology.

    Returns:
        A TypedDict type usable as a LangGraph state schema.
    """
    annotations: dict[str, Any] = dict.fromkeys(schema.model_fields, Any)
    annotations.update(_ReservedChannels.__annotations__)
    return type(
        "GraphState",
        (dict,),
        {"__annotations__": annotations, "__total__": False, "__required_keys__": frozenset()},
    )


class NodeRuntime(Protocol):
    """Invokes an agent for a graph node.

    그래프 노드에 대응하는 에이전트를 호출하는 계약. 구현은 runtime 레이어가
    제공하며, 오케스트레이터는 이 계약 뒤의 컨테이너 사정을 알지 못한다.
    """

    async def invoke(self, node: NodeSpec, task: TaskRequest) -> TaskResult:
        """노드의 에이전트에게 태스크를 실행시킨다."""
        ...


@runtime_checkable
class CancellableRuntime(Protocol):
    """진행 중 태스크를 취소할 수 있는 노드 런타임 — orchestrator 가 먼저 끊을 때 쓴다."""

    async def cancel(self, node: NodeSpec, task_id: str) -> None:
        """노드의 에이전트에게 태스크 취소를 요청한다."""
        ...


def _graph_error(
    code: ErrorCode, message: str, *, retryable: bool = False, **details: Any
) -> MalkuthError:
    """그래프 실행 실패를 GRAPH 카테고리로 변환한다."""
    return MalkuthError(
        category=ErrorCategory.GRAPH,
        code=code,
        message=message,
        retryable=retryable,
        details=details,
    )


def _lg_endpoint(node_id: str) -> str:
    """토폴로지의 START/END 를 LangGraph 예약 노드로 변환한다."""
    if node_id == START:
        return LG_START
    if node_id == END:
        return LG_END
    return node_id


class GraphBuilder:
    """Builds a runnable graph from a validated topology.

    검증된 토폴로지로부터 실행 가능한 그래프를 빌드한다.
    """

    def __init__(
        self,
        topology: GraphTopology,
        runtime: NodeRuntime,
        *,
        state_schema: type[BaseModel] | None = None,
        node_timeout_s: float = 300.0,
        metrics: Metrics | None = None,
        retry_sleep: Callable[[float], Awaitable[None]] | None = None,
        control_overhead_s: float = CONTROL_OVERHEAD_S,
        models: ModelResolver | None = None,
    ) -> None:
        self._topology = topology
        self._models = models
        self._control_overhead_s = control_overhead_s
        self._runtime = runtime
        self._schema = state_schema or resolve_graph_state(topology.spec.state, graph=topology.name)
        self._node_timeout_s = node_timeout_s
        # 06 은 시간 의존 로직이 테스트에서 실제로 자는 것을 금지한다
        self._retry_sleep = retry_sleep
        self._telemetry = (
            None
            if metrics is None
            else OrchestratorTelemetry(metrics, graph=topology.name, mode=topology.mode)
        )

    @property
    def state_schema(self) -> type[BaseModel]:
        """그래프 state 스키마."""
        return self._schema

    def build(self, *, checkpointer: BaseCheckpointSaver[Any] | None = None) -> Any:
        """Compile the topology into a runnable LangGraph graph.

        토폴로지를 실행 가능한 LangGraph 그래프로 컴파일합니다.

        Args:
            checkpointer: Checkpointer attached to the compiled graph.

        Returns:
            The compiled graph.
        """
        graph: StateGraph[Any, Any, Any, Any] = StateGraph(build_channel_schema(self._schema))

        for node in self._topology.spec.nodes:
            graph.add_node(node.id, self._node_runner(node))

        self._wire_edges(graph)
        return graph.compile(checkpointer=checkpointer)

    def _wire_edges(self, graph: StateGraph[Any, Any, Any, Any]) -> None:
        """토폴로지 edge 를 LangGraph edge 로 옮긴다."""
        conditional_sources = {e.source for e in self._topology.spec.edges if e.condition}

        for edge in self._topology.spec.edges:
            if edge.source in conditional_sources:
                continue
            graph.add_edge(_lg_endpoint(edge.source), _lg_endpoint(edge.target))

        for source in conditional_sources:
            graph.add_conditional_edges(
                _lg_endpoint(source), self._router(source), self._route_map(source)
            )

    def _route_map(self, source: str) -> dict[Hashable, str]:
        """조건부 분기의 목적지 맵 — 라우터 반환값을 LangGraph 노드로 잇는다."""
        targets: dict[Hashable, str] = {
            edge.target: _lg_endpoint(edge.target)
            for edge in self._topology.spec.edges
            if edge.source == source
        }
        return targets

    def _router(self, source: str) -> Callable[[dict[str, Any]], str]:
        """조건 함수들을 선언 순서대로 평가하는 라우터를 만든다."""
        edges = [e for e in self._topology.spec.edges if e.source == source]
        conditions = [
            (edge, resolve_condition(edge.condition) if edge.condition else None) for edge in edges
        ]

        def route(state: dict[str, Any]) -> str:
            # 조건부 edge 를 선언 순서대로 먼저 평가한다 — 무조건 edge 를 만나는
            # 즉시 반환하면 그 뒤에 선언된 조건이 영영 평가되지 않는다
            for edge, condition in conditions:
                if condition is None or not condition(state):
                    continue
                if edge.max_iterations is not None:
                    self._check_iterations(state, edge.source, edge.max_iterations)
                return edge.target

            # 어떤 조건도 참이 아니면 무조건 edge 로 폴백, 없으면 토폴로지 결함
            for edge, condition in conditions:
                if condition is None:
                    return edge.target
            raise _graph_error(
                ErrorCode.GRAPH_001,
                f"no edge condition matched at node: {source}",
                graph=self._topology.name,
                node_id=source,
            )

        return route

    def _check_iterations(self, state: dict[str, Any], node_id: str, limit: int) -> None:
        """cycle edge 의 반복 상한을 강제한다 (mission 무한 루프 방지)."""
        counts = state.get(_ITERATION_KEY) or {}
        if counts.get(node_id, 0) >= limit:
            raise _graph_error(
                ErrorCode.GRAPH_004,
                f"max iterations exceeded at node: {node_id}",
                graph=self._topology.name,
                node_id=node_id,
                max_iterations=limit,
            )

    def _node_runner(self, node: NodeSpec) -> Any:
        """노드 실행 래퍼 — 입력 추출 → 호출 → 출력 투영."""

        async def run(state: dict[str, Any]) -> dict[str, Any]:
            # 태스크는 **재시도 밖에서** 만든다 — 회차마다 새 task_id 를 발급하면
            # agentd 의 멱등 캐시가 걸리지 않아 부수효과가 겹친다 (02 Rule 3)
            task = self._make_task(node, state)
            # 강제는 agentd 가 timeout_s 에 한다 — 같은 값으로 끊으면 왕복만큼 먼저 끊어
            # agentd 가 보낸 TO_001 결과를 버린다 (#314). 여기는 그 결과가 올 틈까지 기다린다
            wait_s = task.config.timeout_s + self._control_overhead_s

            async def attempt() -> TaskResult:
                try:
                    return await asyncio.wait_for(self._runtime.invoke(node, task), timeout=wait_s)
                except TimeoutError as err:
                    await self._cancel_quietly(node, task)
                    raise _graph_error(
                        ErrorCode.TO_003,
                        f"node timed out: {node.id}",
                        retryable=True,
                        graph=self._topology.name,
                        node_id=node.id,
                        task_id=task.task_id,
                    ) from err

            result = await self._attempt_node(node, attempt, task_id=task.task_id)

            if result.status is not TaskStatus.COMPLETED:
                # 실패한 노드의 출력은 state 에 병합하지 않는다 — state 오염 방지
                raise _graph_error(
                    ErrorCode.GRAPH_002,
                    f"node execution failed: {node.id}",
                    graph=self._topology.name,
                    node_id=node.id,
                    task_id=task.task_id,
                    error_code=result.error.code if result.error else None,
                )

            update = merge_output(node, result.output, schema=self._schema)
            return self._with_iteration(state, node.id, update)

        if self._telemetry is None:
            # 계측이 없으면 래퍼도 두지 않는다 — 선택적 경로가 상시 비용을 내면 안 된다
            return run

        telemetry = self._telemetry

        async def timed(state: dict[str, Any]) -> dict[str, Any]:
            """노드 latency 를 남긴다 — 실패한 노드도 관측 대상이다."""
            started = time.perf_counter()
            try:
                return await run(state)
            finally:
                telemetry.node_finished(node_id=node.id, duration_s=time.perf_counter() - started)

        return timed

    def _with_iteration(
        self, state: dict[str, Any], node_id: str, update: dict[str, Any]
    ) -> dict[str, Any]:
        """cycle 반복 횟수를 state 에 누적한다 (max_iterations 판정용)."""
        counts = dict(state.get(_ITERATION_KEY) or {})
        counts[node_id] = counts.get(node_id, 0) + 1
        return {**update, _ITERATION_KEY: counts}

    async def _attempt_node(
        self,
        node: NodeSpec,
        attempt: Callable[[], Awaitable[TaskResult]],
        *,
        task_id: str,
    ) -> TaskResult:
        """Run one node, retrying only when the topology asked for it.

        토폴로지가 요청한 만큼만 재시도합니다 (05 Retry Layering).

        **기본값 `retry: 0` 은 그대로 둔다**: agentd 가 이미 모델 호출을
        재시도하므로(#177), 노드 재시도가 그 위에 곱해진다 — 05 는 이것을
        "중복 주의" 로 명시한다. 기본을 켜면 모든 그래프가 그 곱을 떠안는다.
        """
        if node.retry <= 0:
            return await attempt()

        # 첫 시도 + retry 회 = 총 시도 횟수
        policy = replace(NODE_RETRY, max_attempts=node.retry + 1)
        return await retrying(
            policy,
            attempt,
            sleep=self._retry_sleep,
            graph=self._topology.name,
            node_id=node.id,
            task_id=task_id,
        )

    async def _cancel_quietly(self, node: NodeSpec, task: TaskRequest) -> None:
        """먼저 끊은 태스크의 취소를 best-effort 로 알린다.

        알리지 않으면 에이전트는 아무도 기다리지 않는 태스크를 끝까지 돌며 슬롯과 비용을 쓴다.
        취소 실패가 노드 실패의 원인(TO_003)을 가리지 않도록 경고만 남긴다.
        """
        if not isinstance(self._runtime, CancellableRuntime):
            return
        try:
            await asyncio.wait_for(self._runtime.cancel(node, task.task_id), CANCEL_TIMEOUT_S)
        except (MalkuthError, TimeoutError) as err:
            log.warning(
                "node task cancel failed",
                graph=self._topology.name,
                run_id=task.run_id,
                node_id=node.id,
                task_id=task.task_id,
                error_code=err.code if isinstance(err, MalkuthError) else ErrorCode.TO_003,
            )

    def _make_task(self, node: NodeSpec, state: dict[str, Any]) -> TaskRequest:
        """state 로부터 노드 태스크를 구성한다."""
        run_id = str(state.get("_run_id") or "run-unknown")
        trace_id = str(state.get("_trace_id") or run_id)
        return TaskRequest(
            task_id=str(uuid.uuid4()),
            run_id=run_id,
            node_id=node.id,
            input=extract_input(node, state, schema=self._schema),
            config=TaskConfig(
                timeout_s=node.timeout_s or self._node_timeout_s,
                model=self._resolve_model(node),
            ),
            trace=TraceContext(trace_id=trace_id, graph=self._topology.name),
        )

    def _resolve_model(self, node: NodeSpec) -> ModelConfig | None:
        """노드의 실효 모델을 해석한다 — 계층을 합친 결과가 모델이 아니면 GRAPH_002."""
        if self._models is None:
            return None
        try:
            return self._models(node)
        except ValidationError as err:
            # 계층 합성의 실패는 노드 실행 실패다 — pydantic 오류가 run 을 날것으로 터뜨리지 않게
            raise _graph_error(
                ErrorCode.GRAPH_002,
                "node model does not resolve to a valid model",
                graph=self._topology.name,
                node_id=node.id,
                reason=err.errors()[0]["msg"],
            ) from err


def build_graph(
    topology: GraphTopology,
    runtime: NodeRuntime,
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    state_schema: type[BaseModel] | None = None,
    node_timeout_s: float = 300.0,
    metrics: Metrics | None = None,
    retry_sleep: Callable[[float], Awaitable[None]] | None = None,
    control_overhead_s: float = CONTROL_OVERHEAD_S,
    models: ModelResolver | None = None,
) -> Any:
    """Build a runnable graph from a topology.

    토폴로지로부터 실행 가능한 그래프를 빌드합니다 (편의 함수).

    Args:
        topology: A validated topology.
        runtime: Node runtime used to invoke agents.
        checkpointer: Checkpointer attached to the compiled graph.
        state_schema: Optional pre-resolved state schema.
        node_timeout_s: Default per-node timeout.
        metrics: Optional metric registry for node latency.
        retry_sleep: Injected wait for node retries — 06 은 시간 의존 로직이
            테스트에서 실제로 자는 것을 금지합니다.
        control_overhead_s: 노드 timeout 위에 더 기다리는 여유 — agentd 의 ``TO_001``
            결과가 돌아올 틈이다.
        models: Resolves each node's model into ``TaskConfig.model`` — 생략하면 모델을
            싣지 않아 agentd 가 manifest 기본값을 쓴다.

    Returns:
        The compiled graph.
    """
    builder = GraphBuilder(
        topology,
        runtime,
        state_schema=state_schema,
        node_timeout_s=node_timeout_s,
        retry_sleep=retry_sleep,
        metrics=metrics,
        control_overhead_s=control_overhead_s,
        models=models,
    )
    return builder.build(checkpointer=checkpointer)
