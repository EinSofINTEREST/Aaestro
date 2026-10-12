"""Service runs and the run store.

`ServiceRunner` 는 `store` 파라미터와 `_publish` / drain 폴링을 갖췄는데,
`RunSubmitter` 가 **그것을 넘기지 않았다** — 프로덕션에서 store 는 늘
`None` 이었다 (#197).

두 가지가 죽어 있었다: iteration 진행이 프로세스 밖에 남지 않고, 프로세스 밖
drain 요청이 전달되지 않는다.
"""

from __future__ import annotations

from typing import Any

from aaestro.core.agent import TaskResult
from aaestro.orchestrator.checkpoint import build_checkpointer
from aaestro.orchestrator.run import RunManager, RunStatus
from aaestro.orchestrator.runstore import InMemoryRunStore
from aaestro.orchestrator.submit import RunSubmitter
from tests.fixtures.topologies import make_service


class EchoRuntime:
    """노드를 빈 출력으로 완료시키는 runtime 대역."""

    async def invoke(self, node: Any, task: Any) -> TaskResult:
        return TaskResult.completed(task, output={})


async def no_sleep(_delay: float) -> None:
    """idle backoff 를 즉시 통과시킨다 — 06 은 실제 sleep 을 금지한다."""


def submitter(store: InMemoryRunStore | None = None) -> RunSubmitter:
    return RunSubmitter(
        runtime=EchoRuntime(),
        checkpointer=build_checkpointer("memory"),
        manager=RunManager(store=store),
    )


async def drive(runs: RunSubmitter, run_id: str, *, iterations: int = 3) -> Any:
    handle = await runs.start_service(
        make_service(), {}, run_id=run_id, max_iterations=iterations, sleep=no_sleep
    )
    await runs.services[handle.run_id]
    return handle


async def test_iteration_progress_reaches_the_store():
    """#197 — 저장소의 회차가 영원히 0 이면 재개가 처음부터 돈다."""
    store = InMemoryRunStore()

    handle = await drive(submitter(store), "svc-progress")

    record = store.get("svc-progress")
    assert record is not None
    assert record.iteration == handle.iteration == 3


async def test_the_stored_status_settles_when_the_run_stops():
    """running 인 채 남으면 재개가 '살아있는 run' 으로 오인해 거부된다."""
    store = InMemoryRunStore()

    await drive(submitter(store), "svc-settled", iterations=1)

    record = store.get("svc-settled")
    assert record is not None
    assert record.status == str(RunStatus.STOPPED)


async def test_a_drain_left_by_another_process_is_honoured():
    """`_drain_requested` 의 docstring 이 경고하는 상황 — store 없이는 영원히 전달되지 않는다."""
    store = InMemoryRunStore()
    runs = submitter(store)

    handle = await runs.start_service(
        make_service(), {}, run_id="svc-drain", max_iterations=50, sleep=no_sleep
    )
    # 다른 프로세스가 남긴 요청 — 러너는 iteration 경계에서 이것을 본다
    store.request_drain("svc-drain")
    await runs.services[handle.run_id]

    assert handle.iteration < 50
    assert handle.status is RunStatus.STOPPED


async def test_a_submitter_without_a_store_still_runs():
    """store 미주입 배선(테스트, in-memory dev)은 그대로 동작해야 한다."""
    handle = await drive(submitter(), "svc-nostore", iterations=2)

    assert handle.iteration == 2


async def test_a_halted_run_is_recorded_as_halted():
    """`resume_service` 는 halted 만 허용한다 — 남기지 않으면 재개할 수 없다."""
    from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

    class AlwaysFails:
        async def invoke(self, node: Any, task: Any) -> TaskResult:
            raise AaestroError(
                category=ErrorCategory.GRAPH,
                code=ErrorCode.GRAPH_002,
                message="node failed",
            )

    store = InMemoryRunStore()
    runs = RunSubmitter(
        runtime=AlwaysFails(),
        checkpointer=build_checkpointer("memory"),
        manager=RunManager(store=store),
    )

    handle = await runs.start_service(
        make_service(), {}, run_id="svc-halted", max_iterations=50, sleep=no_sleep
    )
    await runs.services[handle.run_id]

    record = store.get("svc-halted")
    assert record is not None
    assert record.status == str(RunStatus.HALTED)


async def test_the_runner_and_the_manager_share_one_store():
    """둘이 다른 저장소를 보면 한쪽의 기록이 다른 쪽에 안 보인다."""
    store = InMemoryRunStore()
    runs = submitter(store)

    await drive(runs, "svc-shared", iterations=1)

    assert runs.manager.store is store
    assert store.get("svc-shared") is not None
