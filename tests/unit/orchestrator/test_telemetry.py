"""Orchestration metric wiring tests.

run / node / iteration / checkpoint 메트릭이 실제 실행 경로에서 집계되는지
검증한다 — 계측 클래스만 부르면 배선을 지워도 통과한다.
"""

from __future__ import annotations

import pytest
from prometheus_client import CollectorRegistry

from aaestro.core.agent import TaskResult
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.observability.metrics import Metrics
from aaestro.orchestrator.builder import build_graph
from aaestro.orchestrator.checkpoint import guarded_restore, guarded_save
from aaestro.orchestrator.run import RunManager, RunStatus, ServiceRunner
from tests.fixtures.topologies import make_mission, make_service

GRAPH = "research-pipeline"
SERVICE_GRAPH = "feed-monitor"


def make_metrics() -> Metrics:
    """이 테스트만의 registry — 다른 테스트의 카운터와 섞이지 않는다."""
    return Metrics(registry=CollectorRegistry())


def value(metrics: Metrics, name: str, **labels: str) -> float:
    """해당 라벨 조합의 현재 값 — 미기록이면 0.0."""
    return metrics.registry.get_sample_value(name, labels) or 0.0


class FakeSleep:
    """대기를 기록만 하고 즉시 반환하는 sleep 대역."""

    async def __call__(self, seconds: float) -> None:
        return None


class EchoRuntime:
    """노드를 빈 출력으로 완료시키는 runtime 대역 (``NodeRuntime`` 계약)."""

    async def invoke(self, node, task) -> TaskResult:
        return TaskResult.completed(task, output={})


class FailingRuntime:
    """항상 실패하는 runtime 대역."""

    async def invoke(self, node, task) -> TaskResult:
        raise AaestroError(
            category=ErrorCategory.GRAPH,
            code=ErrorCode.GRAPH_002,
            message="node execution failed",
        )


# --- run 슬롯 ----------------------------------------------------------------


def test_acquiring_a_run_raises_the_active_gauge():
    metrics = make_metrics()
    manager = RunManager(metrics=metrics)

    manager.acquire(make_mission())

    assert value(metrics, "aaestro_runs_active", graph=GRAPH, mode="mission") == 1.0


def test_releasing_a_run_lowers_the_gauge_and_counts_the_outcome():
    metrics = make_metrics()
    manager = RunManager(metrics=metrics)
    handle = manager.acquire(make_mission())

    manager.release(handle.run_id, RunStatus.COMPLETED)

    assert value(metrics, "aaestro_runs_active", graph=GRAPH, mode="mission") == 0.0
    assert (
        value(metrics, "aaestro_runs_total", graph=GRAPH, mode="mission", status="completed") == 1.0
    )


def test_releasing_twice_does_not_drive_the_gauge_negative():
    """이미 끝난 run 을 다시 반납해도 active 회계가 어긋나면 안 된다."""
    metrics = make_metrics()
    manager = RunManager(metrics=metrics)
    handle = manager.acquire(make_mission())

    manager.release(handle.run_id, RunStatus.COMPLETED)
    manager.release(handle.run_id, RunStatus.FAILED)

    assert value(metrics, "aaestro_runs_active", graph=GRAPH, mode="mission") == 0.0
    assert value(metrics, "aaestro_runs_total", graph=GRAPH, mode="mission", status="failed") == 0.0


def test_service_runs_are_labelled_by_their_own_mode():
    """mission 과 service 는 슬롯 풀이 다르다 — 게이지도 갈라져야 한다."""
    metrics = make_metrics()
    manager = RunManager(metrics=metrics)

    manager.acquire(make_service())

    assert value(metrics, "aaestro_runs_active", graph=SERVICE_GRAPH, mode="service") == 1.0
    assert value(metrics, "aaestro_runs_active", graph=SERVICE_GRAPH, mode="mission") == 0.0


def test_run_management_works_without_metrics():
    manager = RunManager()
    handle = manager.acquire(make_mission())

    manager.release(handle.run_id, RunStatus.COMPLETED)

    assert handle.status is RunStatus.COMPLETED


# --- node latency ------------------------------------------------------------


async def test_node_duration_is_observed_per_node():
    metrics = make_metrics()
    topology = make_mission()
    graph = build_graph(topology, EchoRuntime(), metrics=metrics)

    await graph.ainvoke({"query": "q"})

    assert (
        value(metrics, "aaestro_node_duration_seconds_count", graph=GRAPH, node_id="planner") == 1.0
    )


async def test_failed_node_is_still_timed():
    """실패한 노드가 latency 에서 빠지면 느린 실패를 관측할 수 없다."""
    metrics = make_metrics()
    topology = make_mission()
    graph = build_graph(topology, FailingRuntime(), metrics=metrics)

    with pytest.raises(AaestroError):
        await graph.ainvoke({"query": "q"})

    assert (
        value(metrics, "aaestro_node_duration_seconds_count", graph=GRAPH, node_id="planner") == 1.0
    )


# --- service iteration -------------------------------------------------------


async def test_each_iteration_is_counted():
    """ServiceRunStalled 알림이 이 카운터의 증가를 본다."""
    metrics = make_metrics()
    topology = make_service()
    runner = ServiceRunner(
        topology, build_graph(topology, EchoRuntime()), sleep=FakeSleep(), metrics=metrics
    )
    handle = RunManager().acquire(topology)

    await runner.run(handle, {"feeds": []}, max_iterations=3)

    assert (
        value(metrics, "aaestro_service_iterations_total", graph=SERVICE_GRAPH, status="completed")
        == 3.0
    )


async def test_halted_run_is_counted_under_its_own_status():
    """ServiceRunHalted 알림은 halted 라벨을 본다 — 단순 실패와 구분해야 한다."""
    metrics = make_metrics()
    topology = make_service(
        service={"idle": {"min_delay_s": 1, "max_delay_s": 2}, "max_failure_streak": 1}
    )
    runner = ServiceRunner(
        topology, build_graph(topology, FailingRuntime()), sleep=FakeSleep(), metrics=metrics
    )
    handle = RunManager().acquire(topology)

    await runner.run(handle, {"feeds": []}, max_iterations=5)

    assert handle.status is RunStatus.HALTED
    assert (
        value(metrics, "aaestro_service_iterations_total", graph=SERVICE_GRAPH, status="halted")
        == 1.0
    )


async def test_idle_backoff_is_reflected_in_the_gauge():
    metrics = make_metrics()
    topology = make_service()
    runner = ServiceRunner(
        topology, build_graph(topology, EchoRuntime()), sleep=FakeSleep(), metrics=metrics
    )
    handle = RunManager().acquire(topology)

    await runner.run(handle, {"feeds": []}, max_iterations=1, is_idle=lambda _s: True)

    assert value(metrics, "aaestro_service_idle_delay_seconds", graph=SERVICE_GRAPH) > 0.0


async def test_service_loop_works_without_metrics():
    topology = make_service()
    runner = ServiceRunner(topology, build_graph(topology, EchoRuntime()), sleep=FakeSleep())
    handle = RunManager().acquire(topology)

    await runner.run(handle, {"feeds": []}, max_iterations=2)

    assert handle.iteration == 2


# --- checkpoint --------------------------------------------------------------


async def test_successful_save_is_counted():
    metrics = make_metrics()

    await guarded_save(_ok, graph=GRAPH, run_id="run-1", metrics=metrics)

    assert (
        value(metrics, "aaestro_checkpoint_operations_total", operation="save", status="completed")
        == 1.0
    )


async def test_failed_save_is_counted():
    """실패 값은 모든 계층이 failed 로 통일한다 (#104) — 알림도 그 값을 본다."""
    metrics = make_metrics()

    with pytest.raises(AaestroError):
        await guarded_save(_boom, graph=GRAPH, run_id="run-1", metrics=metrics)

    assert (
        value(metrics, "aaestro_checkpoint_operations_total", operation="save", status="failed")
        == 1.0
    )


async def test_restore_is_counted_under_its_own_operation():
    metrics = make_metrics()

    await guarded_restore(_ok, graph=GRAPH, run_id="run-1", metrics=metrics)

    assert (
        value(metrics, "aaestro_checkpoint_operations_total", operation="load", status="completed")
        == 1.0
    )


async def test_checkpoint_guards_work_without_metrics():
    assert await guarded_save(_ok, graph=GRAPH, run_id="run-1") == "ok"


async def _ok() -> str:
    return "ok"


async def _boom() -> str:
    raise RuntimeError("disk full")


# --- 조립 지점의 주입 ------------------------------------------------------------


def test_submitter_shares_its_registry_with_the_manager():
    """슬롯 게이지만 빠지면 run 수가 영원히 0 으로 보인다."""
    from aaestro.orchestrator.submit import RunSubmitter

    metrics = make_metrics()
    submitter = RunSubmitter(runtime=EchoRuntime(), metrics=metrics)

    submitter.manager.acquire(make_mission())

    assert value(metrics, "aaestro_runs_active", graph=GRAPH, mode="mission") == 1.0


def test_an_explicitly_given_manager_keeps_its_own_registry():
    """호출자가 물린 registry 를 덮어쓰면 그쪽 집계가 사라진다."""
    from aaestro.orchestrator.submit import RunSubmitter

    theirs = make_metrics()
    ours = make_metrics()
    manager = RunManager(metrics=theirs)

    RunSubmitter(runtime=EchoRuntime(), manager=manager, metrics=ours)
    manager.acquire(make_mission())

    assert value(theirs, "aaestro_runs_active", graph=GRAPH, mode="mission") == 1.0
    assert value(ours, "aaestro_runs_active", graph=GRAPH, mode="mission") == 0.0


async def test_node_metrics_flow_through_the_submitter():
    """build_graph 에 registry 가 닿지 않으면 노드 latency 가 비어 있다."""
    from aaestro.orchestrator.submit import RunSubmitter

    metrics = make_metrics()
    submitter = RunSubmitter(runtime=EchoRuntime(), metrics=metrics)

    await submitter.submit(make_mission(), {"query": "q"})

    assert (
        value(metrics, "aaestro_node_duration_seconds_count", graph=GRAPH, node_id="planner") == 1.0
    )


# --- 에이전트 메트릭의 graph 라벨 (#113) -------------------------------------------


async def test_a_graph_run_stamps_its_name_on_the_task():
    """에이전트는 이 값을 통해서만 자기 그래프를 안다 — 가정하지 않고 전달받는다."""
    seen: list[str | None] = []

    class RecordingRuntime:
        async def invoke(self, node, task) -> TaskResult:
            seen.append(task.trace.graph)
            return TaskResult.completed(task, output={})

    graph = build_graph(make_mission(), RecordingRuntime())

    await graph.ainvoke({"query": "q"})

    assert seen
    assert set(seen) == {GRAPH}


async def test_the_agent_metric_carries_the_real_graph_name():
    """빈 문자열이면 대시보드의 그래프별 분해가 무의미해진다 (#113 완료 조건)."""
    from aaestro.agentd.executor import Executor, ExecutorServices
    from aaestro.agentd.telemetry import ExecutorTelemetry
    from tests.fixtures.fake_model import FakeModel, FakeTools, text

    metrics = make_metrics()

    class AgentdRuntime:
        """오케스트레이터가 준 태스크를 실제 executor 에 흘려보낸다."""

        async def invoke(self, node, task) -> TaskResult:
            executor = Executor(
                agent="researcher",
                model=FakeModel([text("done")]),
                tools=FakeTools(),
                render=lambda _t: "prompt",
                services=ExecutorServices(
                    telemetry=ExecutorTelemetry(metrics, agent="researcher", group="research")
                ),
            )
            return await executor.execute(task)

    graph = build_graph(make_mission(), AgentdRuntime())

    await graph.ainvoke({"query": "q"})

    assert (
        value(
            metrics,
            "aaestro_agent_tasks_total",
            agent="researcher",
            group="research",
            graph=GRAPH,
            status="completed",
        )
        > 0.0
    )


async def test_a2a_delegation_keeps_the_originating_graph():
    """위임된 태스크가 그래프를 잃으면 같은 run 의 메트릭이 둘로 갈라진다."""
    from aaestro.core.agent import TraceContext

    original = TraceContext(trace_id="trace-1", graph=GRAPH)

    delegated = original.child("span-2")

    assert delegated.graph == GRAPH
    assert delegated.depth == 1
