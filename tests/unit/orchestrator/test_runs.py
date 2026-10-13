"""RunService — 배포에 run 을 내고 이어간다 (#244)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from aaestro.core.agent import TaskResult
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import ModelConfig
from aaestro.core.topology import GraphMode
from aaestro.orchestrator.checkpoint import build_checkpointer
from aaestro.orchestrator.run import RESUMED_SUFFIX, RunHandle, RunManager, RunStatus
from aaestro.orchestrator.runs import RoutedClients, RunService
from aaestro.orchestrator.runstore import InMemoryRunStore, RunRecord
from aaestro.orchestrator.submit import RunResult, RunSubmitter
from aaestro.runtime.deployments import DeploymentRecord, DeploymentStatus
from tests.fixtures.topologies import make_mission, make_service


class FakeCatalog:
    def __init__(self, graphs: dict[str, Any]) -> None:
        self._graphs = graphs

    def graph(self, name: str):
        try:
            return self._graphs[name]
        except KeyError:
            raise AaestroError(
                category="not_found", code=ErrorCode.NF_001, message=f"unknown graph: {name}"
            ) from None


class FakeDeployments:
    def __init__(self, records: dict[str, DeploymentRecord]) -> None:
        self.records = records

    def get(self, deployment_id: str) -> DeploymentRecord:
        try:
            return self.records[deployment_id]
        except KeyError:
            raise AaestroError(
                category="not_found", code=ErrorCode.NF_001, message="unknown deployment"
            ) from None


@dataclass
class FakeSubmitter:
    """제출 계약만 흉내 낸다 — 무엇이 어떤 인자로 불렸는지 기록한다."""

    store: InMemoryRunStore
    calls: list[tuple[str, str]] = field(default_factory=list)
    topologies: list[Any] = field(default_factory=list)
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    stopped: bool = False

    async def submit(self, topology, initial_state, *, run_id=None) -> RunResult:
        self.calls.append(("submit", run_id))
        self.store.upsert(
            RunRecord(run_id=run_id, graph=topology.metadata.name, mode="mission", status="running")
        )
        await self.gate.wait()
        self.store.upsert(
            RunRecord(
                run_id=run_id, graph=topology.metadata.name, mode="mission", status="completed"
            )
        )
        return RunResult(
            run_id=run_id,
            graph=topology.metadata.name,
            mode=GraphMode.MISSION,
            status=RunStatus.COMPLETED,
            state={**initial_state, "report": "done"},
        )

    async def resume(self, topology, run_id) -> RunResult:
        self.calls.append(("resume", run_id))
        return RunResult(
            run_id=run_id,
            graph=topology.metadata.name,
            mode=GraphMode.MISSION,
            status=RunStatus.COMPLETED,
            state={"resumed": True},
        )

    async def start_service(self, topology, initial_state, *, run_id=None, **_):
        self.calls.append(("start_service", run_id))
        self.topologies.append(topology)
        self.store.upsert(
            RunRecord(run_id=run_id, graph=topology.metadata.name, mode="service", status="running")
        )

    async def resume_service(self, topology, run_id, **_):
        self.calls.append(("resume_service", run_id))
        self.topologies.append(topology)
        resumed = f"{run_id}{RESUMED_SUFFIX}"
        self.store.upsert(
            RunRecord(
                run_id=resumed, graph=topology.metadata.name, mode="service", status="running"
            )
        )
        return RunHandle(run_id=resumed, graph=topology.metadata.name, mode=GraphMode.SERVICE)

    async def stop_services(self) -> None:
        self.stopped = True


def deployed(graph: str, version: str = "1.0.0", status=DeploymentStatus.READY) -> DeploymentRecord:
    return DeploymentRecord(deployment_id="dep-1", graph=graph, version=version, status=status)


@pytest.fixture
def parts():
    store = InMemoryRunStore()
    mission = make_mission()
    service = make_service()
    catalog = FakeCatalog({mission.metadata.name: mission, service.metadata.name: service})
    submitter = FakeSubmitter(store=store)
    return store, catalog, submitter, mission, service


def service_for(parts, record: DeploymentRecord) -> RunService:
    store, catalog, submitter, _, _ = parts
    return RunService(
        catalog=catalog,
        deployments=FakeDeployments({"dep-1": record}),
        submitter=submitter,
        store=store,
    )


async def test_submit_returns_before_the_mission_finishes(parts):
    """긴 run 을 HTTP 로 붙잡지 않는다 — 제출은 즉시, 결과는 GET."""
    store, _, submitter, mission, _ = parts
    runs = service_for(parts, deployed(mission.metadata.name))

    record = await runs.submit("dep-1", {"query": "q"})

    assert record.status == "running" and record.graph == mission.metadata.name
    assert runs.result_of(record.run_id) is None
    submitter.gate.set()
    await asyncio.gather(*runs.drivers.values())
    assert runs.result_of(record.run_id).state["report"] == "done"
    assert store.get(record.run_id).status == "completed"
    assert runs.drivers == {}


async def test_an_unknown_deployment_is_not_found(parts):
    runs = service_for(parts, deployed("x"))
    runs.deployments = FakeDeployments({})

    with pytest.raises(AaestroError) as exc_info:
        await runs.submit("dep-nope", {})

    assert exc_info.value.code == ErrorCode.NF_001


async def test_a_deployment_that_is_not_ready_is_not_found(parts):
    _, _, _, mission, _ = parts
    runs = service_for(parts, deployed(mission.metadata.name, status=DeploymentStatus.STOPPED))

    with pytest.raises(AaestroError) as exc_info:
        await runs.submit("dep-1", {})

    assert exc_info.value.code == ErrorCode.NF_001
    assert exc_info.value.details["status"] == "stopped"


async def test_a_graph_edited_since_the_deployment_is_refused(parts):
    """배포된 컨테이너는 그 버전의 선언으로 떴다 — 다른 버전으로 run 을 내지 않는다."""
    _, _, submitter, mission, _ = parts
    runs = service_for(parts, deployed(mission.metadata.name, version="0.9.0"))

    with pytest.raises(AaestroError) as exc_info:
        await runs.submit("dep-1", {})

    assert exc_info.value.code == ErrorCode.VAL_002
    assert submitter.calls == []


async def test_a_mode_that_contradicts_the_graph_is_refused(parts):
    _, _, submitter, mission, _ = parts
    runs = service_for(parts, deployed(mission.metadata.name))

    with pytest.raises(AaestroError) as exc_info:
        await runs.submit("dep-1", {}, mode="service")

    assert exc_info.value.code == ErrorCode.VAL_002
    assert submitter.calls == []


async def test_a_service_graph_starts_its_loop(parts):
    _, _, submitter, _, service = parts
    runs = service_for(parts, deployed(service.metadata.name))

    record = await runs.submit("dep-1", {}, run_id="svc-1")

    assert submitter.calls == [("start_service", "svc-1")]
    assert record.mode == "service" and record.status == "running"


async def test_resume_drives_the_recorded_graph(parts):
    store, _, submitter, mission, service = parts
    runs = service_for(parts, deployed(mission.metadata.name))
    store.upsert(
        RunRecord(run_id="m-1", graph=mission.metadata.name, mode="mission", status="failed")
    )
    store.upsert(
        RunRecord(run_id="s-1", graph=service.metadata.name, mode="service", status="halted")
    )

    await runs.resume("m-1")
    await runs.resume("s-1")
    await asyncio.gather(*runs.drivers.values())

    # 드라이버는 다음 await 에서 돈다 — 호출 순서가 아니라 집합을 본다
    assert sorted(submitter.calls) == [("resume", "m-1"), ("resume_service", "s-1")]
    assert runs.result_of("m-1").state == {"resumed": True}


class FailOnceRuntime:
    """researcher 가 첫 호출에서만 실패한다."""

    def __init__(self) -> None:
        self.invoked: list[str] = []

    async def invoke(self, node, task):
        self.invoked.append(node.id)
        if node.id == "researcher" and self.invoked.count("researcher") == 1:
            raise AaestroError(category=ErrorCategory.GRAPH, code="GRAPH_002", message="flaky")
        return TaskResult.completed(task, output={})


async def test_resume_through_the_control_plane_skips_completed_nodes(parts):
    """UI 의 재개 버튼이 타는 경로 — 실제 submitter 로 앞선 노드가 다시 돌지 않는지 본다 (#309)."""
    store, catalog, _, mission, _ = parts
    runtime = FailOnceRuntime()
    runs = RunService(
        catalog=catalog,
        deployments=FakeDeployments({"dep-1": deployed(mission.metadata.name)}),
        submitter=RunSubmitter(
            runtime=runtime,
            manager=RunManager(store=store),
            checkpointer=build_checkpointer("memory"),
        ),
        store=store,
    )
    await runs.submit("dep-1", {"query": "q"}, run_id="m-1")
    await asyncio.gather(*runs.drivers.values())
    assert store.get("m-1").status == "failed"

    await runs.resume("m-1")
    await asyncio.gather(*runs.drivers.values())

    assert runtime.invoked == ["planner", "researcher", "researcher"]
    assert store.get("m-1").status == "completed"
    assert runs.result_of("m-1").ok


async def test_resume_of_an_unknown_run_is_not_found(parts):
    runs = service_for(parts, deployed("x"))

    with pytest.raises(AaestroError) as exc_info:
        await runs.resume("nope")

    assert exc_info.value.code == ErrorCode.NF_001


async def test_close_cancels_drivers_and_stops_services(parts):
    _, _, submitter, mission, _ = parts
    runs = service_for(parts, deployed(mission.metadata.name))
    await runs.submit("dep-1", {})
    assert runs.drivers

    await runs.close()

    assert runs.drivers == {} and submitter.stopped


def test_routed_clients_follow_the_launcher_and_miss_cleanly():
    """없는 에이전트는 KeyError → 노드 런타임이 GRAPH_002 로 바꾼다."""
    from aaestro.runtime.docker.engine import DockerEngine
    from aaestro.runtime.launcher import AgentLauncher
    from tests.fixtures.fake_docker import FakeDockerClient

    launcher = AgentLauncher(engine=DockerEngine(client=FakeDockerClient()))
    clients = RoutedClients(launcher)

    assert clients.get("planner") is None
    assert len(clients) == 0 and list(clients) == []


async def test_the_record_is_readable_right_after_submit_without_yielding(parts):
    """202 직후의 GET 이 404 면 안 된다 — 드라이버가 아니라 제출이 기록을 쓴다."""
    store, _, _, mission, _ = parts
    runs = service_for(parts, deployed(mission.metadata.name))

    record = await runs.submit("dep-1", {}, run_id="fresh")

    assert store.get("fresh") is not None and record.status == "running"
    await runs.close()


async def test_a_driver_that_raises_leaves_a_failed_record(parts):
    """제출 시점의 running 기록을 그대로 두면 영원히 진행 중으로 보인다."""
    store, _, submitter, mission, _ = parts
    runs = service_for(parts, deployed(mission.metadata.name))

    async def broken(topology, initial_state, *, run_id=None):
        raise RuntimeError("driver exploded")

    submitter.submit = broken  # type: ignore[method-assign]
    await runs.submit("dep-1", {}, run_id="boom")
    with pytest.raises(RuntimeError):
        await asyncio.gather(*runs.drivers.values())

    assert store.get("boom").status == "failed"
    assert runs.result_of("boom") is None


async def test_results_are_bounded(parts):
    _, _, submitter, mission, _ = parts
    runs = service_for(parts, deployed(mission.metadata.name))
    runs.max_results = 2
    submitter.gate.set()
    for i in range(3):
        await runs.submit("dep-1", {}, run_id=f"r{i}")
        await asyncio.gather(*runs.drivers.values())

    assert list(runs.results) == ["r1", "r2"]
    assert runs.result_of("r0") is None


# --- 배포의 모델 override 가 run 에 이어진다 (#358) ---------------------------------

BASE_MODEL = ModelConfig(provider="anthropic", name="claude-sonnet-5")
OVERRIDES = {"researcher": {"name": "claude-haiku-4-5"}}


def base_models(node) -> ModelConfig:
    """manifest 대역 — 노드에 입혀진 override 를 그대로 올린다 (catalog 없이 배선만 본다)."""
    return BASE_MODEL.overridden(node.model)


def overridden_deployment(graph: str) -> DeploymentRecord:
    return DeploymentRecord(
        deployment_id="dep-1",
        graph=graph,
        version="1.0.0",
        status=DeploymentStatus.READY,
        model_overrides=OVERRIDES,
    )


def real_runs(parts, runtime, deployments: dict[str, DeploymentRecord]) -> RunService:
    store, catalog, _, _, _ = parts
    return RunService(
        catalog=catalog,
        deployments=FakeDeployments(deployments),
        submitter=RunSubmitter(
            runtime=runtime,
            manager=RunManager(store=store),
            checkpointer=build_checkpointer("memory"),
            models=base_models,
        ),
        store=store,
    )


class RecordingRuntime(FailOnceRuntime):
    """노드 태스크가 받은 모델을 기록한다 — researcher 는 첫 호출에서 실패한다."""

    def __init__(self) -> None:
        super().__init__()
        self.models: list[tuple[str, str | None]] = []

    async def invoke(self, node, task):
        model = task.config.model
        self.models.append((node.id, None if model is None else model.name))
        return await super().invoke(node, task)


async def test_submit_runs_nodes_with_the_deployment_overrides_and_copies_them(parts):
    store, _, _, mission, _ = parts
    runtime = RecordingRuntime()
    runs = real_runs(parts, runtime, {"dep-1": overridden_deployment(mission.metadata.name)})

    await runs.submit("dep-1", {"query": "q"}, run_id="m-1")
    await asyncio.gather(*runs.drivers.values())

    assert runtime.models == [("planner", "claude-sonnet-5"), ("researcher", "claude-haiku-4-5")]
    # 슬롯 반납 같은 핸들 갱신이 지나간 뒤에도 남아 있어야 한다
    assert store.get("m-1").model_overrides == OVERRIDES


async def test_resume_keeps_the_models_even_after_the_deployment_is_gone(parts):
    store, _, _, mission, _ = parts
    runtime = RecordingRuntime()
    deployments = {"dep-1": overridden_deployment(mission.metadata.name)}
    runs = real_runs(parts, runtime, deployments)
    await runs.submit("dep-1", {"query": "q"}, run_id="m-1")
    await asyncio.gather(*runs.drivers.values())
    assert store.get("m-1").status == "failed"
    deployments.clear()

    await runs.resume("m-1")
    await asyncio.gather(*runs.drivers.values())

    assert runtime.models[-1] == ("researcher", "claude-haiku-4-5")
    assert store.get("m-1").status == "completed"
    assert store.get("m-1").model_overrides == OVERRIDES


async def test_a_service_run_records_and_resumes_with_the_deployment_overrides(parts):
    store, _, submitter, _, service = parts
    node_id = service.spec.nodes[0].id
    record = DeploymentRecord(
        deployment_id="dep-1",
        graph=service.metadata.name,
        version=service.metadata.version,
        status=DeploymentStatus.READY,
        model_overrides={node_id: {"name": "claude-haiku-4-5"}},
    )
    runs = service_for(parts, record)

    await runs.submit("dep-1", {}, run_id="svc-1")
    store.upsert(
        RunRecord(run_id="svc-1", graph=service.metadata.name, mode="service", status="halted")
    )
    await runs.resume("svc-1")

    started, resumed = submitter.topologies
    for topology in (started, resumed):
        assert topology.node(node_id).model.name == "claude-haiku-4-5"
    assert store.get("svc-1").model_overrides == {node_id: {"name": "claude-haiku-4-5"}}
    # 재개된 run 은 새 id 다 — 그것을 다시 재개해도 같은 모델이어야 한다
    assert store.get(f"svc-1{RESUMED_SUFFIX}").model_overrides == {
        node_id: {"name": "claude-haiku-4-5"}
    }
