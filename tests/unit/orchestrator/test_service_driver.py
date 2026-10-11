"""Service run driving path tests.

시간 의존 동작은 주입된 fake sleep 으로 검증한다 — 실제 sleep 금지 (06 규칙).
구동 태스크는 소유자가 관리해야 한다 — fire-and-forget 은 금지다 (07 Async 5).
"""

from __future__ import annotations

import asyncio

import pytest
from langgraph.checkpoint.memory import MemorySaver

from malkuth.core.agent import TaskResult
from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.core.manifest import ModelConfig
from malkuth.orchestrator.run import RunStatus
from malkuth.orchestrator.submit import RunSubmitter
from malkuth.orchestrator.topology import GraphMode
from tests.fixtures.topologies import make_mission, make_service


async def no_sleep(seconds: float) -> None:
    """대기를 건너뛴다 — 실제 backoff 를 기다리면 테스트가 느려진다."""
    return


class EchoRuntime:
    """노드를 빈 출력으로 완료시키는 runtime 대역 (``NodeRuntime`` 계약)."""

    def __init__(self) -> None:
        self.invoked: list[str] = []

    async def invoke(self, node, task) -> TaskResult:
        self.invoked.append(node.id)
        return TaskResult.completed(task, output={})


class FailingRuntime:
    """항상 실패하는 runtime 대역."""

    async def invoke(self, node, task) -> TaskResult:
        raise MalkuthError(
            category=ErrorCategory.GRAPH,
            code=ErrorCode.GRAPH_002,
            message="node execution failed",
        )


def submitter(runtime=None, *, checkpointer=None) -> RunSubmitter:
    return RunSubmitter(runtime=runtime or EchoRuntime(), checkpointer=checkpointer)


# --- 시작 --------------------------------------------------------------------


async def test_service_run_iterates_and_stops_at_the_bound():
    """상주 그래프를 구동할 경로가 없으면 ServiceRunner 는 죽은 코드다."""
    runtime = EchoRuntime()
    sub = submitter(runtime)
    topology = make_service()

    handle = await sub.start_service(topology, {"feeds": []}, max_iterations=3, sleep=no_sleep)
    await asyncio.wait_for(sub.services[handle.run_id], timeout=10)

    assert handle.iteration == 3
    assert handle.status is RunStatus.STOPPED
    assert runtime.invoked == ["watcher", "watcher", "watcher"]


async def test_every_iteration_task_carries_the_resolved_model():
    """service 경로의 build_graph 가 resolver 를 빠뜨리면 상주 run 만 manifest 모델로 돈다."""

    class ModelRuntime(EchoRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.models: list[str | None] = []

        async def invoke(self, node, task) -> TaskResult:
            self.models.append(None if task.config.model is None else task.config.model.name)
            return await super().invoke(node, task)

    runtime = ModelRuntime()
    sub = RunSubmitter(
        runtime=runtime,
        models=lambda node: ModelConfig(provider="anthropic", name=f"for-{node.id}"),
    )

    handle = await sub.start_service(
        make_service(), {"feeds": []}, max_iterations=2, sleep=no_sleep
    )
    await asyncio.wait_for(sub.services[handle.run_id], timeout=10)

    assert runtime.models == ["for-watcher", "for-watcher"]


async def test_mission_graph_is_rejected_by_the_service_path():
    sub = submitter()

    with pytest.raises(MalkuthError) as exc_info:
        await sub.start_service(make_mission(), {"query": "q"})

    assert exc_info.value.code == ErrorCode.GRAPH_001


async def test_invalid_state_is_rejected_before_a_slot_is_taken():
    """슬롯을 잡고 나서 실패하면 상주 슬롯이 낭비된다."""
    sub = submitter()
    topology = make_service()

    with pytest.raises(MalkuthError):
        await sub.start_service(topology, {"feeds": "not-a-list"})

    assert sub.manager.active(GraphMode.SERVICE) == 0
    assert sub.services == {}


# --- 태스크 소유 -------------------------------------------------------------


async def test_the_driving_task_is_owned_not_fire_and_forget():
    """소유자가 없으면 취소·정리 경로가 사라진다 (07 Async 5)."""
    sub = submitter()

    handle = await sub.start_service(
        make_service(), {"feeds": []}, max_iterations=1, sleep=no_sleep
    )

    assert handle.run_id in sub.services
    await asyncio.wait_for(sub.services[handle.run_id], timeout=10)


async def test_finished_run_releases_its_slot_and_task():
    sub = submitter()
    topology = make_service()

    handle = await sub.start_service(topology, {"feeds": []}, max_iterations=1, sleep=no_sleep)
    await asyncio.wait_for(sub.services[handle.run_id], timeout=10)

    assert sub.services == {}
    assert sub.manager.active(GraphMode.SERVICE) == 0


async def test_mission_and_service_slots_are_counted_apart():
    """상주 run 이 mission 풀을 잠식하면 안 된다 (max_service_runs)."""
    sub = submitter()

    handle = await sub.start_service(
        make_service(), {"feeds": []}, max_iterations=5, sleep=no_sleep
    )

    assert sub.manager.active(GraphMode.SERVICE) == 1
    assert sub.manager.active(GraphMode.MISSION) == 0

    handle.request_drain()
    await asyncio.wait_for(sub.services[handle.run_id], timeout=10)


# --- drain -------------------------------------------------------------------


async def test_drain_stops_after_the_current_iteration():
    """즉시 취소가 아니다 — 반쯤 진행된 iteration 이 남으면 안 된다."""
    sub = submitter()

    handle = await sub.start_service(make_service(), {"feeds": []}, sleep=no_sleep)
    stopped = await asyncio.wait_for(sub.drain_service(handle.run_id), timeout=10)

    assert stopped.status is RunStatus.STOPPED
    assert sub.services == {}


async def test_draining_an_unknown_run_is_rejected():
    sub = submitter()

    with pytest.raises(MalkuthError) as exc_info:
        await sub.drain_service("absent")

    assert exc_info.value.code == ErrorCode.NF_001


async def test_stop_services_drains_every_running_loop():
    """종료 경로가 태스크를 남기면 프로세스가 매달린다."""
    sub = submitter()

    await sub.start_service(make_service(), {"feeds": []}, run_id="svc-a", sleep=no_sleep)
    await sub.start_service(make_service(), {"feeds": []}, run_id="svc-b", sleep=no_sleep)

    await asyncio.wait_for(sub.stop_services(), timeout=10)

    assert sub.services == {}
    assert sub.manager.active(GraphMode.SERVICE) == 0


# --- resume ------------------------------------------------------------------


async def test_halted_run_resumes_from_the_next_iteration():
    """GRAPH_005 로 정지한 run 을 이어갈 수 없으면 운영자가 손쓸 방법이 없다."""
    topology = make_service(
        service={"idle": {"min_delay_s": 1, "max_delay_s": 2}, "max_failure_streak": 1}
    )
    checkpointer = MemorySaver()
    sub = submitter(FailingRuntime(), checkpointer=checkpointer)

    halted = await sub.start_service(
        topology, {"feeds": []}, run_id="svc-1", max_iterations=5, sleep=no_sleep
    )
    await asyncio.wait_for(sub.services[halted.run_id], timeout=10)
    assert halted.status is RunStatus.HALTED

    sub.runtime = EchoRuntime()
    # max_iterations 는 절대 회차다 — 재개분 한 회를 더 돌리려면 +1
    resumed = await sub.resume_service(
        topology, halted.run_id, max_iterations=halted.iteration + 1, sleep=no_sleep
    )
    await asyncio.wait_for(sub.services[resumed.run_id], timeout=10)

    # 실패한 회차를 다시 돌리지 않는다 — 부수효과가 겹친다.
    # 시작 회차가 이어지지 않으면 이전 checkpoint thread 를 덮어쓴다
    assert resumed.iteration == halted.iteration + 1
    assert resumed.status is RunStatus.STOPPED

    # 재개분이 연 checkpoint thread 는 실패했던 회차 **다음** 이어야 한다
    threads = {
        item.config["configurable"]["thread_id"]
        for item in checkpointer.list(None)
        if item.config.get("configurable", {}).get("thread_id", "").startswith("svc-1:resumed")
    }
    assert threads == {f"svc-1:resumed:{halted.iteration}"}


async def test_resume_without_a_checkpointer_is_rejected():
    """이어갈 지점 없이 재개하면 처음부터 다시 돌아 부수효과가 두 번 일어난다."""
    sub = submitter()
    topology = make_service()
    handle = await sub.start_service(topology, {"feeds": []}, max_iterations=1, sleep=no_sleep)
    await asyncio.wait_for(sub.services[handle.run_id], timeout=10)

    with pytest.raises(MalkuthError) as exc_info:
        await sub.resume_service(topology, handle.run_id)

    assert exc_info.value.code == ErrorCode.STOR_002


async def test_resuming_a_cleanly_stopped_run_is_rejected():
    """정상 정지한 run 은 재개가 아니라 새로 시작해야 한다 — 놀라운 재시작 방지."""
    sub = submitter(checkpointer=MemorySaver())
    topology = make_service()
    handle = await sub.start_service(topology, {"feeds": []}, max_iterations=1, sleep=no_sleep)
    await asyncio.wait_for(sub.services[handle.run_id], timeout=10)
    assert handle.status is RunStatus.STOPPED

    with pytest.raises(MalkuthError) as exc_info:
        await sub.resume_service(topology, handle.run_id)

    assert exc_info.value.code == ErrorCode.GRAPH_006


async def test_resuming_a_live_run_is_rejected():
    """살아있는 run 을 재개하면 같은 iteration 을 두 벌이 돌린다."""
    sub = submitter(checkpointer=MemorySaver())
    topology = make_service()
    handle = await sub.start_service(topology, {"feeds": []}, sleep=no_sleep)

    with pytest.raises(MalkuthError) as exc_info:
        await sub.resume_service(topology, handle.run_id)

    assert exc_info.value.code == ErrorCode.GRAPH_006

    handle.request_drain()
    await asyncio.wait_for(sub.services[handle.run_id], timeout=10)
