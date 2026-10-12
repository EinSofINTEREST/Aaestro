"""Node-level retry.

`NodeSpec.retry` 는 선언되고 **검증까지 되면서 아무도 읽지 않았다** — 그래프가
`retry: 3` 을 선언해도 조용히 무시됐다 (#191). 같은 스펙의 `timeout_s` 는
쓰이고 있었으니, 하나만 배선이 빠진 것이다.

05 Retry Layering 은 이 계층을 "node 별 `retry` 설정 시에만" 으로 규정한다 —
기본을 켜면 agentd 의 모델 재시도와 곱해진다.
"""

from __future__ import annotations

from typing import Any

import pytest

from aaestro.core.agent import TaskResult
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.orchestrator.builder import build_graph
from aaestro.orchestrator.topology import GraphTopology
from tests.fixtures.topologies import mission_dict


def topology_with(retry: int) -> GraphTopology:
    """planner 만 재시도를 선언한 토폴로지."""
    raw = mission_dict()
    raw["spec"]["nodes"][0]["retry"] = retry
    return GraphTopology.model_validate(raw)


class FlakyRuntime:
    """정해진 횟수만큼 실패한 뒤 성공하는 runtime 대역."""

    def __init__(self, failures: int, *, error: AaestroError | None = None) -> None:
        self._left = failures
        self._error = error or AaestroError(
            category=ErrorCategory.NETWORK,
            code=ErrorCode.NET_001,
            message="runtime unreachable",
            retryable=True,
        )
        self.attempts: list[str] = []

    async def invoke(self, node: Any, task: Any) -> TaskResult:
        self.attempts.append(task.task_id)
        if self._left > 0:
            self._left -= 1
            raise self._error
        return TaskResult.completed(task, output={})


@pytest.fixture
def waits() -> list[float]:
    return []


def graph_for(topology: GraphTopology, runtime: Any, waits: list[float]):
    async def sleep(delay: float) -> None:
        waits.append(delay)

    return build_graph(topology, runtime, retry_sleep=sleep)


async def test_a_declared_retry_is_honoured(waits):
    """#191 — 이 배선이 없어 `retry: 2` 선언이 조용히 무시됐다."""
    runtime = FlakyRuntime(failures=2)

    await graph_for(topology_with(2), runtime, waits).ainvoke({"query": "q"})

    # planner 3회(첫 시도 + 2회) + researcher 1회
    assert len(runtime.attempts) == 4


async def test_the_default_does_not_retry(waits):
    """기본을 켜면 모든 그래프가 agentd 재시도와의 곱을 떠안는다."""
    runtime = FlakyRuntime(failures=1)

    with pytest.raises(AaestroError):
        await graph_for(topology_with(0), runtime, waits).ainvoke({"query": "q"})

    assert len(runtime.attempts) == 1
    assert waits == []


async def test_retry_keeps_the_same_task_id(waits):
    """회차마다 새 task_id 를 발급하면 agentd 의 멱등 캐시가 걸리지 않는다."""
    runtime = FlakyRuntime(failures=2)

    await graph_for(topology_with(2), runtime, waits).ainvoke({"query": "q"})

    planner_attempts = runtime.attempts[:3]
    assert len(set(planner_attempts)) == 1


async def test_a_permanent_failure_is_not_retried(waits):
    """계약 위반을 다시 돌리는 것은 낭비다 — 같은 입력이면 같은 결과다."""
    permanent = AaestroError(
        category=ErrorCategory.GRAPH,
        code=ErrorCode.GRAPH_003,
        message="state schema mismatch",
        retryable=False,
    )
    runtime = FlakyRuntime(failures=9, error=permanent)

    with pytest.raises(AaestroError):
        await graph_for(topology_with(3), runtime, waits).ainvoke({"query": "q"})

    assert len(runtime.attempts) == 1


async def test_exhausted_retries_fail_the_run(waits):
    """무한히 재시도하면 run 이 끝나지 않는다."""
    runtime = FlakyRuntime(failures=99)

    with pytest.raises(AaestroError) as excinfo:
        await graph_for(topology_with(2), runtime, waits).ainvoke({"query": "q"})

    assert excinfo.value.code == ErrorCode.NET_001
    assert len(runtime.attempts) == 3
    # 마지막 시도 뒤에는 기다리지 않는다
    assert len(waits) == 2


async def test_only_the_declaring_node_retries(waits):
    """researcher 는 retry 를 선언하지 않았다 — 그 노드는 그대로 실패해야 한다."""

    class ResearcherFails:
        def __init__(self) -> None:
            self.attempts: list[str] = []

        async def invoke(self, node: Any, task: Any) -> TaskResult:
            self.attempts.append(node.id)
            if node.id == "researcher":
                raise AaestroError(
                    category=ErrorCategory.NETWORK,
                    code=ErrorCode.NET_001,
                    message="unreachable",
                    retryable=True,
                )
            return TaskResult.completed(task, output={})

    runtime = ResearcherFails()

    with pytest.raises(AaestroError):
        await graph_for(topology_with(3), runtime, waits).ainvoke({"query": "q"})

    assert runtime.attempts.count("researcher") == 1
