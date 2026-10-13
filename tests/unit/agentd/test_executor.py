"""Unit tests for the agentd tool execution loop.

실제 LLM 을 호출하지 않는다 — 스크립트된 FakeModel 로만 검증한다 (06 규칙).
"""

from __future__ import annotations

import asyncio

import pytest

from aaestro.agentd import executor as executor_module
from aaestro.agentd.executor import (
    AssistantTurn,
    Executor,
    ExecutorConfig,
    ExecutorServices,
    ToolCall,
    ToolOutcome,
    ToolTurn,
    UserTurn,
    tool_outcome,
)
from aaestro.agentd.models import ModelChain, ModelLink
from aaestro.core.agent import TaskConfig, TaskStatus
from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.core.events import DoneEvent, ErrorEvent, ToolCallEvent, ToolResultEvent
from aaestro.protocols.mcp.session import ToolResult as McpToolResult
from tests.fixtures.builders import make_task
from tests.fixtures.fake_model import FakeModel, FakeTools, calls, text


def make_executor(
    responses, tools: FakeTools | None = None, *, on_cleanup=None, **config
) -> tuple[Executor, FakeModel, FakeTools]:
    """FakeModel/FakeTools 를 물린 executor."""
    model = FakeModel(responses)
    registry = tools or FakeTools()
    executor = Executor(
        agent="researcher",
        model=model,
        tools=registry,
        render=lambda task: f"prompt:{task.template_name}",
        config=ExecutorConfig(**config) if config else None,
        services=ExecutorServices(on_cleanup=on_cleanup),
    )
    return executor, model, registry


# --- 기본 루프 --------------------------------------------------------------


async def test_final_response_completes_the_task():
    executor, model, _ = make_executor([text("done")])

    result = await executor.execute(make_task())

    assert result.status is TaskStatus.COMPLETED
    assert result.output == {"content": "done"}
    assert model.turns == 1


async def test_tool_call_then_completion():
    tools = FakeTools().script("search", result="found")
    executor, model, _ = make_executor([calls("search"), text("done")], tools)

    result = await executor.execute(make_task())

    assert result.status is TaskStatus.COMPLETED
    assert tools.calls == ["search"]
    assert model.turns == 2


async def test_tool_results_feed_the_next_prompt():
    tools = FakeTools().script("search", result="found")
    executor, model, _ = make_executor([calls("search"), text("done")], tools)

    await executor.execute(make_task())

    # 결과는 태스크 텍스트에 섞이지 않고 호출 id 로 짝지어 돌아간다 (#308)
    _, conversation, _ = model.requests[1]
    opening, assistant, results = conversation
    assert isinstance(opening, UserTurn)
    assert isinstance(assistant, AssistantTurn)
    assert [c.id for c in assistant.tool_calls] == ["c0"]
    assert results == ToolTurn(outcomes=(ToolOutcome(call_id="c0", content="found"),))


# --- 상한과 타임아웃 --------------------------------------------------------


async def test_max_turns_exceeded_raises_llm_005():
    tools = FakeTools().script("search")
    executor, _, _ = make_executor([calls("search")], tools, max_turns=3)

    result = await executor.execute(make_task())

    assert result.status is TaskStatus.FAILED
    assert result.error is not None
    assert result.error.code == "LLM_005"
    assert result.error.details["max_turns"] == 3


async def test_task_timeout_reports_to_001():
    tools = FakeTools().script("slow", delay=5)
    executor, _, _ = make_executor([calls("slow"), text("done")], tools)

    result = await executor.execute(make_task(config=TaskConfig(timeout_s=0.05)))

    assert result.error is not None
    assert result.error.code == "TO_001"
    assert result.error.retryable is True


async def test_tool_timeout_reports_to_002():
    tools = FakeTools().script("slow", delay=5).timeout("slow", 0.05)
    executor, _, _ = make_executor([calls("slow"), text("done")], tools)

    result = await executor.execute(make_task())

    assert result.error is not None
    assert result.error.code == "TO_002"
    assert result.error.details["tool"] == "slow"


# --- 에러 변환 --------------------------------------------------------------


async def test_skillset_tool_failure_becomes_skill_001():
    """skill 도메인 예외는 SKILL_001 로 감싼다 (05 Layer Rules)."""
    tools = FakeTools().fail("search", RuntimeError("boom"))
    executor, _, _ = make_executor([calls("search"), text("done")], tools)

    result = await executor.execute(make_task())

    assert result.error is not None
    assert result.error.code == "SKILL_001"
    assert result.error.category is ErrorCategory.MODULE


async def test_mcp_tool_failure_becomes_mcp_003():
    """출처가 다르면 재시도·알림 전략도 달라야 한다."""
    tools = FakeTools().fail("mcp__fs__read_file", RuntimeError("boom"))
    executor, _, _ = make_executor([calls("mcp__fs__read_file"), text("done")], tools)

    result = await executor.execute(make_task())

    assert result.error is not None
    assert result.error.code == "MCP_003"
    assert result.error.category is ErrorCategory.MCP


async def test_structured_tool_errors_pass_through_unchanged():
    """이미 구조화된 에러는 재변환하지 않는다."""
    original = AaestroError(category=ErrorCategory.MCP, code="MCP_002", message="tool not found")
    tools = FakeTools().fail("mcp__fs__missing", original)
    executor, _, _ = make_executor([calls("mcp__fs__missing"), text("done")], tools)

    result = await executor.execute(make_task())

    assert result.error is not None
    assert result.error.code == "MCP_002"


async def test_task_failure_does_not_raise():
    """태스크 실패는 결과로 보고한다 — 데몬을 죽이지 않는다."""
    tools = FakeTools().fail("search", RuntimeError("boom"))
    executor, _, _ = make_executor([calls("search"), text("done")], tools)

    result = await executor.execute(make_task())

    assert result.status is TaskStatus.FAILED


# --- 병렬 실행 --------------------------------------------------------------


async def test_independent_tools_run_in_parallel():
    """직렬 실행이면 0.3s 가 걸린다 — 병렬이면 그보다 훨씬 짧다."""
    tools = FakeTools().script("a", delay=0.1).script("b", delay=0.1).script("c", delay=0.1)
    executor, _, _ = make_executor([calls("a", "b", "c"), text("done")], tools)

    started = asyncio.get_running_loop().time()
    await executor.execute(make_task())
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.25
    assert set(tools.calls) == {"a", "b", "c"}


async def test_all_parallel_tools_start_before_any_completes():
    """이름대로 검증하려면 '완료 전에 모두 시작'을 봐야 한다.

    실행 후 started 길이만 보면 순차 실행도 통과한다.
    """
    tools = FakeTools().script("a", delay=0.1).script("b", delay=0.1)
    executor, _, _ = make_executor([calls("a", "b"), text("done")], tools)

    running = asyncio.create_task(executor.execute(make_task()))
    await asyncio.sleep(0.05)  # 첫 tool 이 끝나기 전 시점
    started_early = list(tools.started)
    completed_early = list(tools.calls)
    await running

    assert started_early == ["a", "b"]  # 둘 다 이미 시작
    assert completed_early == []  # 아직 아무것도 완료되지 않음


# --- usage 집계 -------------------------------------------------------------


async def test_usage_accumulates_across_turns():
    tools = FakeTools().script("search")
    executor, _, _ = make_executor(
        [
            calls("search", input_tokens=10, output_tokens=5),
            text("done", input_tokens=7, output_tokens=3),
        ],
        tools,
    )

    result = await executor.execute(make_task())

    assert result.usage.input_tokens == 17
    assert result.usage.output_tokens == 8


# --- cancellation -----------------------------------------------------------


async def test_cancellation_propagates_and_cleans_up():
    """취소 시 진행 중 tool 을 정리한 뒤 전파한다."""
    cleaned = asyncio.Event()
    tools = FakeTools().script("slow", delay=5)
    executor, _, _ = make_executor([calls("slow"), text("done")], tools, on_cleanup=cleaned.set)

    task = asyncio.create_task(executor.execute(make_task()))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


async def test_cancellation_is_not_reported_as_a_result():
    """취소를 TaskResult 로 삼키면 협조적 종료가 실패로 오인된다."""
    tools = FakeTools().script("slow", delay=5)
    executor, _, _ = make_executor([calls("slow"), text("done")], tools)

    task = asyncio.create_task(executor.execute(make_task()))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# --- 멱등성 ----------------------------------------------------------------


async def test_same_task_id_returns_the_cached_result():
    """재시도/재개 시나리오에서 동일 task_id 재호출은 안전해야 한다."""
    executor, model, _ = make_executor([text("done")])
    task = make_task(task_id="task-idem")

    first = await executor.execute(task)
    second = await executor.execute(task)

    assert first == second
    assert model.turns == 1  # 두 번 실행되지 않는다


async def test_the_oldest_result_is_forgotten_past_the_limit(monkeypatch):
    """상주 컨테이너가 모든 결과를 끝까지 쥐면 메모리가 태스크 수만큼 자란다 (#315)."""
    monkeypatch.setattr(executor_module, "REMEMBERED_RESULTS", 2)
    executor, model, _ = make_executor([text("a"), text("b"), text("c"), text("again")])

    for task_id in ("t1", "t2", "t3"):
        await executor.execute(make_task(task_id=task_id))
    await executor.execute(make_task(task_id="t3"))  # 아직 기억한다
    assert model.turns == 3
    await executor.execute(make_task(task_id="t1"))  # 잊었으므로 다시 돈다

    assert model.turns == 4


async def test_distinct_tasks_execute_separately():
    executor, model, _ = make_executor([text("a"), text("b")])

    await executor.execute(make_task(task_id="t1"))
    await executor.execute(make_task(task_id="t2"))

    assert model.turns == 2


# --- direct 태스크 ----------------------------------------------------------


async def test_direct_task_renders_the_default_template():
    """node_id 가 없으면 default 템플릿 (02 Direct Request Rules 2)."""
    executor, model, _ = make_executor([text("done")])

    await executor.execute(make_task(node_id=None, run_id="direct-1"))

    assert model.calls[0][0] == "prompt:default"


async def test_graph_task_renders_the_node_template():
    executor, model, _ = make_executor([text("done")])

    await executor.execute(make_task(node_id="research"))

    assert model.calls[0][0] == "prompt:research"


# --- 스트리밍 --------------------------------------------------------------


async def test_stream_emits_tool_and_done_events():
    tools = FakeTools().script("search", result="found")
    executor, _, _ = make_executor([calls("search"), text("done")], tools)

    events = [event async for event in executor.stream(make_task())]

    kinds = [type(e) for e in events]
    assert ToolCallEvent in kinds
    assert ToolResultEvent in kinds
    assert isinstance(events[-1], DoneEvent)


async def test_stream_carries_the_turn_index():
    tools = FakeTools().script("search")
    executor, _, _ = make_executor([calls("search"), text("done")], tools)

    events = [e async for e in executor.stream(make_task())]

    tool_calls = [e for e in events if isinstance(e, ToolCallEvent)]
    assert tool_calls[0].turn == 0


async def test_stream_reports_tool_failure_as_error_event():
    tools = FakeTools().fail("search", RuntimeError("boom"))
    executor, _, _ = make_executor([calls("search"), text("done")], tools)

    events = [e async for e in executor.stream(make_task())]

    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].error.code == "SKILL_001"


async def test_stream_reports_max_turns_as_error_event():
    tools = FakeTools().script("search")
    executor, _, _ = make_executor([calls("search")], tools, max_turns=2)

    events = [e async for e in executor.stream(make_task())]

    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].error.code == "LLM_005"


async def test_stream_cancellation_cleans_up():
    cleaned = asyncio.Event()
    tools = FakeTools().script("slow", delay=5)
    executor, _, _ = make_executor([calls("slow"), text("done")], tools, on_cleanup=cleaned.set)

    async def consume():
        async for _ in executor.stream(make_task()):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


async def test_the_stricter_max_turns_wins():
    """태스크와 executor 중 더 엄격한 상한이 적용돼야 한다.

    `or` 로 고르면 TaskConfig 의 기본값(20)이 항상 이겨 executor 설정이 무시된다.
    """
    tools = FakeTools().script("search")
    executor, model, _ = make_executor([calls("search")], tools, max_turns=2)

    result = await executor.execute(make_task(config=TaskConfig(max_turns=20)))

    assert result.error is not None
    assert result.error.details["max_turns"] == 2
    assert model.turns == 2


async def test_task_can_tighten_the_limit_further():
    tools = FakeTools().script("search")
    executor, model, _ = make_executor([calls("search")], tools, max_turns=10)

    result = await executor.execute(make_task(config=TaskConfig(max_turns=3)))

    assert result.error is not None
    assert result.error.details["max_turns"] == 3
    assert model.turns == 3


# --- 재시도 가능 실패는 캐싱하지 않는다 --------------------------------------


async def test_retryable_failures_are_not_cached():
    """재시도 가능 실패를 캐싱하면 이 계층의 재시도가 영원히 무효화된다."""
    tools = FakeTools().script("slow", delay=5)
    executor, _, _ = make_executor([calls("slow"), text("done")], tools)
    task = make_task(task_id="retry-me", config=TaskConfig(timeout_s=0.05))

    first = await executor.execute(task)
    assert first.error is not None
    assert first.error.retryable is True

    # 같은 task_id 로 재시도하면 다시 실행돼야 한다
    executor._chain = ModelChain(
        links=(ModelLink(model=FakeModel([text("recovered")]), provider="", name=""),)
    )
    second = await executor.execute(task)

    assert second.status is TaskStatus.COMPLETED
    assert second.output == {"content": "recovered"}


async def test_permanent_failures_stay_cached():
    """영구 실패는 재실행해도 결과가 같으므로 캐싱한다."""
    tools = FakeTools().fail("search", RuntimeError("boom"))
    executor, model, _ = make_executor([calls("search"), text("done")], tools)
    task = make_task(task_id="permanent")

    first = await executor.execute(task)
    turns_after_first = model.turns
    second = await executor.execute(task)

    assert first == second
    assert model.turns == turns_after_first  # 재실행되지 않는다


# --- 모델 예외 --------------------------------------------------------------


async def test_unexpected_model_error_becomes_a_failed_result():
    """provider 예외가 새어나가면 데몬이 죽는다."""
    executor, _, _ = make_executor([RuntimeError("provider exploded")])

    result = await executor.execute(make_task())

    assert result.status is TaskStatus.FAILED
    assert result.error is not None
    assert result.error.code == "INTERNAL_001"
    assert result.error.category is ErrorCategory.INTERNAL


# --- tool timeout 우선순위 --------------------------------------------------


async def test_task_tool_timeout_is_honored():
    """TaskConfig.tool_timeout_s 가 무시되면 태스크별 설정이 무의미해진다."""
    tools = FakeTools().script("slow", delay=5)
    executor, _, _ = make_executor([calls("slow"), text("done")], tools)

    result = await executor.execute(make_task(config=TaskConfig(tool_timeout_s=0.05)))

    assert result.error is not None
    assert result.error.code == "TO_002"


async def test_per_tool_declaration_overrides_defaults():
    """skillset 이 선언한 상한이 있으면 그것이 우선한다."""
    tools = FakeTools().script("slow", delay=0.1).timeout("slow", 0.5)
    executor, _, _ = make_executor([calls("slow"), text("done")], tools, tool_timeout_s=0.02)

    result = await executor.execute(make_task())

    assert result.status is TaskStatus.COMPLETED


# --- stream 이 execute 와 같은 계약을 지킨다 ---------------------------------


async def test_stream_applies_the_task_timeout():
    """스트리밍은 장시간 태스크 경로라 멈춘 provider 를 만날 가능성이 가장 높다."""
    tools = FakeTools().script("slow", delay=3)
    executor, _, _ = make_executor([calls("slow"), text("done")], tools)

    events = [e async for e in executor.stream(make_task(config=TaskConfig(timeout_s=0.15)))]

    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].error.code == "TO_001"


async def test_stream_runs_tools_in_parallel():
    """두 경로가 같은 입력에 다른 타이밍을 내면 안 된다."""
    tools = FakeTools().script("a", delay=0.1).script("b", delay=0.1).script("c", delay=0.1)
    executor, _, _ = make_executor([calls("a", "b", "c"), text("done")], tools)

    started = asyncio.get_running_loop().time()
    [e async for e in executor.stream(make_task())]
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.25


async def test_stream_max_turns_carries_the_same_details():
    """스트리밍과 동기 경로의 에러 payload 가 갈리면 디버깅 맥락을 잃는다."""
    tools = FakeTools().script("search")
    executor, _, _ = make_executor([calls("search")], tools, max_turns=2)

    events = [e async for e in executor.stream(make_task())]

    assert events[-1].error.details["max_turns"] == 2


# --- 태스크 결과 로그 (#290) ---------------------------------------------------------


def outcome_logs(logs: list[dict]) -> list[dict]:
    return [e for e in logs if e["event"] in ("agent task failed", "agent task completed")]


async def test_a_failed_task_is_logged_as_an_error_with_its_code():
    """실패를 결과로만 돌려주면 원인이 컨테이너 로그에서 사라진다."""
    from structlog.testing import capture_logs

    tools = FakeTools().script("search")
    executor, _, _ = make_executor([calls("search")], tools, max_turns=2)

    with capture_logs() as logs:
        await executor.execute(make_task())

    [entry] = outcome_logs(logs)
    assert entry["log_level"] == "error"
    assert entry["error_code"] == "LLM_005"
    assert (entry["agent"], entry["task_id"], entry["run_id"], entry["node_id"]) == (
        "researcher",
        "task-0001",
        "run-0001",
        "planner",
    )
    assert entry["graph"] == "direct" and isinstance(entry["duration_ms"], int)


async def test_a_completed_task_is_logged_with_its_usage():
    from structlog.testing import capture_logs

    executor, _, _ = make_executor([text("done")])

    with capture_logs() as logs:
        await executor.execute(make_task(node_id=None))

    [entry] = outcome_logs(logs)
    assert entry["log_level"] == "info" and entry["status"] == "completed"
    assert "node_id" not in entry, "direct 태스크에 노드 id 를 지어내면 안 된다"
    assert {"input_tokens", "output_tokens"} <= entry.keys()


async def test_a_replayed_task_is_not_logged_twice():
    """같은 task_id 재호출은 캐시 응답이다 — 한 번 실행을 두 번 기록하면 집계가 틀어진다."""
    from structlog.testing import capture_logs

    executor, _, _ = make_executor([text("done")])
    task = make_task()

    with capture_logs() as logs:
        await executor.execute(task)
        await executor.execute(task)

    assert len(outcome_logs(logs)) == 1


async def test_a_streamed_task_logs_its_outcome_too():
    from structlog.testing import capture_logs

    tools = FakeTools().script("search")
    failing, _, _ = make_executor([calls("search")], tools, max_turns=2)
    passing, _, _ = make_executor([text("done")])

    with capture_logs() as logs:
        [e async for e in failing.stream(make_task(task_id="fails"))]
        [e async for e in passing.stream(make_task(task_id="passes"))]

    by_task = {e["task_id"]: e for e in outcome_logs(logs)}
    assert by_task["fails"]["log_level"] == "error" and by_task["fails"]["error_code"] == "LLM_005"
    assert by_task["passes"]["log_level"] == "info"


async def test_a_streamed_timeout_is_logged():
    from structlog.testing import capture_logs

    class Stuck:
        turns = 0

        async def run(self, *args, **kwargs):
            await asyncio.Event().wait()

    executor = Executor(
        agent="researcher",
        model=Stuck(),
        tools=FakeTools(),
        render=lambda task: "prompt",
    )

    with capture_logs() as logs:
        [e async for e in executor.stream(make_task(config=TaskConfig(timeout_s=0.01)))]

    [entry] = outcome_logs(logs)
    assert entry["error_code"] == "TO_001"


# --- 대화 구조 (#308) ------------------------------------------------------------------


async def test_the_system_instruction_is_sent_apart_from_the_task():
    model = FakeModel([text("done")])
    executor = Executor(
        agent="a", model=model, tools=FakeTools(), render=lambda t: "task", system="rules"
    )

    await executor.execute(make_task())

    system, conversation, _ = model.requests[0]
    assert system == "rules"
    assert conversation == (UserTurn(parts=("task",)),)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("plain", ToolOutcome(call_id="c0", content="plain")),
        # repr 이 아니라 JSON — 모델이 파이썬 리터럴을 해석하게 두지 않는다
        ({"hits": ["a", "b"]}, ToolOutcome(call_id="c0", content='{"hits": ["a", "b"]}')),
        (["한글"], ToolOutcome(call_id="c0", content='["한글"]')),
        (
            McpToolResult(content="denied", is_error=True),
            ToolOutcome(call_id="c0", content="denied", is_error=True),
        ),
        (McpToolResult(content={"ok": 1}), ToolOutcome(call_id="c0", content='{"ok": 1}')),
    ],
)
def test_tool_outcome_serializes_results_for_the_model(result, expected):
    assert tool_outcome(ToolCall(id="c0", name="search"), result) == expected


# --- 태스크 키: task_id 는 보낸 쪽이 고른다 (#400) -------------------------------------------

SHARED_ID = "task-shared"
PEER = "researcher"


def sent_by(caller: str | None, query: str):
    """같은 task_id 를 쓰는 태스크 — ``caller`` 는 입구가 확인한 값이다 (Control API 는 None)."""
    return make_task(task_id=SHARED_ID, input={"query": query}, caller=caller)


@pytest.mark.parametrize("victim", [None, "writer"], ids=["control", "another-peer"])
async def test_a_peer_reusing_another_callers_task_id_runs_its_own_task(victim):
    """task_id 는 보낸 쪽이 고른다 — 같은 id 를 보냈다고 남의 결과를 받으면 안 된다."""
    executor, model, _ = make_executor([text("for the victim"), text("for the peer")])
    await executor.execute(sent_by(victim, "victim input"))

    result = await executor.execute(sent_by(PEER, "peer input"))

    assert result.output == {"content": "for the peer"}
    assert model.turns == 2, "peer 가 다른 호출자의 캐시된 결과를 받았다"
    assert result.task_id == SHARED_ID, "밖으로 나가는 id 는 peer 가 준 그대로여야 한다"


async def test_a_task_id_a_peer_finished_first_does_not_answer_the_control_task():
    """peer 가 먼저 심은 결과가 오케스트레이터의 태스크와 그 재시도에 돌아가면 state 가 오염된다."""
    executor, model, _ = make_executor([text("planted by the peer"), text("the node's own")])
    await executor.execute(sent_by(PEER, "peer input"))

    first = await executor.execute(sent_by(None, "node input"))
    retried = await executor.execute(sent_by(None, "node input"))

    assert first.output == {"content": "the node's own"}
    assert retried.output == {"content": "the node's own"}
    assert model.turns == 2


@pytest.mark.parametrize("caller", [None, PEER], ids=["control", "peer"])
async def test_the_same_caller_retrying_a_task_id_still_gets_the_cached_result(caller):
    """분리는 호출자 사이에만 선다 — 같은 호출자의 재시도는 지금처럼 멱등이다 (02 Rule 3)."""
    executor, model, _ = make_executor([text("once"), text("twice")])
    task = sent_by(caller, "input")

    first = await executor.execute(task)
    retried = await executor.execute(task)

    assert retried == first
    assert model.turns == 1


async def test_a_peer_task_is_logged_under_the_id_the_peer_chose():
    """내부 키만 갈린다 — 운영자는 호출자 쪽 로그와 같은 id 로 대조할 수 있어야 한다."""
    from structlog.testing import capture_logs

    executor, _, _ = make_executor([text("done")])

    with capture_logs() as logs:
        await executor.execute(sent_by(PEER, "peer input"))

    [entry] = outcome_logs(logs)
    assert entry["task_id"] == SHARED_ID
