"""Graph-level tests — routing, state isolation, resume.

컨테이너 없이 그래프 라우팅을 검증한다 (06-testing.md Graph-Level Tests):
runtime 을 fake 로 치환하고 checkpointer 는 in-memory 를 쓴다.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

from malkuth.core.agent import TaskResult
from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.core.manifest import ModelConfig, ModelOverride
from malkuth.graphs.schemas import ResearchState
from malkuth.orchestrator.builder import GraphBuilder, build_channel_schema, build_graph
from tests.fixtures.fake_runtime import FakeRuntime
from tests.fixtures.topologies import make_mission

_CONDITIONAL_EDGES = [
    {"from": "START", "to": "planner"},
    {
        "from": "planner",
        "to": "researcher",
        "condition": "malkuth.graphs.conditions:needs_research",
    },
    {"from": "planner", "to": "END", "condition": "malkuth.graphs.conditions:plan_only"},
    {"from": "researcher", "to": "END"},
]


def linear_topology():
    """planner → researcher → END 선형 그래프."""
    return make_mission(
        nodes=[
            {
                "id": "planner",
                "agent": "agents/planner@0.1.0",
                "input_map": {"query": "state.query"},
                "output_map": {"plan": "output.plan"},
            },
            {
                "id": "researcher",
                "agent": "agents/researcher@0.1.0",
                "input_map": {"plan": "state.plan"},
                "output_map": {"findings": "output.findings"},
            },
        ]
    )


def conditional_topology():
    """planner 에서 조건부로 researcher 를 건너뛰는 그래프."""
    return make_mission(
        nodes=[
            {
                "id": "planner",
                "agent": "agents/planner@0.1.0",
                "output_map": {"needs_research": "output.needs_research"},
            },
            {
                "id": "researcher",
                "agent": "agents/researcher@0.1.0",
                "output_map": {"findings": "output.findings"},
            },
        ],
        edges=_CONDITIONAL_EDGES,
    )


# --- 채널 스키마 ------------------------------------------------------------


def test_channel_schema_includes_state_fields_and_reserved_keys():
    """LangGraph 는 선언된 채널만 보존하므로 state 필드가 모두 포함돼야 한다."""
    annotations = build_channel_schema(ResearchState).__annotations__

    assert {"query", "plan", "needs_research", "findings", "report"} <= set(annotations)
    assert {"_iterations", "_run_id", "_trace_id"} <= set(annotations)


# --- 선형 실행 --------------------------------------------------------------


async def test_linear_graph_invokes_nodes_in_order():
    runtime = (
        FakeRuntime()
        .script("planner", output={"plan": "P"})
        .script("researcher", output={"findings": ["f1"]})
    )

    final = await build_graph(linear_topology(), runtime).ainvoke(
        {"query": "q", "_run_id": "run-1"}
    )

    assert runtime.invoked == ["planner", "researcher"]
    assert final["plan"] == "P"
    assert final["findings"] == ["f1"]


async def test_input_map_extracts_declared_state_only():
    runtime = (
        FakeRuntime()
        .script("planner", output={"plan": "P"})
        .script("researcher", output={"findings": []})
    )

    await build_graph(linear_topology(), runtime).ainvoke(
        {"query": "q", "report": "leaked", "_run_id": "run-1"}
    )

    planner_task = runtime.tasks[0]
    assert planner_task.input == {"query": "q"}
    assert "report" not in planner_task.input


async def test_output_map_projects_declared_keys_only():
    """노드가 반환한 미선언 키는 state 에 반영되지 않는다."""
    runtime = (
        FakeRuntime()
        .script("planner", output={"plan": "P", "scratch": "internal"})
        .script("researcher", output={"findings": []})
    )

    final = await build_graph(linear_topology(), runtime).ainvoke(
        {"query": "q", "_run_id": "run-1"}
    )

    assert final["plan"] == "P"
    assert "scratch" not in final


async def test_task_carries_run_and_node_identity():
    runtime = (
        FakeRuntime()
        .script("planner", output={"plan": "P"})
        .script("researcher", output={"findings": []})
    )

    await build_graph(linear_topology(), runtime).ainvoke(
        {"query": "q", "_run_id": "run-42", "_trace_id": "trace-9"}
    )

    task = runtime.tasks[0]
    assert task.run_id == "run-42"
    assert task.node_id == "planner"
    assert task.trace.trace_id == "trace-9"
    assert task.is_direct is False


async def test_task_carries_the_model_the_resolver_chose_per_node():
    runtime = (
        FakeRuntime()
        .script("planner", output={"plan": "P"})
        .script("researcher", output={"findings": []})
    )
    models = {
        "planner": ModelConfig(provider="anthropic", name="claude-haiku-4-5"),
        "researcher": ModelConfig(provider="anthropic", name="claude-opus-5"),
    }

    await build_graph(linear_topology(), runtime, models=lambda node: models[node.id]).ainvoke(
        {"query": "q"}
    )

    assert {t.node_id: t.config.model for t in runtime.tasks} == models


async def test_without_a_resolver_the_task_carries_no_model():
    runtime = (
        FakeRuntime()
        .script("planner", output={"plan": "P"})
        .script("researcher", output={"findings": []})
    )

    await build_graph(linear_topology(), runtime).ainvoke({"query": "q"})

    assert [t.config.model for t in runtime.tasks] == [None, None]


async def test_a_model_that_does_not_resolve_fails_the_node_as_graph_002():
    """계층을 합친 결과가 모델이 아니면 pydantic 오류가 아니라 typed 노드 실패다."""
    runtime = FakeRuntime().script("planner", output={"plan": "P"})

    def broken(node: Any) -> ModelConfig:
        # 주 모델이 fallback 과 겹치면 override 자체는 유효해도 합친 뒤 재검증에서 거부된다
        base = ModelConfig.model_validate(
            {
                "provider": "anthropic",
                "name": "claude-sonnet-5",
                "fallbacks": [
                    {"provider": "anthropic", "name": "claude-haiku-4-5", "on": ["rate_limit"]}
                ],
            }
        )
        return base.overridden(ModelOverride.model_validate({"name": "claude-haiku-4-5"}))

    with pytest.raises(MalkuthError) as exc_info:
        await build_graph(linear_topology(), runtime, models=broken).ainvoke({"query": "q"})

    assert exc_info.value.code == ErrorCode.GRAPH_002
    assert exc_info.value.category is ErrorCategory.GRAPH
    assert exc_info.value.retryable is False
    assert exc_info.value.details["node_id"] == "planner"
    assert exc_info.value.details["reason"]
    assert runtime.tasks == []


async def test_a_typed_resolver_error_passes_through_unchanged():
    runtime = FakeRuntime().script("planner", output={"plan": "P"})

    def missing(node: Any) -> ModelConfig:
        raise MalkuthError(
            category=ErrorCategory.NOT_FOUND, code=ErrorCode.NF_001, message="agent not found"
        )

    with pytest.raises(MalkuthError) as exc_info:
        await build_graph(linear_topology(), runtime, models=missing).ainvoke({"query": "q"})

    assert exc_info.value.code == ErrorCode.NF_001


# --- 조건부 라우팅 ----------------------------------------------------------


async def test_conditional_edge_skips_node_when_false():
    """조건이 거짓이면 해당 분기 노드는 호출되지 않는다."""
    runtime = FakeRuntime().script("planner", output={"needs_research": False})

    await build_graph(conditional_topology(), runtime).ainvoke({"query": "q", "_run_id": "run-1"})

    assert runtime.invoked == ["planner"]


@pytest.mark.parametrize(
    ("needs_research", "expected"), [(False, ["planner"]), (True, ["planner", "researcher"])]
)
async def test_a_declarative_condition_routes_like_the_function_it_replaces(
    needs_research, expected
):
    """YAML 안의 선언식만으로 분기한다 — 조건 함수를 src/ 에 더하지 않는다 (#316)."""
    topology = make_mission(
        nodes=conditional_topology().model_dump(by_alias=True)["spec"]["nodes"],
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "researcher", "condition": "state.needs_research"},
            {"from": "planner", "to": "END", "condition": "not state.needs_research"},
            {"from": "researcher", "to": "END"},
        ],
    )
    runtime = (
        FakeRuntime()
        .script("planner", output={"needs_research": needs_research})
        .script("researcher", output={"findings": ["f"]})
    )

    await build_graph(topology, runtime).ainvoke({"query": "q", "_run_id": "run-1"})

    assert runtime.invoked == expected


async def test_conditional_edge_follows_node_when_true():
    runtime = (
        FakeRuntime()
        .script("planner", output={"needs_research": True})
        .script("researcher", output={"findings": ["f"]})
    )

    final = await build_graph(conditional_topology(), runtime).ainvoke(
        {"query": "q", "_run_id": "run-1"}
    )

    assert runtime.invoked == ["planner", "researcher"]
    assert final["findings"] == ["f"]


# --- 실패 처리 --------------------------------------------------------------


async def test_node_failure_raises_graph_002():
    runtime = FakeRuntime().script("planner", output={"plan": "P"}).fail("researcher")

    with pytest.raises(MalkuthError) as exc_info:
        await build_graph(linear_topology(), runtime).ainvoke({"query": "q", "_run_id": "run-1"})

    assert exc_info.value.code == "GRAPH_002"
    assert exc_info.value.category is ErrorCategory.GRAPH
    assert exc_info.value.details["node_id"] == "researcher"
    # 원 에이전트 에러 코드를 details 로 보존한다
    assert exc_info.value.details["error_code"] == "LLM_003"


async def test_failed_node_does_not_merge_output_into_state():
    """실패한 노드의 출력은 state 를 오염시키지 않는다."""
    checkpointer = MemorySaver()
    config = {"configurable": {"thread_id": "t-isolation"}}
    runtime = FakeRuntime().script("planner", output={"plan": "P"}).fail("researcher")
    graph = build_graph(linear_topology(), runtime, checkpointer=checkpointer)

    with pytest.raises(MalkuthError):
        await graph.ainvoke({"query": "q", "_run_id": "run-1"}, config)

    snapshot = await graph.aget_state(config)
    assert snapshot.values["plan"] == "P"  # 성공한 노드까지는 반영
    assert "findings" not in snapshot.values  # 실패 노드 출력은 미반영


async def test_node_timeout_raises_to_003():
    class SlowRuntime:
        async def invoke(self, node, task):
            await asyncio.sleep(1)
            return TaskResult.completed(task)

    topology = make_mission(
        nodes=[
            {"id": "planner", "agent": "agents/planner@0.1.0", "timeout_s": 0.01},
            {"id": "researcher", "agent": "agents/researcher@0.1.0"},
        ]
    )

    with pytest.raises(MalkuthError) as exc_info:
        await build_graph(topology, SlowRuntime(), control_overhead_s=0).ainvoke(
            {"query": "q", "_run_id": "r"}
        )

    assert exc_info.value.code == "TO_003"
    assert exc_info.value.retryable is True


# --- timeout 경계: agentd 가 강제하고 orchestrator 는 결과를 기다린다 (#314) ----------------


def _timed_topology(timeout_s: float) -> Any:
    return make_mission(
        nodes=[
            {"id": "planner", "agent": "agents/planner@0.1.0", "timeout_s": timeout_s},
            {"id": "researcher", "agent": "agents/researcher@0.1.0"},
        ]
    )


async def test_an_agent_timeout_reaches_the_graph_as_to_001():
    """agentd 가 timeout_s 에 보낸 TO_001 결과를 orchestrator 가 먼저 끊어 버리면 안 된다."""

    class AgentTimesOut:
        async def invoke(self, node, task):
            # agentd 는 정확히 timeout_s 에 끊고, 결과는 왕복만큼 늦게 도착한다
            await asyncio.sleep(task.config.timeout_s + 0.02)
            err = MalkuthError(category=ErrorCategory.TIMEOUT, code="TO_001", message="late")
            return TaskResult.failed(task, err)

    graph = build_graph(_timed_topology(0.05), AgentTimesOut(), control_overhead_s=0.5)

    with pytest.raises(MalkuthError) as exc_info:
        await graph.ainvoke({"query": "q", "_run_id": "r"})

    assert exc_info.value.code == "GRAPH_002"
    assert exc_info.value.details["error_code"] == "TO_001"


async def test_the_orchestrator_cancels_a_task_it_stopped_waiting_for():
    """취소를 알리지 않으면 에이전트는 아무도 기다리지 않는 태스크를 끝까지 돈다."""

    class Hangs:
        def __init__(self) -> None:
            self.cancelled: list[tuple[str, str]] = []
            self.task_ids: list[str] = []

        async def invoke(self, node, task):
            self.task_ids.append(task.task_id)
            await asyncio.sleep(10)

        async def cancel(self, node, task_id):
            self.cancelled.append((node.id, task_id))

    runtime = Hangs()

    with pytest.raises(MalkuthError) as exc_info:
        await build_graph(_timed_topology(0.01), runtime, control_overhead_s=0).ainvoke(
            {"query": "q", "_run_id": "r"}
        )

    assert exc_info.value.code == "TO_003"
    assert runtime.cancelled == [("planner", runtime.task_ids[0])]


async def test_a_failed_cancel_does_not_hide_the_timeout():
    class CancelFails:
        async def invoke(self, node, task):
            await asyncio.sleep(10)

        async def cancel(self, node, task_id):
            raise MalkuthError(category=ErrorCategory.NETWORK, code="NET_001", message="down")

    with pytest.raises(MalkuthError) as exc_info:
        await build_graph(_timed_topology(0.01), CancelFails(), control_overhead_s=0).ainvoke(
            {"query": "q", "_run_id": "r"}
        )

    assert exc_info.value.code == "TO_003"


# --- checkpoint 재개 --------------------------------------------------------


async def test_resume_from_checkpoint_after_node_failure():
    """node 실패 → 동일 checkpoint 에서 resume → 성공 (06 필수 시나리오)."""
    checkpointer = MemorySaver()
    config = {"configurable": {"thread_id": "t-resume"}}
    runtime = (
        FakeRuntime()
        .script("planner", output={"plan": "P"})
        .script("researcher", output={"findings": ["f1"]})
        .fail("researcher", once=True)
    )
    graph = build_graph(linear_topology(), runtime, checkpointer=checkpointer)

    with pytest.raises(MalkuthError):
        await graph.ainvoke({"query": "q", "_run_id": "run-1"}, config)

    # 같은 thread 로 재개하면 planner 는 다시 실행되지 않는다
    final = await graph.ainvoke(None, config)

    assert final["findings"] == ["f1"]
    assert runtime.invoked == ["planner", "researcher", "researcher"]


async def test_checkpoint_records_state_per_node():
    checkpointer = MemorySaver()
    config = {"configurable": {"thread_id": "t-history"}}
    runtime = (
        FakeRuntime()
        .script("planner", output={"plan": "P"})
        .script("researcher", output={"findings": ["f"]})
    )
    graph = build_graph(linear_topology(), runtime, checkpointer=checkpointer)

    await graph.ainvoke({"query": "q", "_run_id": "run-1"}, config)

    history = [snapshot async for snapshot in graph.aget_state_history(config)]
    assert len(history) > 1


# --- 반복 상한 --------------------------------------------------------------


async def test_max_iterations_exceeded_raises_graph_004():
    topology = make_mission(
        nodes=[
            {
                "id": "planner",
                "agent": "agents/planner@0.1.0",
                "output_map": {"needs_research": "output.needs_research"},
            },
            {"id": "researcher", "agent": "agents/researcher@0.1.0"},
        ],
        edges=[
            {"from": "START", "to": "planner"},
            {
                "from": "planner",
                "to": "researcher",
                "condition": "malkuth.graphs.conditions:needs_research",
            },
            {"from": "planner", "to": "END", "condition": "malkuth.graphs.conditions:plan_only"},
            {
                "from": "researcher",
                "to": "planner",
                "condition": "malkuth.graphs.conditions:needs_research",
                "max_iterations": 2,
            },
            {"from": "researcher", "to": "END", "condition": "malkuth.graphs.conditions:plan_only"},
        ],
    )
    runtime = FakeRuntime().script("planner", output={"needs_research": True}).script("researcher")

    with pytest.raises(MalkuthError) as exc_info:
        await build_graph(topology, runtime).ainvoke(
            {"query": "q", "_run_id": "r"}, {"recursion_limit": 50}
        )

    assert exc_info.value.code == "GRAPH_004"


# --- 빌더 계약 --------------------------------------------------------------


def test_builder_resolves_state_schema_from_topology():
    builder = GraphBuilder(linear_topology(), FakeRuntime())

    assert builder.state_schema is ResearchState


def test_builder_accepts_preresolved_schema():
    builder = GraphBuilder(linear_topology(), FakeRuntime(), state_schema=ResearchState)

    assert builder.state_schema is ResearchState


async def test_unmatched_condition_without_fallback_raises_graph_001():
    topology = make_mission(
        nodes=[
            {"id": "planner", "agent": "agents/planner@0.1.0"},
            {"id": "researcher", "agent": "agents/researcher@0.1.0"},
        ],
        edges=[
            {"from": "START", "to": "planner"},
            {
                "from": "planner",
                "to": "researcher",
                "condition": "malkuth.graphs.conditions:has_new_items",
            },
            {"from": "researcher", "to": "END"},
        ],
    )
    runtime = FakeRuntime().script("planner").script("researcher")

    with pytest.raises(MalkuthError) as exc_info:
        await build_graph(topology, runtime).ainvoke({"query": "q", "_run_id": "r"})

    assert exc_info.value.code == ErrorCode.GRAPH_001


async def test_conditions_declared_after_an_unconditional_edge_still_win():
    """무조건 edge 는 폴백이지 조기 종료가 아니다.

    선언 순서상 뒤에 있는 조건부 edge 가 평가되지 않으면, 폴백을 위에 적은
    토폴로지가 조건 분기를 통째로 잃는다.
    """
    topology = make_mission(
        nodes=[
            {
                "id": "planner",
                "agent": "agents/planner@0.1.0",
                "output_map": {"needs_research": "output.needs_research"},
            },
            {
                "id": "researcher",
                "agent": "agents/researcher@0.1.0",
                "output_map": {"findings": "output.findings"},
            },
        ],
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "END"},  # 폴백을 먼저 선언
            {
                "from": "planner",
                "to": "researcher",
                "condition": "malkuth.graphs.conditions:needs_research",
            },
            {"from": "researcher", "to": "END"},
        ],
    )
    runtime = (
        FakeRuntime()
        .script("planner", output={"needs_research": True})
        .script("researcher", output={"findings": ["f"]})
    )

    await build_graph(topology, runtime).ainvoke({"query": "q", "_run_id": "r"})

    assert runtime.invoked == ["planner", "researcher"]


async def test_unconditional_edge_is_used_when_no_condition_matches():
    topology = make_mission(
        nodes=[
            {
                "id": "planner",
                "agent": "agents/planner@0.1.0",
                "output_map": {"needs_research": "output.needs_research"},
            },
            {"id": "researcher", "agent": "agents/researcher@0.1.0"},
        ],
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "END"},
            {
                "from": "planner",
                "to": "researcher",
                "condition": "malkuth.graphs.conditions:needs_research",
            },
            {"from": "researcher", "to": "END"},
        ],
    )
    runtime = FakeRuntime().script("planner", output={"needs_research": False})

    await build_graph(topology, runtime).ainvoke({"query": "q", "_run_id": "r"})

    assert runtime.invoked == ["planner"]
