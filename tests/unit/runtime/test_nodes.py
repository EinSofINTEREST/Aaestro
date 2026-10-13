"""Unit tests for node execution over the Control API.

그래프는 이 계약 뒤의 컨테이너 사정을 알지 못한다 — 노드가 가리키는 대상이
없으면 진행 전에 명확히 실패해야 한다.
"""

from __future__ import annotations

import asyncio

import pytest
import structlog

from aaestro.core.agent import TaskResult, TaskStatus
from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.core.manifest import agent_name
from aaestro.core.topology import NodeSpec
from aaestro.runtime import nodes as nodes_module
from aaestro.runtime.nodes import ControlNodeRuntime
from tests.fixtures.builders import make_task


@pytest.fixture(autouse=True)
def fresh_logger(monkeypatch):
    """모듈 로거를 테스트마다 새로 물린다.

    structlog 는 설정에 따라 로거를 첫 사용 때 굳힌다 (`cache_logger_on_first_use`). 앞선
    테스트가 이 모듈의 로거를 굳혀 두면 `capture_logs` 가 보지 못한다 — 실행 순서에 기대지 않는다.
    """
    monkeypatch.setattr(nodes_module, "log", structlog.get_logger("test"))


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


# --- steer (#364) --------------------------------------------------------------


class RunningClient:
    """태스크를 붙잡고 있는 클라이언트 대역 — 진행 중인 동안 steer 를 받는다."""

    def __init__(self, *, supported: bool = True) -> None:
        self._supported = supported
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.steers: list[tuple[str, str, str]] = []
        self.failure: Exception | None = None

    async def invoke(self, task):
        self.started.set()
        await self.release.wait()
        if self.failure is not None:
            raise self.failure
        return TaskResult.completed(task, output={})

    async def steer(self, task_id, text, *, by):
        self.steers.append((task_id, text, by))
        return self._supported


class Running:
    """진행 중인 노드 태스크 하나 — 테스트가 끝내 줄 때까지 붙잡힌다."""

    def __init__(self, runtime, client, node_id: str, task_id: str, run_id: str = "run-1") -> None:
        self.client = client
        self._call = asyncio.create_task(
            runtime.invoke(
                node(node_id, f"agents/{node_id}@0.1.0"),
                make_task(task_id=task_id, run_id=run_id, node_id=node_id),
            )
        )

    async def finish(self):
        self.client.release.set()
        return await self._call


async def start(runtime, node_id: str, task_id: str, run_id: str = "run-1") -> Running:
    client = runtime.clients[node_id]
    running = Running(runtime, client, node_id, task_id, run_id)
    await client.started.wait()
    return running


async def test_steer_without_a_node_goes_to_the_one_running_now():
    runtime = ControlNodeRuntime(clients={"planner": RunningClient(), "writer": RunningClient()})
    running = await start(runtime, "planner", "t-1")

    receipt = await runtime.steer("run-1", "look at 2024", by="alice")
    await running.finish()

    assert runtime.clients["planner"].steers == [("t-1", "look at 2024", "alice")]
    assert runtime.clients["writer"].steers == []
    assert (receipt.node_id, receipt.task_id, receipt.agent, receipt.supported) == (
        "planner",
        "t-1",
        "planner",
        True,
    )


async def test_steer_reaches_the_replica_that_took_the_task():
    """이름으로 다시 라우팅하면 round-robin 이 다른 레플리카를 고른다."""
    replicas = [RunningClient(), RunningClient()]

    class RoundRobin(dict):
        def get(self, _name, _default=None):
            replicas.append(replicas.pop(0))
            return replicas[-1]

    took_the_task, other = replicas
    runtime = ControlNodeRuntime(clients=RoundRobin())
    call = asyncio.create_task(runtime.invoke(node(), make_task(task_id="t-1", run_id="run-1")))
    await took_the_task.started.wait()

    await runtime.steer("run-1", "go", by="alice")
    took_the_task.release.set()
    await call

    assert took_the_task.steers == [("t-1", "go", "alice")]
    assert other.steers == []


async def test_steer_with_several_nodes_running_needs_a_name():
    """아무 노드나 고르면 엉뚱한 에이전트의 방향이 바뀐다."""
    runtime = ControlNodeRuntime(clients={"planner": RunningClient(), "writer": RunningClient()})
    planner = await start(runtime, "planner", "t-1")
    writer = await start(runtime, "writer", "t-2")

    with pytest.raises(AaestroError) as exc_info:
        await runtime.steer("run-1", "go", by="alice")
    await runtime.steer("run-1", "go", by="alice", node="writer")
    await planner.finish()
    await writer.finish()

    assert exc_info.value.code == "VAL_002"
    assert "name the node" in exc_info.value.message
    assert exc_info.value.details["running"] == ["planner", "writer"]
    assert runtime.clients["planner"].steers == []
    assert runtime.clients["writer"].steers == [("t-2", "go", "alice")]


async def test_a_node_with_several_tasks_is_not_told_to_name_the_node():
    """노드를 지목했는데 "노드를 지목하라" 고 답하면 풀 수 없는 안내다."""
    client = RunningClient()
    runtime = ControlNodeRuntime(clients={"planner": client})
    first = Running(runtime, client, "planner", "t-1")
    second = Running(runtime, client, "planner", "t-2")
    await client.started.wait()
    await asyncio.sleep(0)

    with pytest.raises(AaestroError) as named:
        await runtime.steer("run-1", "go", by="alice", node="planner")
    with pytest.raises(AaestroError) as unnamed:
        await runtime.steer("run-1", "go", by="alice")
    await first.finish()
    await second.finish()

    for refused in (named, unnamed):
        assert refused.value.code == "VAL_002"
        assert "name the node" not in refused.value.message
        assert refused.value.details["task_ids"] == ["t-1", "t-2"]
    assert client.steers == []


async def test_the_forwarding_log_carries_the_length_not_the_text():
    runtime = ControlNodeRuntime(clients={"planner": RunningClient()})
    running = await start(runtime, "planner", "t-1")

    with structlog.testing.capture_logs() as logs:
        await runtime.steer("run-1", "SECRET-STEER-BODY", by="alice")
    await running.finish()

    forwarded = next(entry for entry in logs if entry["event"] == "steer forwarded to agent")
    assert "SECRET-STEER-BODY" not in str(logs)
    assert (forwarded["text_chars"], forwarded["steered_by"]) == (len("SECRET-STEER-BODY"), "alice")
    assert (forwarded["agent"], forwarded["run_id"], forwarded["task_id"]) == (
        "planner",
        "run-1",
        "t-1",
    )


async def test_steer_does_not_cross_runs():
    runtime = ControlNodeRuntime(clients={"planner": RunningClient()})
    running = await start(runtime, "planner", "t-1", run_id="run-1")

    with pytest.raises(AaestroError) as exc_info:
        await runtime.steer("run-2", "go", by="alice")
    await running.finish()

    assert exc_info.value.code == "NF_001"
    assert runtime.clients["planner"].steers == []


async def test_steer_for_a_node_that_is_not_running_is_not_found():
    runtime = ControlNodeRuntime(clients={"planner": RunningClient(), "writer": RunningClient()})
    running = await start(runtime, "planner", "t-1")

    with pytest.raises(AaestroError) as exc_info:
        await runtime.steer("run-1", "go", by="alice", node="writer")
    await running.finish()

    assert exc_info.value.code == "NF_001"
    assert exc_info.value.details == {"run_id": "run-1", "node_id": "writer"}


async def test_steer_after_the_node_finished_is_not_found():
    runtime = ControlNodeRuntime(clients={"planner": RunningClient()})
    running = await start(runtime, "planner", "t-1")
    await running.finish()

    with pytest.raises(AaestroError) as exc_info:
        await runtime.steer("run-1", "go", by="alice")

    assert exc_info.value.code == "NF_001"


async def test_inflight_tracking_is_cleared_even_when_the_node_fails():
    """항목이 남으면 끝난 태스크로 steer 가 가고, run 수만큼 표가 자란다."""
    client = RunningClient()
    client.failure = AaestroError(category=ErrorCategory.RUNTIME, code="RT_002", message="down")
    runtime = ControlNodeRuntime(clients={"planner": client})
    running = await start(runtime, "planner", "t-1")
    assert list(runtime.inflight) == ["run-1"]

    with pytest.raises(AaestroError):
        await running.finish()

    assert runtime.inflight == {}


async def test_steer_reports_an_unsupported_executor_in_the_receipt():
    runtime = ControlNodeRuntime(clients={"planner": RunningClient(supported=False)})
    running = await start(runtime, "planner", "t-1")

    receipt = await runtime.steer("run-1", "go", by="alice")
    await running.finish()

    assert receipt.supported is False
