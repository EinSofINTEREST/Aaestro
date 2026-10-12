"""Run execution — mission runs and the service iteration loop.

Run 실행. mission 은 END 도달로 종료하고, service 는 iteration 을 무한 반복한다.

Service 루프의 시간 의존 동작(idle backoff)은 주입된 sleep/clock 을 통해서만
수행한다 — 테스트가 실제 sleep 없이 결정적으로 검증할 수 있어야 하기 때문이다.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.orchestrator.runstore import RunRecord
from aaestro.orchestrator.telemetry import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    OrchestratorTelemetry,
)
from aaestro.orchestrator.topology import GraphMode, GraphTopology

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aaestro.observability.metrics import Metrics
    from aaestro.orchestrator.runstore import RunStore

DEFAULT_MAX_CONCURRENT_RUNS = 10
DEFAULT_MAX_SERVICE_RUNS = 5

_RUN_ID_KEY = "_run_id"
_TRACE_ID_KEY = "_trace_id"


class RunStatus(StrEnum):
    """Run 진행 상태.

    ``halted`` 는 service run 이 연속 실패 임계를 넘어 정지한 상태다 (``GRAPH_005``).
    """

    RUNNING = "running"
    DRAINING = "draining"
    COMPLETED = "completed"
    FAILED = "failed"
    HALTED = "halted"
    STOPPED = "stopped"


@dataclass
class RunHandle:
    """A live run.

    실행 중인 run 의 핸들. drain 요청과 상태 조회 창구다.
    """

    run_id: str
    graph: str
    mode: GraphMode
    status: RunStatus = RunStatus.RUNNING
    iteration: int = 0
    failure_streak: int = 0
    state: dict[str, Any] = field(default_factory=dict)
    error: AaestroError | None = None

    _drain: bool = field(default=False, init=False)

    def request_drain(self) -> None:
        """Ask the run to stop after the current iteration.

        진행 중 iteration 을 마친 뒤 정지하도록 요청합니다 (즉시 중단 아님).
        """
        self._drain = True
        if self.status is RunStatus.RUNNING:
            self.status = RunStatus.DRAINING

    @property
    def drain_requested(self) -> bool:
        """Drain 이 요청되었는지."""
        return self._drain


async def _no_sleep(_seconds: float) -> None:
    """기본 sleep — 테스트는 이 자리에 fake 를 주입한다."""
    await asyncio.sleep(_seconds)


class RunManager:
    """Tracks run slots for mission and service graphs.

    mission / service run 슬롯을 분리 관리한다. Service run 은 장기 점유하므로
    mission 과 같은 풀을 쓰면 상주 그래프가 전체 슬롯을 잠식한다.
    """

    def __init__(
        self,
        *,
        max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS,
        max_service_runs: int = DEFAULT_MAX_SERVICE_RUNS,
        metrics: Metrics | None = None,
        store: RunStore | None = None,
    ) -> None:
        self._max_mission = max_concurrent_runs
        self._max_service = max_service_runs
        self._runs: dict[str, RunHandle] = {}
        self._metrics = metrics
        self._store = store

    @property
    def store(self) -> RunStore | None:
        """이 manager 가 기록을 남기는 저장소 — 미주입 시 None."""
        return self._store

    def publish(self, handle: RunHandle) -> None:
        """Make this run visible to other processes.

        run 을 프로세스 밖에서 볼 수 있게 기록합니다 — 저장소가 없으면
        무동작이라 기존 배선이 그대로 동작합니다.
        """
        if self._store is None:
            return
        self._store.upsert(
            RunRecord(
                run_id=handle.run_id,
                graph=handle.graph,
                mode=str(handle.mode),
                status=str(handle.status),
                iteration=handle.iteration,
                failure_streak=handle.failure_streak,
                drain=handle.drain_requested,
            )
        )

    def sync_drain(self, handle: RunHandle) -> bool:
        """Pick up a drain requested by another process.

        다른 프로세스가 남긴 drain 요청을 읽어 핸들에 반영합니다 — 이것이
        없으면 저장소에 요청이 쌓여도 run 은 계속 돕니다.

        Returns:
            Whether a drain is now requested.
        """
        if self._store is not None and not handle.drain_requested:
            record = self._store.get(handle.run_id)
            if record is not None and record.drain:
                handle.request_drain()
        return handle.drain_requested

    @property
    def runs(self) -> dict[str, RunHandle]:
        """추적 중인 run 목록 (run_id → handle)."""
        return dict(self._runs)

    def use_metrics(self, metrics: Metrics) -> None:
        """Attach a metric registry after construction.

        생성 뒤에 registry 를 물립니다 — 조립하는 쪽(``RunSubmitter``)이
        registry 를 알고, 기본 manager 는 그것을 모른 채 만들어지기 때문입니다.
        이미 물려 있으면 바꾸지 않습니다 (호출자 의도 존중).
        """
        if self._metrics is None:
            self._metrics = metrics

    def active(self, mode: GraphMode) -> int:
        """해당 모드로 진행 중인 run 수."""
        return sum(1 for run in self._runs.values() if run.mode is mode and self._is_live(run))

    @staticmethod
    def _is_live(run: RunHandle) -> bool:
        """슬롯을 쥐고 있는 run — 진행 중이거나 drain 중."""
        return run.status in (RunStatus.RUNNING, RunStatus.DRAINING)

    def acquire(
        self, topology: GraphTopology, *, run_id: str | None = None, resuming: bool = False
    ) -> RunHandle:
        """Reserve a run slot for a graph.

        그래프의 run 슬롯을 확보합니다.

        Args:
            topology: The graph to run.
            run_id: Optional run id; generated when omitted.
            resuming: 끝난 run 을 같은 id 로 다시 잡는다 — 재개 전용. 새 제출이 끝난 run 의
                id 를 재사용하면 옛 checkpoint 위에 새 실행이 얹히므로 제출에는 열지 않는다.

        Returns:
            The run handle.

        Raises:
            AaestroError: GRAPH/``GRAPH_001`` if the slot ceiling is reached.
        """
        mode = topology.mode
        limit = self._max_service if mode is GraphMode.SERVICE else self._max_mission

        if self.active(mode) >= limit:
            raise AaestroError(
                category=ErrorCategory.GRAPH,
                code=ErrorCode.GRAPH_001,
                message=f"{mode} run slots exhausted",
                retryable=True,
                details={"graph": topology.name, "mode": str(mode), "limit": limit},
            )

        resolved_id = run_id or f"run-{uuid.uuid4()}"
        existing = self._runs.get(resolved_id)
        if existing is not None and not (resuming and not self._is_live(existing)):
            # 덮어쓰면 앞선 run 의 추적이 사라지고 슬롯 회계가 어긋난다
            raise AaestroError(
                category=ErrorCategory.VALIDATION,
                code=ErrorCode.VAL_002,
                message=f"run id already in use: {resolved_id}",
                details={"run_id": resolved_id, "graph": topology.name},
            )

        handle = RunHandle(run_id=resolved_id, graph=topology.name, mode=mode)
        self._runs[handle.run_id] = handle
        self.publish(handle)
        telemetry = self._telemetry(handle)
        if telemetry is not None:
            telemetry.run_started()
        return handle

    def get(self, run_id: str) -> RunHandle:
        """Look up a run by id.

        run_id 로 run 을 조회합니다.

        Raises:
            AaestroError: NOT_FOUND/``VAL_001`` if the run is unknown.
        """
        run = self._runs.get(run_id) or self._restore(run_id)
        if run is None:
            raise AaestroError(
                category=ErrorCategory.NOT_FOUND,
                code=ErrorCode.NF_001,
                message=f"unknown run: {run_id}",
                details={"run_id": run_id},
            )
        return run

    def _restore(self, run_id: str) -> RunHandle | None:
        """Rebuild a handle for a run this process did not start.

        이 프로세스가 시작하지 않은 run 의 핸들을 기록에서 복원합니다 —
        `RunStore` 의 존재 이유가 "다른 프로세스가 닿는 것" 인데, 읽는 쪽이
        없으면 재시작을 넘는 재개가 성립하지 않습니다 (#186).

        복원한 핸들은 **`_runs` 에 넣지 않습니다**: `active` 가 그것을 세면
        죽은 프로세스의 run 이 동시 실행 상한을 잠식합니다.

        Returns:
            The rebuilt handle, or None when no record exists.
        """
        if self._store is None:
            return None
        record = self._store.get(run_id)
        if record is None:
            return None

        handle = RunHandle(
            run_id=record.run_id,
            graph=record.graph,
            mode=GraphMode(record.mode),
            status=RunStatus(record.status),
            iteration=record.iteration,
            failure_streak=record.failure_streak,
        )
        if record.drain:
            handle.request_drain()
        return handle

    def release(self, run_id: str, status: RunStatus) -> None:
        """Mark a run finished, freeing its slot.

        run 을 종료 상태로 표시해 슬롯을 반납합니다.
        """
        run = self._runs.get(run_id)
        if run is None:
            return
        # 이미 끝난 run 을 다시 반납하면 active 게이지가 음수로 흘러간다
        if run.status not in (RunStatus.RUNNING, RunStatus.DRAINING):
            return

        run.status = status
        self.publish(run)
        telemetry = self._telemetry(run)
        if telemetry is not None:
            telemetry.run_finished(status=status.value)

    def _telemetry(self, handle: RunHandle) -> OrchestratorTelemetry | None:
        """이 run 의 계측기 — graph/mode 라벨이 run 마다 다르므로 그때 만든다."""
        if self._metrics is None:
            return None
        return OrchestratorTelemetry(self._metrics, graph=handle.graph, mode=handle.mode)


RESUMED_SUFFIX = ":resumed"
"""재개한 service run 의 id 접미사 — 재개할 때마다 하나씩 붙는다."""


def iteration_thread(run_id: str, iteration: int) -> str:
    """service run 한 iteration 의 checkpoint thread — iteration 마다 새 thread 를 연다."""
    return f"{run_id}:{iteration}"


def run_generations(run_id: str) -> list[str]:
    """재개로 이어진 run id 들 — 최근 것부터. ``x:resumed:resumed`` → 자신, ``x:resumed``, ``x``."""
    generations = [run_id]
    while generations[-1].endswith(RESUMED_SUFFIX):
        generations.append(generations[-1].removesuffix(RESUMED_SUFFIX))
    return generations


class ServiceRunner:
    """Drives a service graph's perpetual iteration loop.

    상주 그래프의 iteration 루프를 구동한다. Iteration 마다 checkpoint 가 남으므로
    프로세스가 재시작되어도 다음 iteration 부터 이어서 진행할 수 있다.
    """

    def __init__(
        self,
        topology: GraphTopology,
        graph: Any,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        metrics: Metrics | None = None,
        store: RunStore | None = None,
    ) -> None:
        if topology.mode is not GraphMode.SERVICE:
            raise AaestroError(
                category=ErrorCategory.GRAPH,
                code=ErrorCode.GRAPH_001,
                message="ServiceRunner requires a service-mode graph",
                details={"graph": topology.name, "mode": str(topology.mode)},
            )
        if topology.spec.service is None:  # pragma: no cover - 스키마가 이미 강제
            raise AaestroError(
                category=ErrorCategory.GRAPH,
                code=ErrorCode.GRAPH_001,
                message="service graph is missing its idle policy",
                details={"graph": topology.name},
            )

        self._topology = topology
        self._service = topology.spec.service
        self._graph = graph
        self._sleep = sleep or _no_sleep
        self._store = store
        self.delays: list[float] = []
        self._telemetry = (
            None
            if metrics is None
            else OrchestratorTelemetry(metrics, graph=topology.name, mode=topology.mode)
        )

    def _drain_wanted(self, handle: RunHandle) -> bool:
        """이번 iteration 을 더 돌지 — 다른 프로세스의 요청도 함께 본다.

        저장소를 읽지 않으면 프로세스 밖에서 남긴 drain 요청이 영원히
        전달되지 않는다 (#102).
        """
        if self._store is not None and not handle.drain_requested:
            record = self._store.get(handle.run_id)
            if record is not None and record.drain:
                handle.request_drain()
        return handle.drain_requested

    def _publish(self, handle: RunHandle) -> None:
        """iteration 진행을 프로세스 밖에서 볼 수 있게 남긴다."""
        if self._store is None:
            return
        self._store.upsert(
            RunRecord(
                run_id=handle.run_id,
                graph=handle.graph,
                mode=str(handle.mode),
                status=str(handle.status),
                iteration=handle.iteration,
                failure_streak=handle.failure_streak,
                drain=handle.drain_requested,
            )
        )

    def _record_iteration(self, status: str) -> None:
        """iteration 결과를 메트릭에 남긴다 — 메트릭 미주입 시 무동작."""
        if self._telemetry is not None:
            self._telemetry.iteration_finished(status=status)

    async def run(
        self,
        handle: RunHandle,
        initial_state: dict[str, Any],
        *,
        max_iterations: int | None = None,
        is_idle: Callable[[dict[str, Any]], bool] | None = None,
    ) -> RunHandle:
        """Run the service loop until drained, halted, or bounded.

        Service 루프를 실행합니다. 정지 조건은 drain 요청, 연속 실패 임계 초과
        (``GRAPH_005``), 또는 테스트용 ``max_iterations`` 입니다.

        Args:
            handle: The run handle to advance.
            initial_state: State the first iteration starts from.
            max_iterations: Optional bound so tests can end the loop.
            is_idle: Predicate deciding whether an iteration found no work.

        Returns:
            The same handle, with terminal status recorded.
        """
        state = dict(initial_state)
        state.setdefault(_RUN_ID_KEY, handle.run_id)
        state.setdefault(_TRACE_ID_KEY, handle.run_id)
        idle_streak = 0

        while not self._drain_wanted(handle):
            if max_iterations is not None and handle.iteration >= max_iterations:
                break

            try:
                state = await self._iterate(handle, state)
            except asyncio.CancelledError:
                # 협조적 취소는 그대로 전파한다 (drain/shutdown 경로)
                handle.status = RunStatus.STOPPED
                self._publish(handle)
                raise
            except AaestroError as err:
                if self._record_failure(handle, err):
                    return handle
                continue
            except Exception as err:
                # 예상 못한 예외가 전파되면 handle 이 running 인 채 남아 슬롯이 샌다
                if self._record_failure(handle, self._wrap_unexpected(err, handle)):
                    return handle
                continue

            handle.failure_streak = 0
            self._record_iteration(STATUS_COMPLETED)
            self._publish(handle)
            idle_streak = await self._apply_idle_policy(state, idle_streak, is_idle)

        handle.status = RunStatus.STOPPED
        handle.state = state
        # 최종 상태를 남기지 않으면 저장소에 running 인 채로 남아, 재개가
        # "살아있는 run" 으로 오인해 거부한다
        self._publish(handle)
        return handle

    async def _iterate(self, handle: RunHandle, state: dict[str, Any]) -> dict[str, Any]:
        """단일 iteration 실행 — iteration 단위 checkpoint 를 남긴다.

        실패하든 성공하든 iteration 은 진행한 것으로 센다 — 그래야 실패가 반복돼도
        ``max_iterations`` 경계가 유효하고, checkpoint thread 도 매번 새로 열린다.
        """
        config = {"configurable": {"thread_id": iteration_thread(handle.run_id, handle.iteration)}}
        try:
            result = await self._graph.ainvoke(state, config)
        finally:
            handle.iteration += 1
        handle.state = dict(result)
        return dict(result)

    @staticmethod
    def _wrap_unexpected(err: Exception, handle: RunHandle) -> AaestroError:
        """구조화되지 않은 예외를 INTERNAL 로 변환한다."""
        wrapped = AaestroError(
            category=ErrorCategory.INTERNAL,
            code=ErrorCode.INTERNAL_001,
            message="unexpected error during service iteration",
            details={"graph": handle.graph, "run_id": handle.run_id},
        )
        wrapped.__cause__ = err
        return wrapped

    def _record_failure(self, handle: RunHandle, err: AaestroError) -> bool:
        """실패를 누적하고, 임계 초과 시 run 을 halted 로 정지한다."""
        handle.failure_streak += 1
        if handle.failure_streak < self._service.max_failure_streak:
            self._record_iteration(STATUS_FAILED)
            return False

        # ServiceRunHalted 알림이 이 라벨을 본다 — 단순 실패와 구분해야 한다
        self._record_iteration(RunStatus.HALTED.value)
        handle.status = RunStatus.HALTED
        handle.error = AaestroError(
            category=ErrorCategory.GRAPH,
            code=ErrorCode.GRAPH_005,
            message="service iteration failure streak exceeded",
            details={
                "graph": handle.graph,
                "run_id": handle.run_id,
                # _iterate 는 실패해도 카운터를 올리므로, 실패한 회차는 직전 인덱스다
                "iteration": max(handle.iteration - 1, 0),
                "max_failure_streak": self._service.max_failure_streak,
            },
        )
        handle.error.__cause__ = err
        # halted 를 남겨야 재개가 가능하다 — resume_service 는 halted 만 허용한다
        self._publish(handle)
        return True

    async def _apply_idle_policy(
        self,
        state: dict[str, Any],
        idle_streak: int,
        is_idle: Callable[[dict[str, Any]], bool] | None,
    ) -> int:
        """작업이 없으면 backoff 하고, 작업을 찾으면 backoff 를 리셋한다."""
        if is_idle is None or not is_idle(state):
            return 0

        delay = self._service.idle.delay_for(idle_streak)
        self.delays.append(delay)
        if self._telemetry is not None:
            self._telemetry.idle_delay(delay)
        await self._sleep(delay)
        return idle_streak + 1
