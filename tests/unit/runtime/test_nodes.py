"""Unit tests for node execution over the Control API.

그래프는 이 계약 뒤의 컨테이너 사정을 알지 못한다 — 노드가 가리키는 대상이
없으면 진행 전에 명확히 실패해야 한다.
"""

from __future__ import annotations

import pytest

from aaestro.core.agent import TaskResult, TaskStatus
from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.core.manifest import agent_name
from aaestro.orchestrator.topology import NodeSpec
from aaestro.runtime.nodes import ControlNodeRuntime
from tests.fixtures.builders import make_task


class FakeControlClient:
    """Control API 응답을 스크립트하는 클라이언트 대역."""

    def __init__(self, output: dict | None = None) -> None:
        self._output = output or {"answer": "42"}
        self.calls: list[str] = []

    async def invoke(self, task):
        self.calls.append(task.task_id)
        return TaskResult.completed(task, output=self._output)

    async def cancel(self, task_id):
        self.calls.append(f"cancel:{task_id}")


def node(node_id: str = "planner", agent: str | None = "agents/planner@0.1.0") -> NodeSpec:
    return NodeSpec.model_validate({"id": node_id, "agent": agent})


def subgraph_node(node_id: str = "review") -> NodeSpec:
    """subgraph 노드 — agent 가 아니라 다른 그래프를 가리킨다."""
    return NodeSpec.model_validate({"id": node_id, "graph": "graphs/sub-review@1.0.0"})


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("agents/planner@0.1.0", "planner"),
        ("agents/feed-watcher@1.2.3", "feed-watcher"),
    ],
)
def test_agent_name_is_extracted_from_the_ref(ref, expected):
    assert agent_name(ref) == expected


async def test_node_is_routed_to_its_agent():
    client = FakeControlClient()
    runtime = ControlNodeRuntime(clients={"planner": client})

    result = await runtime.invoke(node(), make_task(task_id="t-1"))

    assert result.status == TaskStatus.COMPLETED
    assert client.calls == ["t-1"]


async def test_invocations_are_tracked_in_order():
    """trace 출력과 라우팅 확인에 쓰인다."""
    runtime = ControlNodeRuntime(
        clients={"planner": FakeControlClient(), "writer": FakeControlClient()}
    )

    await runtime.invoke(node("planner"), make_task())
    await runtime.invoke(node("writer", "agents/writer@0.1.0"), make_task())

    assert runtime.invoked == ["planner", "writer"]


async def test_subgraph_node_is_rejected_not_skipped():
    """조용히 건너뛰면 그래프가 실행된 것처럼 보이면서 아무 일도 하지 않는다."""
    runtime = ControlNodeRuntime(clients={})

    with pytest.raises(AaestroError) as exc_info:
        await runtime.invoke(subgraph_node(), make_task())

    assert exc_info.value.code == "GRAPH_002"
    assert exc_info.value.category is ErrorCategory.GRAPH
    assert exc_info.value.details["graph_ref"] == "graphs/sub-review@1.0.0"


async def test_missing_container_is_graph_002():
    """노드가 가리키는 에이전트가 떠 있지 않으면 그래프가 진행할 수 없다."""
    runtime = ControlNodeRuntime(clients={})

    with pytest.raises(AaestroError) as exc_info:
        await runtime.invoke(node(), make_task())

    assert exc_info.value.code == "GRAPH_002"
    assert exc_info.value.agent == "planner"


async def test_failed_node_is_not_tracked_as_invoked():
    """호출되지 못한 노드를 호출됐다고 기록하면 trace 가 거짓말한다."""
    runtime = ControlNodeRuntime(clients={})

    with pytest.raises(AaestroError):
        await runtime.invoke(node(), make_task())

    assert runtime.invoked == []


# --- 취소 (#314) ----------------------------------------------------------------


async def test_cancel_reaches_the_nodes_agent():
    client = FakeControlClient()
    runtime = ControlNodeRuntime(clients={"planner": client})

    await runtime.cancel(node(), "t-1")

    assert client.calls == ["cancel:t-1"]


@pytest.mark.parametrize("target", [subgraph_node(), node(agent="agents/gone@0.1.0")])
async def test_cancel_without_a_container_is_a_no_op(target):
    """취소할 컨테이너가 없으면 취소할 태스크도 없다."""
    client = FakeControlClient()

    await ControlNodeRuntime(clients={"planner": client}).cancel(target, "t-1")

    assert client.calls == []
