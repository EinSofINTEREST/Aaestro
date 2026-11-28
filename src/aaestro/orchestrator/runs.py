"""Submitting runs against a deployment (#244).

배포(#243)가 에이전트 주소를 알고 있으므로 run 제출자는 주소를 적지 않는다 —
`deployment_id` 만 준다. mission 은 완주를 기다리지 않고 run_id 를 즉시 돌려주며,
결과는 `GET /v1/runs/{id}` 로 본다 (긴 run 을 HTTP 로 붙잡지 않는다).
"""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.topology import GraphMode
from aaestro.orchestrator.models import with_recorded_overrides
from aaestro.orchestrator.run import RunStatus, run_refused
from aaestro.orchestrator.runstore import RunRecord, participation
from aaestro.resilience import cancel_all_and_wait
from aaestro.runtime.deployments import DeploymentStatus

if TYPE_CHECKING:
    from aaestro.catalog import Catalog
    from aaestro.core.topology import GraphTopology
    from aaestro.orchestrator.runstore import RunStore
    from aaestro.orchestrator.submit import RunResult, RunSubmitter
    from aaestro.runtime.control import ControlClient
    from aaestro.runtime.deployments import DeploymentManager, DeploymentRecord
    from aaestro.runtime.launcher import AgentLauncher
    from aaestro.runtime.nodes import SteerReceipt

log = structlog.get_logger(__name__)

DEFAULT_MAX_RESULTS = 256
"""프로세스 메모리에 쥐는 완주 결과 수 — 넘치면 오래된 것부터 버린다."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class RoutedClients(Mapping[str, "ControlClient"]):
    """`ControlNodeRuntime.clients` 자리에 들어가는, launcher 의 라우팅을 따르는 매핑.

    고정 dict 는 같은 레플리카만 계속 부른다 — `route` 는 준비된 레플리카 사이를
    round-robin 한다 (01 Scalability). 없는 에이전트는 KeyError — 노드 런타임이
    GRAPH_002 로 바꾼다.
    """

    def __init__(self, launcher: AgentLauncher) -> None:
        self._launcher = launcher

    def __getitem__(self, agent: str) -> ControlClient:
        try:
            return self._launcher.route(agent)
        except AaestroError as err:
            if err.code == ErrorCode.RT_009:
                raise KeyError(agent) from err
            raise

    def __iter__(self) -> Iterator[str]:
        return iter(dict.fromkeys(name for name, _ in self._launcher.launched))

    def __len__(self) -> int:
        return len({name for name, _ in self._launcher.launched})


@runtime_checkable
class SteerableRuntime(Protocol):
    """진행 중 run 의 태스크에 운영자 지시를 전달할 수 있는 노드 런타임 (#364)."""

    async def steer(
        self, run_id: str, text: str, *, by: str, node: str | None = None
    ) -> SteerReceipt:
        """run 이 지금 실행 중인 노드 태스크에 지시를 대기시킨다."""
        ...


def not_deployed_for_runs(deployment_id: str, *, status: str | None = None) -> AaestroError:
    return AaestroError(
        category=ErrorCategory.NOT_FOUND,
        code=ErrorCode.NF_001,
        message=("deployment is not ready to take runs" if status else "unknown deployment"),
        details={"deployment_id": deployment_id, **({"status": status} if status else {})},
    )


@dataclass
class RunService:
    """The control plane's run driver — 배포에 run 을 내고, 이어가고, 결과를 보여준다.

    mission 드라이버 태스크의 **소유자**다 (07 Async 5): fire-and-forget 이 아니라
    여기 목록에 있고, `close()` 가 정리한다.
    """

    catalog: Catalog
    deployments: DeploymentManager
    submitter: RunSubmitter
    store: RunStore
    drivers: dict[str, asyncio.Task[RunResult]] = field(default_factory=dict)
    results: OrderedDict[str, RunResult] = field(default_factory=OrderedDict)
    """완주한 mission 의 결과 — 최근 `max_results` 건만 쥔다. state 는 클 수 있고
    control plane 은 오래 산다; 전부 쥐면 메모리가 run 수만큼 자란다."""
    max_results: int = DEFAULT_MAX_RESULTS

    async def submit(
        self,
        deployment_id: str,
        initial_state: Mapping[str, Any],
        *,
        mode: str | None = None,
        run_id: str | None = None,
    ) -> RunRecord:
        deployment, topology = self._deployed(deployment_id)
        if mode is not None and GraphMode(mode) is not topology.spec.mode:
            raise AaestroError(
                category=ErrorCategory.VALIDATION,
                code=ErrorCode.VAL_002,
                message="requested mode differs from the graph's declared mode",
                details={"requested": mode, "declared": str(topology.spec.mode)},
            )
        run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        # 배포 기록에서 **복사**한다 — resume 은 배포를 다시 찾지 않고 이 값으로 잇는다
        overrides = dict(deployment.model_overrides)
        topology = with_recorded_overrides(topology, overrides)
        bound = log.bind(graph=topology.metadata.name, run_id=run_id, mode=str(topology.spec.mode))
        if topology.spec.mode is GraphMode.SERVICE:
            await self.submitter.start_service(topology, initial_state, run_id=run_id)
            self._pin_models(run_id, topology, overrides)
        else:
            # 제출 응답이 돌아간 직후의 GET 이 404 면 안 된다 — 드라이버가 기록을
            # 남기기 전이라도 **여기서** 먼저 쓴다 (read-your-writes). 기록이 이미 있으면
            # (끝난 run 포함) checkpoint thread 가 겹친다 — 없을 때만 만드는 한 연산이라
            # 다른 프로세스의 같은 id 제출과 경합해도 하나만 이긴다 (#711)
            until = self._announce(run_id, topology, model_overrides=overrides, create=True)
            self._drive(
                run_id,
                lambda: self.submitter.submit(
                    topology, initial_state, run_id=run_id, announced=True, lease_until=until
                ),
            )
        bound.info("run submitted", deployment_id=deployment.deployment_id)
        return self._record(run_id, topology)

    async def resume(self, run_id: str) -> RunRecord:
        record = self.store.get(run_id)
        if record is None:
            raise AaestroError(
                category=ErrorCategory.NOT_FOUND,
                code=ErrorCode.NF_001,
                message="unknown run",
                details={"run_id": run_id},
            )
        # 제출 때 복사해 둔 override 로 잇는다 — 배포가 사라졌어도 시작할 때의 모델이다
        topology = with_recorded_overrides(self.catalog.graph(record.graph), record.model_overrides)
        if topology.spec.mode is GraphMode.SERVICE:
            resumed = await self.submitter.resume_service(topology, run_id)
            # 재개된 run 은 새 id 다 — 그것을 다시 재개할 때도 같은 모델이어야 한다
            self._pin_models(resumed.run_id, topology, record.model_overrides)
        else:
            # 재개 시작은 임대를 가져오는 조건부 갱신이다 — 끝난 run 이거나 소유자의 임대가 만료된
            # run 만 가져오고, 동시에 재개해도 가져온 쪽만 구동한다. 같은 checkpoint thread 를
            # 둘이 이어 쓰면 뒤엉킨다 (#723)
            lease = self.submitter.manager.lease
            until = lease.until()
            if not self.store.reopen(run_id, lease.owner, until, now=lease.clock()):
                raise AaestroError(
                    category=ErrorCategory.GRAPH,
                    code=ErrorCode.GRAPH_006,
                    message="run is still running under a live lease",
                    details={"run_id": run_id, "graph": record.graph},
                )
            self.results.pop(run_id, None)
            self._drive(run_id, lambda: self.submitter.resume(topology, run_id, lease_until=until))
        log.info("run resumed", graph=record.graph, run_id=run_id, mode=record.mode)
        return self._record(run_id, topology)

    async def steer(
        self, run_id: str, text: str, *, by: str, node: str | None = None
    ) -> SteerReceipt | None:
        """Forward an operator instruction to the task a run is executing right now.

        run 이 지금 실행 중인 노드 태스크에 운영자 지시를 전달합니다 (#364). 진행 중 태스크를
        아는 것은 run 을 구동하는 노드 런타임뿐이라 그쪽에 맡깁니다.

        Returns:
            Where it went — or ``None`` when this service's node runtime cannot forward a
            steer at all. 그것을 어떻게 답할지(501)는 HTTP 표면이 정합니다.

        Raises:
            AaestroError: The runtime's or the agent's refusal (``NF_001``, ``VAL_002``).
        """
        runtime = self.submitter.runtime
        if not isinstance(runtime, SteerableRuntime):
            return None
        return await runtime.steer(run_id, text, by=by, node=node)

    def result_of(self, run_id: str) -> RunResult | None:
        """완주한 mission 의 결과 — 이 프로세스가 구동한 것만 안다."""
        return self.results.get(run_id)

    async def close(self) -> None:
        """Cancel every driver, wait for their cleanup, then stop the services.

        드라이버마다 한 번씩만 끊고 정리를 병렬로 기다린다. 바깥 취소(종료 timeout)가 오면
        드라이버에 취소를 한 번 더 넣어 정리를 끊고, 그것들이 끝난 뒤 서비스를 정지하고 취소를
        다시 올린다 (`cancel_all_and_wait`) — 그래서 취소가 전파되기까지 잘린 정리와 서비스 정지만큼
        더 걸릴 수 있다.
        """
        try:
            failures = await cancel_all_and_wait(list(self.drivers.values()))
            for task, err in failures.items():
                run_id = task.get_name()
                record = self._lookup_for_log(run_id)
                # 드라이버가 이미 ERROR 로 남긴 실패다 — 종료 경로의 반복이라 WARN
                log.warning(
                    "run driver failed while closing",
                    graph=record.graph if record is not None else None,
                    run_id=run_id,
                    error_code=(
                        err.code if isinstance(err, AaestroError) else ErrorCode.INTERNAL_001
                    ),
                    exc_info=err,
                )
        finally:
            # 아직 시작도 못 한 드라이버는 finally 를 타지 않아 스스로 빠지지 않는다
            self.drivers.clear()
            await self.submitter.stop_services()

    def _lookup_for_log(self, run_id: str) -> RunRecord | None:
        """종료 경로의 로그용 조회 — 저장소 실패가 남은 정리와 종료 결과를 바꾸지 않게 한다."""
        try:
            return self.store.get(run_id)
        except AaestroError:
            return None

    # --- 내부 --------------------------------------------------------------

    def _deployed(self, deployment_id: str) -> tuple[DeploymentRecord, GraphTopology]:
        deployment = self.deployments.get(deployment_id)
        if deployment.status != DeploymentStatus.READY:
            raise not_deployed_for_runs(deployment_id, status=str(deployment.status))
        topology = self.catalog.graph(deployment.graph)
        if topology.metadata.version != deployment.version:
            # 배포된 컨테이너는 그 버전의 선언으로 떴다 — 다른 버전으로 run 을 내면
            # 노드/에이전트 매핑이 어긋난 채 돈다
            raise AaestroError(
                category=ErrorCategory.VALIDATION,
                code=ErrorCode.VAL_002,
                message="graph version differs from the deployed one — redeploy first",
                details={
                    "graph": deployment.graph,
                    "deployed": deployment.version,
                    "current": topology.metadata.version,
                },
            )
        return deployment, topology

    def _announce(
        self,
        run_id: str,
        topology: GraphTopology,
        *,
        model_overrides: Mapping[str, Mapping[str, Any]] | None = None,
        create: bool = False,
    ) -> float:
        """run 기록을 이 프로세스의 소유 · 임대로 남기고, 쓴 임대 만료를 돌려준다."""
        # 비워 보내도 저장소가 기존 override 를 보존한다 — resume 의 재공지가 지우지 않는다
        write = self.store.create if create else self.store.upsert
        # 새 기록은 처음부터 이 프로세스의 임대를 쥔다 — 소유자 없는 running 은 만료로 보여
        # 드라이버가 뜨기 전에 다른 프로세스의 재개가 가져갈 수 있다 (#723)
        lease = self.submitter.manager.lease
        until = lease.until()
        written = write(
            RunRecord(
                run_id=run_id,
                graph=topology.metadata.name,
                mode=str(topology.spec.mode),
                status=str(RunStatus.RUNNING),
                updated_at=_now(),
                model_overrides={k: dict(v) for k, v in (model_overrides or {}).items()},
                owner=lease.owner,
                lease_until=until,
                # 저장소는 처음 기록에서만 받는다 — resume 의 재공지가 현재 그래프로 덮지 않는다
                **participation(topology),
            )
        )
        if not written:
            # 새 제출이면 같은 id 의 기록이 이미 있고, 재공지면 다른 프로세스가 소유한 run
            # 기록이다 (같은 id 의 service run) — 202 를 내지 않는다
            raise run_refused(run_id, topology.metadata.name)
        return until

    def _pin_models(
        self,
        run_id: str,
        topology: GraphTopology,
        model_overrides: Mapping[str, Mapping[str, Any]],
    ) -> None:
        """service run 기록에 override 를 남긴다 — 기록은 보통 submitter 의 슬롯이 먼저 쓴다."""
        if not model_overrides:
            return
        record = self.store.get(run_id)
        if record is None:
            # 슬롯이 다른 저장소를 보면 여기가 첫 기록이다 — 빠뜨리면 resume 이 manifest 모델로 돈다
            self._announce(run_id, topology, model_overrides=model_overrides)
            return
        pinned = self.store.upsert(
            RunRecord(
                **{
                    **record.__dict__,
                    # 이 프로세스의 이름으로 쓴다 — 기록의 소유자를 실으면 남의 기록도 통과한다
                    "owner": self.submitter.manager.lease.owner,
                    "model_overrides": {k: dict(v) for k, v in model_overrides.items()},
                }
            )
        )
        if not pinned:
            # run 은 이미 떴다 — 그 사이 소유자가 바뀐 것이라 실패시키지 않고 남긴다
            log.warning(
                "run model overrides not recorded, owner changed",
                graph=topology.metadata.name,
                run_id=run_id,
                error_code=ErrorCode.GRAPH_006,
            )

    def _record(self, run_id: str, topology: GraphTopology) -> RunRecord:
        record = self.store.get(run_id)
        if record is None:  # pragma: no cover - 방금 썼다 (service 는 submitter 가 쓴다)
            self._announce(run_id, topology)
            record = self.store.get(run_id)
        assert record is not None  # noqa: S101 — 바로 위에서 썼다
        return record

    def _remember(self, run_id: str, result: RunResult) -> None:
        self.results[run_id] = result
        self.results.move_to_end(run_id)
        while len(self.results) > self.max_results:
            self.results.popitem(last=False)

    def _fail(self, run_id: str) -> None:
        record = self.store.get(run_id)
        if record is None or record.status != str(RunStatus.RUNNING):
            return
        failed = self.store.upsert(
            RunRecord(
                **{
                    **record.__dict__,
                    "status": str(RunStatus.FAILED),
                    # 이 프로세스가 소유자일 때만 쓴다 — 기록의 소유자를 그대로 실으면 임대를
                    # 가져간 새 소유자의 run 을 failed 로 덮고, 그 끝난 기록을 또 다른 재개가
                    # 가져간다 (#723)
                    "owner": self.submitter.manager.lease.owner,
                    "updated_at": _now(),
                }
            )
        )
        if not failed:
            # 정리 경로다 — 다른 소유자의 기록이면 그쪽 상태가 맞다
            log.warning(
                "run failure not recorded, owner changed",
                graph=record.graph,
                run_id=run_id,
                error_code=ErrorCode.GRAPH_006,
            )
        self.results.pop(run_id, None)

    def _drive(self, run_id: str, driver: Callable[[], Awaitable[RunResult]]) -> None:
        """mission 드라이버를 이 서비스 소유의 태스크로 띄운다 — 결과는 끝나는 순간 기록된다.

        done-callback 은 `gather` 보다 늦게 돌 수 있어 결과가 잠시 비어 보인다 —
        결과 기록을 코루틴 안에 두면 태스크 완료가 곧 기록 완료다. 드라이버는
        **팩토리**로 받는다: 태스크가 시작 전에 취소되면 미리 만든 코루틴은 한 번도
        await 되지 않은 채 버려진다.
        """

        async def run() -> RunResult:
            try:
                result = await driver()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 — 드라이버의 예외는 여기서 끝난다 (05 Fail Gracefully)
                # 제출 시점에 쓴 running 기록을 그대로 두면 영원히 "진행 중" 으로 보인다. 상태 충돌
                # (GRAPH_006 — 임대를 잃었거나 여기서 아직 도는 run) 은 그 기록이 이 드라이버의 것이
                # 아니라는 뜻이라 건드리지 않는다
                if not (isinstance(err, AaestroError) and err.code == ErrorCode.GRAPH_006):
                    self._fail(run_id)
                log.error("run driver failed", run_id=run_id, exc_info=err)
                raise
            finally:
                self.drivers.pop(run_id, None)
            self._remember(run_id, result)
            return result

        self.drivers[run_id] = asyncio.create_task(run(), name=run_id)


__all__ = ["RoutedClients", "RunService", "SteerableRuntime"]
