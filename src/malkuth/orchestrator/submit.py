"""Run submission.

그래프 하나를 실제로 굴리는 경로. 토폴로지 · 노드 런타임 · checkpointer 를 묶어
mission run 을 완주시키고, service run 은 iteration 을 이어간다.

Run 슬롯은 ``RunManager`` 가 관리하며, **슬롯 반납을 finally 로 보장한다** —
예외 경로에서 놓치면 슬롯이 영원히 점유된 채 남는다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.orchestrator.builder import build_graph
from malkuth.orchestrator.run import (
    RESUMED_SUFFIX,
    RunHandle,
    RunManager,
    RunStatus,
    ServiceRunner,
    iteration_thread,
    run_generations,
)
from malkuth.orchestrator.state import resolve_graph_state, validate_state
from malkuth.orchestrator.topology import GraphMode

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from malkuth.observability.metrics import Metrics
    from malkuth.orchestrator.builder import ModelResolver, NodeRuntime
    from malkuth.orchestrator.topology import GraphTopology

DEFAULT_NODE_TIMEOUT_S = 300.0

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RunResult:
    """The outcome of one submitted run.

    제출된 run 하나의 결과.
    """

    run_id: str
    graph: str
    mode: GraphMode
    status: RunStatus
    state: dict[str, Any] = field(default_factory=dict)
    error: MalkuthError | None = None

    @property
    def ok(self) -> bool:
        """완료 상태인지."""
        return self.status is RunStatus.COMPLETED


def _thread_config(run_id: str) -> RunnableConfig:
    """checkpointer 의 thread 를 run 에 고정한다 — 재개가 같은 흐름을 잇도록."""
    return {"configurable": {"thread_id": run_id}}


def seed_state(
    initial_state: Mapping[str, Any], *, run_id: str, trace_id: str | None = None
) -> dict[str, Any]:
    """Inject the reserved run channels into the initial state.

    예약 채널(``_run_id`` / ``_trace_id``)을 초기 state 에 주입합니다.

    이걸 빠뜨리면 각 노드 태스크가 ``run-unknown`` 으로 실행되어, 슬롯이 쥔
    실제 run_id 와 로그·trace 가 어긋납니다 — run_id 하나로 전 계층 로그를
    잇는다는 05 의 추적 전제가 깨집니다.

    Args:
        initial_state: The caller's starting state.
        run_id: The run this graph execution belongs to.
        trace_id: Distributed trace id; defaults to the run id.

    Returns:
        The state with reserved channels populated.
    """
    return {**dict(initial_state), "_run_id": run_id, "_trace_id": trace_id or run_id}


@dataclass
class RunSubmitter:
    """Submits graph runs and tracks their slots.

    그래프 run 을 제출하고 슬롯을 추적한다.

    Attributes:
        runtime: Invokes each node's agent.
        manager: Owns run slots — mission/service 를 분리 관리한다.
        checkpointer: Persists state so a run can resume.
    """

    runtime: NodeRuntime
    manager: RunManager = field(default_factory=RunManager)
    checkpointer: BaseCheckpointSaver[Any] | None = None
    node_timeout_s: float = DEFAULT_NODE_TIMEOUT_S
    metrics: Metrics | None = None
    """계측 registry — 여기서 넘기지 않으면 각 계층의 집계 로직이 무동작이다."""
    models: ModelResolver | None = None
    """노드별 해석된 모델을 태스크에 싣는다 — 없으면 agentd 가 manifest 기본값을 쓴다."""

    def __post_init__(self) -> None:
        # 기본 manager 는 registry 를 모른 채 만들어진다 — 슬롯 게이지만 빠지면
        # run 수가 영원히 0 으로 보이므로 같은 registry 를 물려준다
        if self.metrics is not None:
            self.manager.use_metrics(self.metrics)

    # 구동 중인 service 루프 — fire-and-forget 을 막기 위해 소유자를 명시한다 (07 Async 5)
    services: dict[str, asyncio.Task[RunHandle]] = field(default_factory=dict)

    async def submit(
        self,
        topology: GraphTopology,
        initial_state: Mapping[str, Any],
        *,
        run_id: str | None = None,
    ) -> RunResult:
        """Run a mission graph to completion.

        mission 그래프를 완주시킵니다.

        Args:
            topology: The validated graph topology.
            initial_state: The starting state.
            run_id: Explicit run id; generated when omitted.

        Returns:
            The run result — ``ok`` is False when the run failed.

        Raises:
            MalkuthError: GRAPH/``GRAPH_001`` when a service graph is submitted
                here — 상주 그래프는 완주 개념이 없으므로 ``resume`` 로 이어갑니다.
        """
        # mode 검사가 먼저다 — service 그래프에 state 문제까지 겹치면
        # GRAPH_003 이 앞서 나와 운영자가 진짜 원인(mode 위반)을 놓친다
        if topology.spec.mode is not GraphMode.MISSION:
            raise MalkuthError(
                category=ErrorCategory.GRAPH,
                code=ErrorCode.GRAPH_001,
                message="only mission graphs can be submitted for completion",
                details={"graph": topology.metadata.name, "mode": str(topology.spec.mode)},
            )

        self._reject_invalid_state(topology, initial_state)

        handle = self.manager.acquire(topology, run_id=run_id or f"run-{uuid4().hex[:12]}")
        return await self._drive(topology, handle, initial_state)

    def _reject_invalid_state(
        self, topology: GraphTopology, initial_state: Mapping[str, Any]
    ) -> None:
        """Check the initial state before a slot is taken.

        슬롯을 잡기 전에 초기 state 를 검증합니다.

        검증 없이 제출하면 필수 필드 누락이 **첫 노드 실행에서야** 드러납니다 —
        그때는 이미 슬롯을 점유하고 컨테이너를 호출한 뒤입니다. 여기서 막으면
        어느 필드가 왜 부족한지 즉시 알 수 있습니다.

        검증만 하고 **원본은 그대로 둡니다**: ``validate_state`` 의 결과에는
        예약 채널(``_run_id``/``_trace_id``)이 빠져 있어, 그걸 쓰면 추적 정보가
        사라집니다.

        호출 순서상 **mode 검사 이후**입니다 — service 그래프에 state 문제가
        겹치면 진짜 원인(mode 위반)이 가려집니다.

        Raises:
            MalkuthError: GRAPH/``GRAPH_003`` if the state fails the schema.
        """
        # resolve_graph_state 는 실패 시 예외를 던지고 성공 시 항상 모델을
        # 준다 — None 분기를 두면 "schema 가 없을 수도 있다" 는 잘못된 신호가 된다
        schema = resolve_graph_state(topology.spec.state, graph=topology.metadata.name)
        validate_state(schema, dict(initial_state))

    async def resume(self, topology: GraphTopology, run_id: str) -> RunResult:
        """Continue a run from its last checkpoint.

        마지막 checkpoint 에서 run 을 이어갑니다. 완료된 노드는 다시 돌지 않고 실패한
        노드부터 이어갑니다 — 그래프에 **입력 없이**(``None``) 들어가야 LangGraph 가 중단
        지점에서 잇습니다. 입력을 넘기면 채널은 남지만 START 부터 새 실행이 시작돼, 앞선
        노드의 부수효과가 두 번 일어납니다 (#309).

        Raises:
            MalkuthError: STORAGE/``STOR_002`` when no checkpointer is attached or
                the run left no checkpoint to continue from.
            MalkuthError: GRAPH/``GRAPH_006`` if the run is still running (HTTP 409).
        """
        if self.checkpointer is None:
            raise MalkuthError(
                category=ErrorCategory.STORAGE,
                code=ErrorCode.STOR_002,
                message="cannot resume without a checkpointer",
                details={"run_id": run_id, "graph": topology.metadata.name},
            )
        live = self.manager.runs.get(run_id)
        if live is not None and live.status in (RunStatus.RUNNING, RunStatus.DRAINING):
            # 같은 thread 를 두 실행이 이어 쓰면 checkpoint 가 뒤엉킨다
            raise MalkuthError(
                category=ErrorCategory.GRAPH,
                code=ErrorCode.GRAPH_006,
                message="run is still running",
                details={"run_id": run_id, "status": str(live.status)},
            )

        if await self.checkpointer.aget_tuple(_thread_config(run_id)) is None:
            # 이어갈 지점이 없는데 입력 없이 들어가면 LangGraph 는 아무것도 하지 않고 끝난다 —
            # "재개 성공" 으로 보이는 빈 결과를 내느니 거부한다
            raise MalkuthError(
                category=ErrorCategory.STORAGE,
                code=ErrorCode.STOR_002,
                message="run has no checkpoint to resume from",
                details={"run_id": run_id, "graph": topology.metadata.name},
            )

        # 이 프로세스가 끝까지 지켜본 run(실패한 run 을 UI 에서 재개)도 이어갈 수 있어야 한다 —
        # 재시작한 프로세스만 재개할 수 있으면 재개 버튼이 같은 프로세스에서 409 로 막힌다
        handle = self.manager.acquire(topology, run_id=run_id, resuming=True)
        return await self._drive(topology, handle, None)

    async def start_service(
        self,
        topology: GraphTopology,
        initial_state: Mapping[str, Any],
        *,
        run_id: str | None = None,
        max_iterations: int | None = None,
        is_idle: Callable[[dict[str, Any]], bool] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        start_iteration: int = 0,
    ) -> RunHandle:
        """Start a service graph's perpetual loop.

        상주 그래프의 iteration 루프를 시작하고 즉시 핸들을 돌려줍니다 —
        완주 개념이 없으므로 결과를 기다리지 않습니다. 정지는 ``drain_service``
        로 요청합니다.

        Args:
            topology: The validated service topology.
            initial_state: The starting state.
            run_id: Explicit run id; generated when omitted.
            max_iterations: Bound for tests; unbounded when omitted.
            is_idle: Decides whether an iteration found no work.
            sleep: Injected sleep for idle backoff — 테스트가 실제로 대기하지
                않게 합니다.
            start_iteration: Iteration counter to continue from — resume 경로가
                이전 회차를 이어받습니다.

        Returns:
            The run handle — drain 요청과 상태 조회 창구입니다.

        Raises:
            MalkuthError: GRAPH/``GRAPH_001`` if the graph is not a service graph.
        """
        # mode 검사가 먼저다 — state 문제까지 겹치면 GRAPH_003 이 앞서 나와
        # 운영자가 진짜 원인(mode 위반)을 놓친다
        if topology.spec.mode is not GraphMode.SERVICE:
            raise MalkuthError(
                category=ErrorCategory.GRAPH,
                code=ErrorCode.GRAPH_001,
                message="only service graphs run as a perpetual loop",
                details={"graph": topology.metadata.name, "mode": str(topology.spec.mode)},
            )

        self._reject_invalid_state(topology, initial_state)

        handle = self.manager.acquire(topology, run_id=run_id or f"svc-{uuid4().hex[:12]}")
        # 태스크가 돌기 전에 세워야 한다 — checkpoint thread_id 와 max_iterations
        # 판정이 둘 다 이 값을 본다
        handle.iteration = start_iteration
        runner = ServiceRunner(
            topology,
            build_graph(
                topology,
                self.runtime,
                checkpointer=self.checkpointer,
                node_timeout_s=self.node_timeout_s,
                metrics=self.metrics,
                models=self.models,
            ),
            sleep=sleep,
            metrics=self.metrics,
            # 매니저와 **같은** 저장소를 봐야 한다: 러너가 iteration 진행을
            # 남기지 않으면 저장소의 회차는 영원히 0 이고, 프로세스 밖에서
            # 남긴 drain 요청도 전달되지 않는다 (#197)
            store=self.manager.store,
        )
        task = asyncio.create_task(
            runner.run(
                handle,
                seed_state(initial_state, run_id=handle.run_id),
                max_iterations=max_iterations,
                is_idle=is_idle,
            ),
            name=f"service-run:{handle.run_id}",
        )
        self.services[handle.run_id] = task
        # 루프가 어떻게 끝나든 슬롯이 반납되어야 한다 — 예외로 끝나도 마찬가지다
        task.add_done_callback(lambda _t: self._release_service(handle))

        log.info("service run started", graph=topology.metadata.name, run_id=handle.run_id)
        return handle

    def _release_service(self, handle: RunHandle) -> None:
        """구동이 끝난 service run 의 슬롯과 태스크 참조를 정리한다."""
        self.services.pop(handle.run_id, None)
        # 루프가 스스로 halted/stopped 를 기록했으면 그 상태를 존중한다
        outcome = handle.status if handle.status is not RunStatus.RUNNING else RunStatus.STOPPED
        self.manager.release(handle.run_id, outcome)

    async def resume_service(
        self,
        topology: GraphTopology,
        run_id: str,
        *,
        max_iterations: int | None = None,
        is_idle: Callable[[dict[str, Any]], bool] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> RunHandle:
        """Restart a halted service run from its last iteration.

        ``GRAPH_005`` 로 정지한 상주 run 을 마지막 iteration 다음부터 재개합니다
        (05 Incident Response). 원인이 해소되지 않았다면 다시 정지하지만,
        그때도 실패 회차는 checkpoint 로 남습니다.

        Args:
            topology: The service topology this run belongs to.
            run_id: The halted run to resume.
            max_iterations: Bound for tests; unbounded when omitted.
            is_idle: Decides whether an iteration found no work.
            sleep: Injected sleep for idle backoff.

        Returns:
            The handle of the resumed run.

        Raises:
            MalkuthError: STORAGE/``STOR_002`` without a checkpointer —
                이어갈 지점이 없는데 재개하면 처음부터 다시 돌아 부수효과가
                두 번 일어납니다.
            MalkuthError: NOT_FOUND/``NF_001`` if the run is unknown.
            MalkuthError: GRAPH/``GRAPH_006`` if the run is not halted — 살아있는
                run 도, drain 으로 정상 정지한 run 도 재개 대상이 아닙니다 (HTTP 409).
        """
        if self.checkpointer is None:
            raise MalkuthError(
                category=ErrorCategory.STORAGE,
                code=ErrorCode.STOR_002,
                message="cannot resume without a checkpointer",
                details={"run_id": run_id, "graph": topology.metadata.name},
            )

        previous = self.manager.get(run_id)
        if previous.status is not RunStatus.HALTED:
            # 살아있는 run 은 같은 iteration 을 두 벌이 돌게 되고, drain 으로
            # 정상 정지한 run 은 재개가 아니라 새로 시작해야 한다 — 둘 다
            # 놀라운 재시작이므로 halted 만 허용한다
            raise MalkuthError(
                category=ErrorCategory.GRAPH,
                code=ErrorCode.GRAPH_006,
                message="only a halted run can be resumed",
                details={"run_id": run_id, "status": str(previous.status)},
            )

        resumed = await self.start_service(
            topology,
            # checkpoint 가 정본이다 — 이 프로세스가 시작하지 않은 run 은 핸들에 state 가 없고
            # (기록만 복원된다), 빈 state 로 시작하면 누적이 사라지거나 필수 필드가 없어
            # GRAPH_003 으로 거부된다 (#310)
            await self._iteration_state(topology, previous),
            run_id=f"{run_id}{RESUMED_SUFFIX}",
            max_iterations=max_iterations,
            is_idle=is_idle,
            sleep=sleep,
            # 재개는 다음 회차부터다 — 실패한 회차를 다시 돌리면 부수효과가 겹친다
            start_iteration=previous.iteration,
        )
        log.info(
            "service run resumed",
            graph=topology.metadata.name,
            run_id=resumed.run_id,
            iteration=previous.iteration,
        )
        return resumed

    async def _iteration_state(
        self, topology: GraphTopology, previous: RunHandle
    ) -> dict[str, Any]:
        """The state the next iteration starts from, read back from the checkpointer.

        다음 iteration 이 받을 state 를 checkpoint 에서 읽습니다 — 살아 있는 루프가 넘겼을 값과
        같아야 합니다. 가장 최근 iteration 이

        - 끝까지 갔으면 그 **출력**
        - 실패했으면 그 **입력** — 루프는 실패한 회차의 부분 결과를 버리고 같은 state 로 잇는다

        Raises:
            MalkuthError: STORAGE/``STOR_002`` if no iteration left a checkpoint.
        """
        graph = build_graph(
            topology, self.runtime, checkpointer=self.checkpointer, models=self.models
        )
        for iteration in range(previous.iteration - 1, -1, -1):
            for generation in run_generations(previous.run_id):
                config = _thread_config(iteration_thread(generation, iteration))
                snapshot = await graph.aget_state(config)
                if not snapshot.values:
                    continue  # 이 세대가 이 회차를 돌지 않았다
                if not snapshot.next:
                    return dict(snapshot.values)
                return await _iteration_input(graph, config)
        raise MalkuthError(
            category=ErrorCategory.STORAGE,
            code=ErrorCode.STOR_002,
            message="service run has no iteration checkpoint to resume from",
            details={"run_id": previous.run_id, "graph": topology.metadata.name},
        )

    async def drain_service(self, run_id: str, *, timeout_s: float | None = None) -> RunHandle:
        """Ask a service run to stop after its current iteration.

        진행 중 iteration 을 마친 뒤 정지하도록 요청하고 완료를 기다립니다 —
        즉시 취소가 아니므로 반쯤 진행된 iteration 이 남지 않습니다.

        Args:
            run_id: The service run to drain.
            timeout_s: Wait budget; unbounded when omitted.

        Returns:
            The stopped run handle.

        Raises:
            MalkuthError: NOT_FOUND/``NF_001`` if the run is unknown.
            TimeoutError: If the iteration does not finish within the budget.
        """
        handle = self.manager.get(run_id)
        handle.request_drain()

        task = self.services.get(run_id)
        if task is None:
            return handle

        if timeout_s is None:
            await task
        else:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
        return handle

    async def stop_services(self) -> None:
        """구동 중인 service run 을 전부 drain 한다 — 종료 경로에서 태스크를 남기지 않는다."""
        for run_id in list(self.services):
            await self.drain_service(run_id)

    async def _drive(
        self,
        topology: GraphTopology,
        handle: RunHandle,
        initial_state: Mapping[str, Any] | None,
    ) -> RunResult:
        """그래프를 실행하고 슬롯을 반드시 반납한다.

        ``initial_state`` 가 None 이면 새 실행이 아니라 checkpoint 에서 잇는다.
        """
        graph = build_graph(
            topology,
            self.runtime,
            checkpointer=self.checkpointer,
            node_timeout_s=self.node_timeout_s,
            metrics=self.metrics,
            models=self.models,
        )
        bound = log.bind(graph=topology.metadata.name, run_id=handle.run_id)
        outcome = RunStatus.FAILED

        try:
            payload = (
                None if initial_state is None else seed_state(initial_state, run_id=handle.run_id)
            )
            final = await graph.ainvoke(payload, config=_thread_config(handle.run_id))
        except MalkuthError as err:
            bound.error("graph run failed", error_code=err.code, mode=str(topology.spec.mode))
            return RunResult(
                run_id=handle.run_id,
                graph=topology.metadata.name,
                mode=topology.spec.mode,
                status=RunStatus.FAILED,
                error=err,
            )
        else:
            outcome = RunStatus.COMPLETED
            bound.info("graph run completed", mode=str(topology.spec.mode))
            return RunResult(
                run_id=handle.run_id,
                graph=topology.metadata.name,
                mode=topology.spec.mode,
                status=RunStatus.COMPLETED,
                state=dict(final),
            )
        finally:
            # 슬롯 반납을 finally 로 보장한다 — 예외 경로에서 놓치면 슬롯이
            # 영원히 점유된 채 남는다. handle.status 는 RUNNING 그대로이므로
            # 실제 결과(outcome)로 반납해야 상태가 거짓말하지 않는다
            self.manager.release(handle.run_id, outcome)


async def _iteration_input(graph: Any, config: RunnableConfig) -> dict[str, Any]:
    """실패한 iteration 이 받았던 state — step 0 checkpoint 가 입력이 반영된 첫 지점이다."""
    started: dict[str, Any] = {}
    async for snapshot in graph.aget_state_history(config):
        if snapshot.metadata and snapshot.metadata.get("step") == 0:
            started = dict(snapshot.values)
    return started


__all__ = ["DEFAULT_NODE_TIMEOUT_S", "RunResult", "RunSubmitter", "seed_state"]
