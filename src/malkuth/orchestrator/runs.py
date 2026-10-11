"""Submitting runs against a deployment (#244).

배포(#243)가 에이전트 주소를 알고 있으므로 run 제출자는 주소를 적지 않는다 —
`deployment_id` 만 준다. mission 은 완주를 기다리지 않고 run_id 를 즉시 돌려주며,
결과는 `GET /v1/runs/{id}` 로 본다 (긴 run 을 HTTP 로 붙잡지 않는다).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.orchestrator.models import with_recorded_overrides
from malkuth.orchestrator.run import RunStatus
from malkuth.orchestrator.runstore import RunRecord
from malkuth.orchestrator.topology import GraphMode
from malkuth.runtime.deployments import DeploymentStatus

if TYPE_CHECKING:
    from malkuth.catalog import Catalog
    from malkuth.orchestrator.runstore import RunStore
    from malkuth.orchestrator.submit import RunResult, RunSubmitter
    from malkuth.orchestrator.topology import GraphTopology
    from malkuth.runtime.control import ControlClient
    from malkuth.runtime.deployments import DeploymentManager, DeploymentRecord
    from malkuth.runtime.launcher import AgentLauncher

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
        except MalkuthError as err:
            if err.code == ErrorCode.RT_009:
                raise KeyError(agent) from err
            raise

    def __iter__(self) -> Iterator[str]:
        return iter(dict.fromkeys(name for name, _ in self._launcher.launched))

    def __len__(self) -> int:
        return len({name for name, _ in self._launcher.launched})


def not_deployed_for_runs(deployment_id: str, *, status: str | None = None) -> MalkuthError:
    return MalkuthError(
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
            raise MalkuthError(
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
            # 남기기 전이라도 **여기서** 먼저 쓴다 (read-your-writes)
            self._announce(run_id, topology, model_overrides=overrides)
            self._drive(
                run_id, lambda: self.submitter.submit(topology, initial_state, run_id=run_id)
            )
        bound.info("run submitted", deployment_id=deployment.deployment_id)
        return self._record(run_id, topology)

    async def resume(self, run_id: str) -> RunRecord:
        record = self.store.get(run_id)
        if record is None:
            raise MalkuthError(
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
            self.results.pop(run_id, None)
            self._announce(run_id, topology)
            self._drive(run_id, lambda: self.submitter.resume(topology, run_id))
        log.info("run resumed", graph=record.graph, run_id=run_id, mode=record.mode)
        return self._record(run_id, topology)

    def result_of(self, run_id: str) -> RunResult | None:
        """완주한 mission 의 결과 — 이 프로세스가 구동한 것만 안다."""
        return self.results.get(run_id)

    async def close(self) -> None:
        for task in list(self.drivers.values()):
            task.cancel()
        for task in list(self.drivers.values()):
            # 종료 중이다 — 취소된/실패한 드라이버의 결과는 버린다
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # 아직 시작도 못 한 드라이버는 finally 를 타지 않아 스스로 빠지지 않는다
        self.drivers.clear()
        await self.submitter.stop_services()

    # --- 내부 --------------------------------------------------------------

    def _deployed(self, deployment_id: str) -> tuple[DeploymentRecord, GraphTopology]:
        deployment = self.deployments.get(deployment_id)
        if deployment.status != DeploymentStatus.READY:
            raise not_deployed_for_runs(deployment_id, status=str(deployment.status))
        topology = self.catalog.graph(deployment.graph)
        if topology.metadata.version != deployment.version:
            # 배포된 컨테이너는 그 버전의 선언으로 떴다 — 다른 버전으로 run 을 내면
            # 노드/에이전트 매핑이 어긋난 채 돈다
            raise MalkuthError(
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
    ) -> None:
        # 비워 보내도 저장소가 기존 override 를 보존한다 — resume 의 재공지가 지우지 않는다
        self.store.upsert(
            RunRecord(
                run_id=run_id,
                graph=topology.metadata.name,
                mode=str(topology.spec.mode),
                status=str(RunStatus.RUNNING),
                updated_at=_now(),
                model_overrides={k: dict(v) for k, v in (model_overrides or {}).items()},
            )
        )

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
        self.store.upsert(
            RunRecord(
                **{
                    **record.__dict__,
                    "model_overrides": {k: dict(v) for k, v in model_overrides.items()},
                }
            )
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
        self.store.upsert(
            RunRecord(**{**record.__dict__, "status": str(RunStatus.FAILED), "updated_at": _now()})
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
                # 제출 시점에 쓴 running 기록을 그대로 두면 영원히 "진행 중" 으로 보인다
                self._fail(run_id)
                log.error("run driver failed", run_id=run_id, exc_info=err)
                raise
            finally:
                self.drivers.pop(run_id, None)
            self._remember(run_id, result)
            return result

        self.drivers[run_id] = asyncio.create_task(run(), name=run_id)


__all__ = ["RoutedClients", "RunService"]
