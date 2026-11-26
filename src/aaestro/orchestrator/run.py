"""Run execution — mission runs and the service iteration loop.

Run 실행. mission 은 END 도달로 종료하고, service 는 iteration 을 무한 반복한다.

Service 루프의 시간 의존 동작(idle backoff)은 주입된 sleep/clock 을 통해서만
수행한다 — 테스트가 실제 sleep 없이 결정적으로 검증할 수 있어야 하기 때문이다.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.topology import GraphMode, GraphTopology
from aaestro.orchestrator.runstore import RunRecord, participation
from aaestro.orchestrator.telemetry import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    OrchestratorTelemetry,
)

if TYPE_CHECKING:
    from aaestro.observability.metrics import Metrics
    from aaestro.orchestrator.compactor import StateCompactor
    from aaestro.orchestrator.runstore import RunStore

DEFAULT_MAX_CONCURRENT_RUNS = 10
DEFAULT_MAX_SERVICE_RUNS = 5
DEFAULT_RUN_LEASE_S = 30.0
"""service run 소유자의 임대 기간 (#662). 갱신은 이 1/3 마다 별도 태스크가 한다 — iteration ·
idle backoff 길이와 무관하다. 소유자가 죽으면 재개는 최대 이만큼 기다린다 (409)."""

log = structlog.get_logger(__name__)

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
    lease_lost: bool = False
    """임대를 잃어 루프가 스스로 멈췄는가 — 그 뒤의 기록은 새 소유자의 것이다 (#662)."""
    lease_until: float = 0.0
    """이 프로세스가 저장소에 **실제로 쓴** 임대 만료 시각 — 갱신이 실패하는 동안의 정지 시각을
    여기서 잰다 (`hold_lease`). service run 만 쓴다."""

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


def _new_owner() -> str:
    return f"owner-{uuid.uuid4().hex}"


@dataclass(frozen=True)
class RunLease:
    """Who drives service runs from this process, and for how long a silence means death.

    service run 소유권의 임대 (#662). run 저장소는 control plane 과 CLI 가 같이 쓰고 orchestrator
    인스턴스는 여럿일 수 있어, "이 프로세스가 모른다" 로는 죽은 run 과 다른 프로세스가 구동 중인
    run 을 가르지 못한다. 그래서 소유자가 기록의 임대 (``lease_until``) 를 계속 미루고, 임대가
    지난 ``running`` run 만 죽은 것으로 본다.

    두 실행이 겹치지 않는 것은 **시간 여유** 로 보장한다 — checkpoint 쓰기에는 소유자 확인
    (fencing) 이 없다. 소유자는 갱신이 실패하는 동안 자기가 저장소에 쓴 만료 시각에서 ``margin_s``
    (갱신 주기) 를 뺀 시각에 스스로 멈추고 진행 중 회차를 취소한다. 다른 프로세스는 만료 시각이
    지나야 가져간다. 그래서 전제는 **저장소를 공유하는 호스트들의 시계 차이 < margin_s** 이고, 그
    밖에서는 같은 회차가 두 번 돌 수 있다. 이벤트 루프가 margin 보다 오래 막힌 소유자도 늦게
    멈춘다 — 막힌 동안은 checkpoint 도 쓰지 못하지만, 풀린 직후의 쓰기 하나는 막지 못한다.

    시각은 ``clock`` 의 epoch 초다. ``sleep`` 은 갱신 간격에만 쓴다 (idle backoff 의 sleep 과
    별개 — 테스트가 그것을 즉시 통과시켜도 갱신 루프가 돌지 않게).
    """

    owner: str = field(default_factory=_new_owner)
    duration_s: float = DEFAULT_RUN_LEASE_S
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def until(self) -> float:
        """지금 잡거나 미루면 임대가 끝나는 시각."""
        return self.clock() + self.duration_s

    @property
    def margin_s(self) -> float:
        """갱신 주기이자 정지 여유 — 소유자는 만료 이만큼 전에 멈춘다."""
        return self.duration_s / 3


def run_refused(run_id: str, graph: str) -> AaestroError:
    """다른 소유자가 쥔 run 기록 — 저장소가 이 프로세스의 기록을 받지 않았다 (#662)."""
    return AaestroError(
        category=ErrorCategory.GRAPH,
        code=ErrorCode.GRAPH_006,
        message="run record is owned by another process",
        details={"run_id": run_id, "graph": graph},
    )


def _mark_lost(handle: RunHandle) -> None:
    if not handle.lease_lost:
        handle.lease_lost = True
        log.error(
            "service run lease lost, stopping",
            graph=handle.graph,
            run_id=handle.run_id,
            iteration=handle.iteration,
            error_code=ErrorCode.GRAPH_006,
        )


async def hold_lease(
    store: RunStore, lease: RunLease, handle: RunHandle, on_lost: Callable[[], object]
) -> None:
    """Keep renewing a run's lease until cancelled; call ``on_lost`` once it is gone.

    곧바로 한 번, 그 뒤 ``margin_s`` 마다 임대를 미룬다. 저장소가 소유자가 아니라고 답하면
    (남이 가져갔다), 또는 갱신이 실패한 채 ``handle.lease_until − margin_s`` 에 닿으면 (곧 남이
    가져갈 수 있다) 잃은 것으로 보고 멈춘다. 재개 준비 (`resume_service`) 와 루프 (`ServiceRunner`)
    가 같이 쓴다.
    """
    while True:
        now = lease.clock()
        try:
            renewed: bool | None = store.renew(handle.run_id, lease.owner, now + lease.duration_s)
        except AaestroError as err:
            # 일시 장애일 수 있다 — 정지 시각 전까지는 다음 주기에 다시 미룬다
            log.warning(
                "service run lease renewal failed",
                graph=handle.graph,
                run_id=handle.run_id,
                iteration=handle.iteration,
                error_code=err.code,
            )
            renewed = None
        if renewed:
            handle.lease_until = now + lease.duration_s
        elif renewed is False or now >= handle.lease_until - lease.margin_s:
            _mark_lost(handle)
            on_lost()
            return
        await lease.sleep(lease.margin_s)


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
        lease: RunLease | None = None,
    ) -> None:
        self._max_mission = max_concurrent_runs
        self._max_service = max_service_runs
        self._runs: dict[str, RunHandle] = {}
        self._metrics = metrics
        self._store = store
        self.lease = lease or RunLease()
        """이 manager 가 service run 을 구동할 때의 소유자 · 임대."""

    @property
    def store(self) -> RunStore | None:
        """이 manager 가 기록을 남기는 저장소 — 미주입 시 None."""
        return self._store

    def publish(self, handle: RunHandle, *, topology: GraphTopology | None = None) -> bool:
        """Make this run visible to other processes.

        run 을 프로세스 밖에서 볼 수 있게 기록합니다 — 저장소가 없으면
        무동작이라 기존 배선이 그대로 동작합니다. ``topology`` 를 주면 run 참여 스냅샷
        (run space · 노드 에이전트) 을 함께 남깁니다 — 저장소는 처음 기록에서만 받습니다.

        Returns:
            False when the store refused it — the record belongs to another owner.
        """
        if self._store is None:
            return True
        if handle.lease_lost:
            return False  # 반납 기록도 쓰지 않는다 — `ServiceRunner._publish` 와 같다
        # mission 재개는 임대를 보지 않는다 — 소유자를 두면 다른 프로세스의 공지가 막힌다
        service = handle.mode is GraphMode.SERVICE
        if service and not handle.lease_until:
            handle.lease_until = self.lease.until()
        return self._store.upsert(
            RunRecord(
                run_id=handle.run_id,
                graph=handle.graph,
                mode=str(handle.mode),
                status=str(handle.status),
                iteration=handle.iteration,
                failure_streak=handle.failure_streak,
                drain=handle.drain_requested,
                owner=self.lease.owner if service else "",
                lease_until=handle.lease_until if service else 0.0,
                **(participation(topology) if topology is not None else {}),
            )
        )

    def claim(self, run_id: str) -> float | None:
        """Take over a service run whose owner's lease expired.

        소유자의 임대가 만료된 ``running`` run 을 이 manager 의 소유로 가져옵니다 — 저장소의
        조건부 갱신이라 다른 프로세스와 동시에 시도해도 하나만 이깁니다 (#662). 저장소가 없으면
        다른 소유자를 알 길이 없으므로 가져오지 않습니다.

        Returns:
            The lease expiry written to the store, or None when the run was not taken.
        """
        if self._store is None:
            return None
        until = self.lease.until()
        taken = self._store.claim(run_id, self.lease.owner, until, now=self.lease.clock())
        return until if taken else None

    def let_go(self, run_id: str) -> None:
        """가져온 run 의 임대를 놓는다 — 재개가 실패하면 다음 재개가 임대 기간을 기다리지 않게."""
        if self._store is not None:
            self._store.release(run_id, self.lease.owner)

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
        self,
        topology: GraphTopology,
        *,
        run_id: str | None = None,
        resuming: bool = False,
        lease_until: float = 0.0,
    ) -> RunHandle:
        """Reserve a run slot for a graph.

        그래프의 run 슬롯을 확보합니다.

        Args:
            topology: The graph to run.
            run_id: Optional run id; generated when omitted.
            resuming: 끝난 run 을 같은 id 로 다시 잡는다 — 재개 전용. 새 제출이 끝난 run 의
                id 를 재사용하면 옛 checkpoint 위에 새 실행이 얹히므로 제출에는 열지 않는다.
            lease_until: 이미 저장소에 쓴 임대 만료 (``claim``) — 재개 전용. 없으면 기록이 쓴다.

        Returns:
            The run handle.

        Raises:
            AaestroError: GRAPH/``GRAPH_001`` if the slot ceiling is reached.
            AaestroError: GRAPH/``GRAPH_006`` if another owner holds the run record.
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

        handle = RunHandle(
            run_id=resolved_id, graph=topology.name, mode=mode, lease_until=lease_until
        )
        if not self.publish(handle, topology=topology):
            # 202 를 내고 첫 갱신에서야 임대를 잃은 것으로 드러나지 않게 지금 거절한다
            raise run_refused(resolved_id, topology.name)
        self._runs[handle.run_id] = handle
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
        # 거절은 임대를 잃은 뒤의 반납뿐이다 — 기록은 새 소유자의 것이라 쓰지 않는 것이 맞다
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
        compactor: StateCompactor | None = None,
        lease: RunLease | None = None,
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
        # 저장소가 없으면 다른 프로세스가 볼 기록도 없다 — 임대를 들 곳이 없다
        self._lease = lease if store is not None else None
        # 선언이 없는 그래프는 None 이다 — 그 그래프의 루프는 압축 경로에 들어가지 않는다
        self._compactor = compactor
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
            if self._lost_to(record):
                # 다음 iteration 을 열지 않는다 — 같은 회차를 새 소유자와 두 벌 돌게 된다
                _mark_lost(handle)
        return handle.drain_requested or handle.lease_lost

    def _lost_to(self, record: RunRecord | None) -> bool:
        """기록의 소유자가 이제 다른 프로세스인가."""
        return (
            self._lease is not None
            and record is not None
            and record.owner not in ("", self._lease.owner)
        )

    def _publish(self, handle: RunHandle) -> None:
        """iteration 진행을 프로세스 밖에서 볼 수 있게 남긴다."""
        if self._store is None or handle.lease_lost:
            # 임대를 잃은 뒤의 기록 (정지 · 진행) 은 쓰지 않는다 — 기록은 running 으로 남아
            # 다음 소유자가 가져가고, 이미 가져갔다면 그쪽의 것이다
            return
        written = self._store.upsert(
            RunRecord(
                run_id=handle.run_id,
                graph=handle.graph,
                mode=str(handle.mode),
                status=str(handle.status),
                iteration=handle.iteration,
                failure_streak=handle.failure_streak,
                drain=handle.drain_requested,
                owner=self._lease.owner if self._lease is not None else "",
            )
        )
        if not written:
            # 저장소가 거절했다 — 기록의 소유자가 바뀌었다. 다음 경계에서 루프를 멈춘다
            _mark_lost(handle)

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
        pending_boundary: int | None = None,
    ) -> RunHandle:
        """Run the service loop until drained, halted, or bounded.

        Service 루프를 실행합니다. 정지 조건은 drain 요청, 연속 실패 임계 초과
        (``GRAPH_005``), 또는 테스트용 ``max_iterations`` 입니다.

        Args:
            handle: The run handle to advance.
            initial_state: State the first iteration starts from.
            max_iterations: Optional bound so tests can end the loop.
            is_idle: Predicate deciding whether an iteration found no work.
            pending_boundary: The iteration whose finished state ``initial_state`` is,
                when its compaction has not been carried forward yet — 재개가 끝난
                iteration 의 checkpoint 에서 이어갈 때 같은 경계를 다시 지나게 한다.

        Returns:
            The same handle, with terminal status recorded.
        """
        loop = asyncio.current_task()
        if self._lease is None or self._store is None or loop is None:
            return await self._loop(
                handle, initial_state, max_iterations, is_idle, pending_boundary
            )
        stopped_by_lease = False

        def stop() -> None:
            nonlocal stopped_by_lease
            stopped_by_lease = True
            loop.cancel()

        holder = asyncio.create_task(
            hold_lease(self._store, self._lease, handle, stop),
            name=f"run-lease:{handle.run_id}",
        )
        try:
            return await self._loop(
                handle, initial_state, max_iterations, is_idle, pending_boundary
            )
        except asyncio.CancelledError:
            # 임대 갱신이 건 취소만 삼킨다 — 바깥의 취소 (drain · 종료) 가 겹쳤으면 그대로 올린다
            if not stopped_by_lease or loop.cancelling() > 1:
                raise
            loop.uncancel()
            return handle
        finally:
            holder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await holder

    async def _loop(
        self,
        handle: RunHandle,
        initial_state: dict[str, Any],
        max_iterations: int | None,
        is_idle: Callable[[dict[str, Any]], bool] | None,
        pending_boundary: int | None,
    ) -> RunHandle:
        """`run` 의 iteration 루프 — 임대 갱신과 분리해 그 취소를 한 곳에서 가른다."""
        state = dict(initial_state)
        state.setdefault(_RUN_ID_KEY, handle.run_id)
        state.setdefault(_TRACE_ID_KEY, handle.run_id)
        idle_streak = 0
        # 방금 끝난 iteration 의 인덱스 — 그 state 로 다음 입력을 만들 때가 압축의 경계다.
        # 실패한 iteration 뒤에는 None 이다: state 가 그대로라 다시 지날 경계가 없다
        boundary = pending_boundary

        while not self._drain_wanted(handle):
            if max_iterations is not None and handle.iteration >= max_iterations:
                break

            try:
                if boundary is not None and self._compactor is not None:
                    compacted = await self._compact(state, boundary)
                    if self._drain_wanted(handle):
                        # 요약을 기다리는 사이 drain 이 들어왔다 — iteration 을 하나 더 돌리지
                        # 않는다. 압축 결과는 버린다: 재개가 같은 경계를 다시 지난다
                        break
                    state = compacted
                boundary = None
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

            boundary = handle.iteration - 1  # _iterate 가 이미 카운터를 올렸다
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

    async def _compact(self, state: dict[str, Any], iteration: int) -> dict[str, Any]:
        """Build the next iteration's input from the finished iteration's state.

        끝난 iteration 의 state 를 압축해 다음 iteration 의 입력을 만든다. 끝난 iteration 의
        checkpoint 는 고치지 않는다 — 압축된 state 는 다음 iteration 의 checkpoint 들이
        거기서 시작하는 것으로 남는다. 그래서 재개가 끝난 iteration 의 checkpoint 를 읽으면
        이 경계를 다시 지나고, 원문 artifact 는 같은 key 를 덮어쓴다.
        """
        from aaestro.orchestrator.compaction_boundary import Boundary

        assert self._compactor is not None  # noqa: S101 — 호출자가 확인했다
        updates = await self._compactor.compact(state, Boundary.after_iteration(state, iteration))
        return {**state, **updates} if updates else state

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
