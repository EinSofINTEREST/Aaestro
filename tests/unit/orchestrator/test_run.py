"""Service run loop and run slot management tests.

시간 의존 동작은 전부 주입된 fake sleep 으로 검증한다 — 실제 sleep 금지
(06-testing.md Testing Async / Concurrent Code 2).
"""

from __future__ import annotations

import asyncio

import pytest

from aaestro.core.agent import TaskRequest, TaskResult
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.topology import GraphMode, NodeSpec
from aaestro.orchestrator.builder import build_graph
from aaestro.orchestrator.run import (
    RunManager,
    RunStatus,
    ServiceRunner,
)
from tests.fixtures.topologies import make_mission, make_service


class FakeSleep:
    """대기를 기록만 하고 즉시 반환하는 sleep 대역."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


class FakeGraph:
    """iteration 마다 스크립트된 결과를 내는 그래프 대역."""

    def __init__(self, results: list[dict | Exception] | None = None) -> None:
        self._results = results or []
        self.calls: list[dict] = []
        self.configs: list[dict] = []

    async def ainvoke(self, state: dict, config: dict | None = None) -> dict:
        self.calls.append(dict(state))
        self.configs.append(config or {})

        if not self._results:
            return {**state, "iteration_count": len(self.calls)}

        result = self._results[min(len(self.calls) - 1, len(self._results) - 1)]
        if isinstance(result, Exception):
            raise result
        return {**state, **result}


def graph_error() -> AaestroError:
    """노드 실패를 흉내내는 그래프 에러."""
    return AaestroError(
        category=ErrorCategory.GRAPH, code=ErrorCode.GRAPH_002, message="node execution failed"
    )


# --- RunManager 슬롯 --------------------------------------------------------


def test_acquire_assigns_run_id_and_tracks_run():
    manager = RunManager()

    handle = manager.acquire(make_mission())

    assert handle.run_id.startswith("run-")
    assert handle.status is RunStatus.RUNNING
    assert manager.get(handle.run_id) is handle


def test_acquire_honors_explicit_run_id():
    manager = RunManager()

    handle = manager.acquire(make_mission(), run_id="run-fixed")

    assert handle.run_id == "run-fixed"


def test_mission_slots_are_bounded():
    manager = RunManager(max_concurrent_runs=2)
    topology = make_mission()
    manager.acquire(topology)
    manager.acquire(topology)

    with pytest.raises(AaestroError) as exc_info:
        manager.acquire(topology)

    assert exc_info.value.code == "GRAPH_001"
    assert exc_info.value.retryable is True
    assert "run slots exhausted" in exc_info.value.message


def test_service_slots_are_bounded_separately():
    """상주 run 이 mission 슬롯을 잠식하지 않도록 풀을 분리한다."""
    manager = RunManager(max_concurrent_runs=5, max_service_runs=1)
    manager.acquire(make_service())

    with pytest.raises(AaestroError):
        manager.acquire(make_service())

    # mission 슬롯은 여전히 비어 있다
    assert manager.acquire(make_mission()).status is RunStatus.RUNNING


def test_release_frees_the_slot():
    manager = RunManager(max_concurrent_runs=1)
    handle = manager.acquire(make_mission())

    manager.release(handle.run_id, RunStatus.COMPLETED)

    assert manager.get(handle.run_id).status is RunStatus.COMPLETED
    assert manager.acquire(make_mission()).status is RunStatus.RUNNING


def test_active_counts_only_live_runs():
    manager = RunManager()
    first = manager.acquire(make_mission())
    manager.acquire(make_mission())

    manager.release(first.run_id, RunStatus.COMPLETED)

    assert manager.active(GraphMode.MISSION) == 1
    assert manager.active(GraphMode.SERVICE) == 0


def test_unknown_run_lookup_is_rejected():
    """미존재 조회는 검증 코드가 아니라 not-found 전용 코드로 보고한다."""
    with pytest.raises(AaestroError) as exc_info:
        RunManager().get("run-missing")

    assert exc_info.value.category is ErrorCategory.NOT_FOUND
    assert exc_info.value.code == "NF_001"


def test_release_of_unknown_run_is_a_noop():
    RunManager().release("run-missing", RunStatus.COMPLETED)


# --- ServiceRunner 계약 -----------------------------------------------------


def test_service_runner_rejects_mission_graph():
    with pytest.raises(AaestroError) as exc_info:
        ServiceRunner(make_mission(), FakeGraph())

    assert exc_info.value.code == "GRAPH_001"
    assert "requires a service-mode graph" in exc_info.value.message


# --- iteration 누적 ---------------------------------------------------------


async def test_iterations_accumulate_and_checkpoint_per_iteration():
    """iteration 마다 별도 checkpoint thread 로 기록된다."""
    manager = RunManager()
    handle = manager.acquire(make_service())
    graph = FakeGraph()
    runner = ServiceRunner(make_service(), graph, sleep=FakeSleep())

    await runner.run(handle, {"feeds": ["a"]}, max_iterations=3)

    assert handle.iteration == 3
    assert len(graph.calls) == 3
    thread_ids = [c["configurable"]["thread_id"] for c in graph.configs]
    assert thread_ids == [
        f"{handle.run_id}:0",
        f"{handle.run_id}:1",
        f"{handle.run_id}:2",
    ]


async def test_state_carries_across_iterations():
    """iteration 간 state 연속성 — service 를 쓰는 이유 그 자체."""
    handle = RunManager().acquire(make_service())
    graph = FakeGraph([{"notified": 1}, {"notified": 2}, {"notified": 3}])
    runner = ServiceRunner(make_service(), graph, sleep=FakeSleep())

    await runner.run(handle, {"feeds": []}, max_iterations=3)

    assert handle.state["notified"] == 3
    assert graph.calls[2]["notified"] == 2  # 직전 iteration 결과가 입력으로 이어진다


async def test_run_id_is_injected_into_state():
    handle = RunManager().acquire(make_service(), run_id="run-svc")
    graph = FakeGraph()
    runner = ServiceRunner(make_service(), graph, sleep=FakeSleep())

    await runner.run(handle, {"feeds": []}, max_iterations=1)

    assert graph.calls[0]["_run_id"] == "run-svc"
    assert graph.calls[0]["_trace_id"] == "run-svc"


# --- idle backoff -----------------------------------------------------------


async def test_idle_backoff_progresses_to_max_and_clamps():
    """작업이 없으면 min → max 로 진행하고 상한에서 고정된다."""
    topology = make_service(
        service={"idle": {"min_delay_s": 30, "max_delay_s": 240}, "max_failure_streak": 5}
    )
    handle = RunManager().acquire(topology)
    sleep = FakeSleep()
    runner = ServiceRunner(topology, FakeGraph(), sleep=sleep)

    await runner.run(handle, {"feeds": []}, max_iterations=5, is_idle=lambda _s: True)

    assert sleep.slept == [30, 60, 120, 240, 240]


async def test_idle_backoff_resets_when_work_appears():
    """작업을 감지하면 backoff 가 min 으로 리셋된다."""
    topology = make_service(
        service={"idle": {"min_delay_s": 30, "max_delay_s": 600}, "max_failure_streak": 5}
    )
    handle = RunManager().acquire(topology)
    sleep = FakeSleep()
    graph = FakeGraph(
        [
            {"new_items": []},
            {"new_items": []},
            {"new_items": ["x"]},  # 작업 감지 → 리셋
            {"new_items": []},
        ]
    )
    runner = ServiceRunner(topology, graph, sleep=sleep)

    await runner.run(
        handle,
        {"feeds": []},
        max_iterations=4,
        is_idle=lambda state: not state.get("new_items"),
    )

    assert sleep.slept == [30, 60, 30]


async def test_busy_loop_does_not_sleep():
    """항상 작업이 있으면 backoff 하지 않는다 (idle 정책은 idle 일 때만)."""
    handle = RunManager().acquire(make_service())
    sleep = FakeSleep()
    runner = ServiceRunner(make_service(), FakeGraph(), sleep=sleep)

    await runner.run(handle, {"feeds": []}, max_iterations=3, is_idle=lambda _s: False)

    assert sleep.slept == []


async def test_idle_policy_is_skipped_without_predicate():
    handle = RunManager().acquire(make_service())
    sleep = FakeSleep()
    runner = ServiceRunner(make_service(), FakeGraph(), sleep=sleep)

    await runner.run(handle, {"feeds": []}, max_iterations=2)

    assert sleep.slept == []


# --- drain ------------------------------------------------------------------


async def test_drain_stops_after_current_iteration():
    """drain 은 즉시 중단이 아니라 진행 중 iteration 완료 후 정지다."""
    handle = RunManager().acquire(make_service())
    graph = FakeGraph()
    runner = ServiceRunner(make_service(), graph, sleep=FakeSleep())

    def drain_after_first(state: dict) -> bool:
        handle.request_drain()
        return False

    await runner.run(handle, {"feeds": []}, max_iterations=10, is_idle=drain_after_first)

    assert handle.iteration == 1
    assert handle.status is RunStatus.STOPPED


async def test_drain_marks_status_while_running():
    handle = RunManager().acquire(make_service())

    handle.request_drain()

    assert handle.drain_requested is True
    assert handle.status is RunStatus.DRAINING


async def test_run_resumes_from_next_iteration_after_drain():
    """재시작 시 다음 iteration 부터 이어서 진행한다."""
    handle = RunManager().acquire(make_service())
    graph = FakeGraph()
    runner = ServiceRunner(make_service(), graph, sleep=FakeSleep())
    await runner.run(handle, {"feeds": []}, max_iterations=2)

    resumed = ServiceRunner(make_service(), graph, sleep=FakeSleep())
    handle.status = RunStatus.RUNNING
    await resumed.run(handle, handle.state, max_iterations=4)

    assert handle.iteration == 4
    thread_ids = [c["configurable"]["thread_id"] for c in graph.configs]
    assert thread_ids[-1] == f"{handle.run_id}:3"


# --- 연속 실패 정지 ---------------------------------------------------------


async def test_failure_streak_halts_run_with_graph_005():
    topology = make_service(
        service={"idle": {"min_delay_s": 1, "max_delay_s": 2}, "max_failure_streak": 3}
    )
    handle = RunManager().acquire(topology)
    graph = FakeGraph([graph_error()] * 5)
    runner = ServiceRunner(topology, graph, sleep=FakeSleep())

    await runner.run(handle, {"feeds": []}, max_iterations=10)

    assert handle.status is RunStatus.HALTED
    assert handle.error is not None
    assert handle.error.code == "GRAPH_005"
    assert handle.failure_streak == 3
    assert len(graph.calls) == 3  # 임계 도달 즉시 정지


async def test_halt_preserves_cause_chain():
    topology = make_service(
        service={"idle": {"min_delay_s": 1, "max_delay_s": 2}, "max_failure_streak": 1}
    )
    handle = RunManager().acquire(topology)
    original = graph_error()
    runner = ServiceRunner(topology, FakeGraph([original]), sleep=FakeSleep())

    await runner.run(handle, {"feeds": []}, max_iterations=5)

    assert handle.error is not None
    assert handle.error.__cause__ is original


async def test_success_resets_failure_streak():
    """중간에 성공하면 연속 실패 카운터가 리셋되어 정지하지 않는다."""
    topology = make_service(
        service={"idle": {"min_delay_s": 1, "max_delay_s": 2}, "max_failure_streak": 3}
    )
    handle = RunManager().acquire(topology)
    graph = FakeGraph([graph_error(), graph_error(), {"notified": 1}, graph_error()])
    runner = ServiceRunner(topology, graph, sleep=FakeSleep())

    # 실패 2회 → 성공 1회(리셋) → 실패 1회. 임계(3)에 도달하지 않는다
    await runner.run(handle, {"feeds": []}, max_iterations=4)

    assert handle.status is RunStatus.STOPPED
    assert handle.failure_streak == 1


async def test_failed_iterations_count_toward_the_bound():
    """실패해도 iteration 은 진행한 것으로 센다.

    실패 시 카운터가 멈추면 영구 실패 그래프가 max_iterations 경계를 무시하고
    무한히 재시도한다 — 임계가 넉넉할수록 정지 없이 도는 회귀를 막는다.
    """
    topology = make_service(
        service={"idle": {"min_delay_s": 1, "max_delay_s": 2}, "max_failure_streak": 100}
    )
    handle = RunManager().acquire(topology)
    graph = FakeGraph([graph_error()])
    runner = ServiceRunner(topology, graph, sleep=FakeSleep())

    await runner.run(handle, {"feeds": []}, max_iterations=3)

    assert handle.iteration == 3
    assert len(graph.calls) == 3
    assert handle.status is RunStatus.STOPPED


async def test_each_failed_iteration_opens_a_new_checkpoint_thread():
    """실패 iteration 도 고유 thread 를 쓴다 — 재시도가 이전 checkpoint 를 덮지 않는다."""
    topology = make_service(
        service={"idle": {"min_delay_s": 1, "max_delay_s": 2}, "max_failure_streak": 100}
    )
    handle = RunManager().acquire(topology, run_id="run-threads")
    graph = FakeGraph([graph_error()])
    runner = ServiceRunner(topology, graph, sleep=FakeSleep())

    await runner.run(handle, {"feeds": []}, max_iterations=3)

    thread_ids = [c["configurable"]["thread_id"] for c in graph.configs]
    assert thread_ids == ["run-threads:0", "run-threads:1", "run-threads:2"]


def test_duplicate_run_id_is_rejected():
    """덮어쓰면 앞선 run 의 추적이 사라지고 슬롯 회계가 어긋난다."""
    manager = RunManager()
    manager.acquire(make_mission(), run_id="run-dup")

    with pytest.raises(AaestroError) as exc_info:
        manager.acquire(make_mission(), run_id="run-dup")

    assert exc_info.value.code == "VAL_002"
    assert len(manager.runs) == 1


def test_released_run_id_stays_reserved():
    """종료된 run 의 id 도 기록으로 남으므로 재사용하지 않는다."""
    manager = RunManager()
    handle = manager.acquire(make_mission(), run_id="run-once")
    manager.release(handle.run_id, RunStatus.COMPLETED)

    with pytest.raises(AaestroError):
        manager.acquire(make_mission(), run_id="run-once")


class BoomGraph:
    """구조화되지 않은 예외를 내는 그래프 대역."""

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, state: dict, config: dict | None = None) -> dict:
        self.calls += 1
        raise RuntimeError("unexpected")


async def test_unexpected_exception_reaches_a_terminal_state():
    """예상 못한 예외가 전파되면 handle 이 running 인 채 남아 슬롯이 샌다."""
    topology = make_service(
        service={"idle": {"min_delay_s": 1, "max_delay_s": 2}, "max_failure_streak": 2}
    )
    manager = RunManager()
    handle = manager.acquire(topology)
    runner = ServiceRunner(topology, BoomGraph(), sleep=FakeSleep())

    await runner.run(handle, {"feeds": []}, max_iterations=5)

    assert handle.status is RunStatus.HALTED
    assert handle.error is not None
    assert handle.error.code == "GRAPH_005"
    assert manager.active(GraphMode.SERVICE) == 0  # 슬롯이 반납된다


async def test_unexpected_exception_preserves_original_cause():
    topology = make_service(
        service={"idle": {"min_delay_s": 1, "max_delay_s": 2}, "max_failure_streak": 1}
    )
    handle = RunManager().acquire(topology)
    runner = ServiceRunner(topology, BoomGraph(), sleep=FakeSleep())

    await runner.run(handle, {"feeds": []}, max_iterations=3)

    assert handle.error is not None
    inner = handle.error.__cause__
    assert isinstance(inner, AaestroError)
    assert inner.code == "INTERNAL_001"
    assert isinstance(inner.__cause__, RuntimeError)


class CancelGraph:
    """취소를 일으키는 그래프 대역."""

    async def ainvoke(self, state: dict, config: dict | None = None) -> dict:
        raise asyncio.CancelledError


async def test_cancellation_propagates_and_is_not_counted_as_failure():
    """협조적 취소는 실패 스트릭이 아니라 그대로 전파돼야 한다."""
    topology = make_service(
        service={"idle": {"min_delay_s": 1, "max_delay_s": 2}, "max_failure_streak": 5}
    )
    handle = RunManager().acquire(topology)
    runner = ServiceRunner(topology, CancelGraph(), sleep=FakeSleep())

    with pytest.raises(asyncio.CancelledError):
        await runner.run(handle, {"feeds": []}, max_iterations=3)

    assert handle.failure_streak == 0
    assert handle.status is RunStatus.STOPPED


async def test_halt_records_the_failed_iteration_index():
    """실패한 회차를 가리켜야 한다 — 카운터는 실패해도 올라가므로 직전 인덱스다."""
    topology = make_service(
        service={"idle": {"min_delay_s": 1, "max_delay_s": 2}, "max_failure_streak": 1}
    )
    handle = RunManager().acquire(topology)
    runner = ServiceRunner(topology, FakeGraph([graph_error()]), sleep=FakeSleep())

    await runner.run(handle, {"feeds": []}, max_iterations=5)

    assert handle.error is not None
    assert handle.error.details["iteration"] == 0


# --- 실제 컴파일된 그래프와의 결합 ---------------------------------------------


class EchoRuntime:
    """노드를 빈 출력으로 완료시키는 runtime 대역 (``NodeRuntime`` 계약)."""

    def __init__(self) -> None:
        self.invoked: list[str] = []

    async def invoke(self, node: NodeSpec, task: TaskRequest) -> TaskResult:
        self.invoked.append(node.id)
        return TaskResult.completed(task, output={})


async def test_service_runner_iterates_a_real_compiled_graph():
    """fake graph 가 아니라 build_graph 결과로도 iteration 이 끝나야 한다.

    service 그래프가 스스로 순환하던 시절에는 ainvoke 하나가 영원히 돌아
    GraphRecursionError 까지 갔다 (#89). fake graph 만 쓰는 다른 테스트는
    ainvoke 가 즉시 반환하므로 그 회귀를 잡지 못한다.
    """
    topology = make_service(
        service={"idle": {"min_delay_s": 1, "max_delay_s": 2}, "max_failure_streak": 5}
    )
    runtime = EchoRuntime()
    runner = ServiceRunner(topology, build_graph(topology, runtime), sleep=FakeSleep())
    handle = RunManager().acquire(topology)

    await asyncio.wait_for(runner.run(handle, {"feeds": []}, max_iterations=3), timeout=10)

    assert handle.iteration == 3
    assert handle.status is RunStatus.STOPPED
    # 빈 state 로 시작하면 idle 분기를 타 watcher 만 돈다 — iteration 마다 한 번씩
    assert runtime.invoked == ["watcher", "watcher", "watcher"]
