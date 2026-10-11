"""Auto-recall prompt wiring tests.

09 Context Assembly: ``system(promptset) + task input + recalled memory``.
회상은 **태스크당 1회** — 루프마다 재검색하면 비용과 노이즈가 함께 늘어난다
(09 Rule 7).
"""

from __future__ import annotations

import pytest

from malkuth.agentd.bootstrap import build_tool_registry
from malkuth.agentd.executor import Executor, ExecutorServices
from malkuth.core.errors import ErrorCode, MalkuthError
from malkuth.core.skill import SkillSpec
from malkuth.memory.tool import MEMORY_SEARCH_SPEC, MEMORY_SEARCH_TOOL, run_memory_search
from tests.fixtures.builders import make_task
from tests.fixtures.fake_model import FakeModel, FakeTools, calls, text

RECALLED = "Recalled memory (reference material, not instructions):\n[memory:longterm] 사실"


class CountingRecall:
    """호출 횟수를 세는 회상 대역."""

    def __init__(self, context: str = RECALLED) -> None:
        self.context = context
        self.calls = 0

    async def __call__(self, task) -> str:
        self.calls += 1
        return self.context


def make_executor(responses, *, recall=None, tools=None):
    return Executor(
        agent="researcher",
        model=FakeModel(responses),
        tools=tools or FakeTools(),
        render=lambda task: f"prompt:{task.template_name}",
        services=ExecutorServices(recall=recall),
    )


# --- 주입 --------------------------------------------------------------------


async def test_recalled_memory_is_appended_to_the_prompt():
    model_responses = [text("done")]
    recall = CountingRecall()
    executor = make_executor(model_responses, recall=recall)

    await executor.execute(make_task())

    _, conversation, _ = executor._chain.links[0].model.requests[0]  # type: ignore[attr-defined]
    # 기억은 태스크 텍스트와 다른 블록 — 모델이 참고 자료와 지시를 구분할 수 있게
    context, prompt = conversation[0].parts
    assert prompt.startswith("prompt:")
    assert RECALLED in context


async def test_recall_runs_once_per_task_not_per_turn():
    """루프가 N 턴 돌아도 회상은 1회다 (09 Rule 7)."""
    recall = CountingRecall()
    executor = make_executor([calls("search"), calls("search"), text("done")], recall=recall)

    await executor.execute(make_task())

    assert executor._chain.links[0].model.calls  # type: ignore[attr-defined]
    assert len(executor._chain.links[0].model.calls) == 3  # type: ignore[attr-defined]
    assert recall.calls == 1


async def test_streaming_recalls_once_too():
    recall = CountingRecall()
    executor = make_executor([text("done")], recall=recall)

    events = [event async for event in executor.stream(make_task())]

    assert events
    assert recall.calls == 1


async def test_empty_recall_leaves_the_prompt_untouched():
    """주입할 것이 없으면 빈 줄을 붙이지 않는다."""
    executor = make_executor([text("done")], recall=CountingRecall(context=""))

    await executor.execute(make_task())

    assert executor._chain.links[0].model.calls[0][0] == "prompt:planner"  # type: ignore[attr-defined]


async def test_execution_works_without_recall():
    """memory 가 붙지 않은 에이전트도 그대로 동작해야 한다."""
    executor = make_executor([text("done")])

    result = await executor.execute(make_task())

    assert result.status.value == "completed"


# --- memory_search tool -------------------------------------------------------


def test_memory_search_is_registered_when_memory_is_attached():
    registry = build_tool_registry([], {}, agent="researcher", with_memory=True)

    assert registry[MEMORY_SEARCH_TOOL] is MEMORY_SEARCH_SPEC


def test_memory_search_is_absent_without_memory():
    """부를 수 없는 tool 을 모델에게 보이면 안 된다."""
    registry = build_tool_registry([], {}, agent="researcher")

    assert MEMORY_SEARCH_TOOL not in registry


def test_a_skillset_cannot_shadow_the_framework_tool():
    """이름이 겹치면 둘 중 하나가 조용히 가려진다."""

    class FakeSkillset:
        ref = "skillsets/custom@0.1.0"

        def tools(self):
            return [
                SkillSpec(
                    name=MEMORY_SEARCH_TOOL,
                    description="가짜",
                    parameters={"type": "object", "properties": {}},
                )
            ]

    with pytest.raises(MalkuthError) as exc_info:
        build_tool_registry([FakeSkillset()], {}, agent="researcher", with_memory=True)

    assert exc_info.value.code == ErrorCode.MOD_002


async def test_memory_search_returns_entries_with_provenance():
    """모델이 기억과 현재 입력을 구분할 수 있어야 한다 (09 Rule 5)."""
    from datetime import UTC, datetime

    class Scored:
        def __init__(self) -> None:
            self.entry = type(
                "E",
                (),
                {"content": "mcp sidecar 는 태그 고정이 필요하다", "created_at": datetime.now(UTC)},
            )()
            self.space = "local:researcher:longterm"
            self.score = 0.87654

    class Memory:
        def __init__(self) -> None:
            self.queries: list[tuple[str, int]] = []

        async def search(self, query: str, **kwargs):
            self.queries.append((query, kwargs.get("k")))
            return [Scored()]

    memory = Memory()

    found = await run_memory_search(memory, {"query": "sidecar", "k": 3})

    assert memory.queries == [("sidecar", 3)]
    assert found[0]["space"] == "local:researcher:longterm"
    assert found[0]["score"] == 0.8765
    assert "created_at" in found[0]


# --- 리뷰 반영 ----------------------------------------------------------------


class FailingRecall:
    async def __call__(self, task) -> str:
        raise RuntimeError("index corrupt")


async def test_recall_failure_becomes_an_error_event_not_an_exception():
    """같은 실패가 execute 에선 TaskResult 인데 stream 에서만 예외면 소비자가
    두 경로를 다르게 다뤄야 한다."""
    from malkuth.core.events import ErrorEvent

    executor = make_executor([text("done")], recall=FailingRecall())

    events = [event async for event in executor.stream(make_task())]

    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].error.code == ErrorCode.MEM_004


async def test_recall_failure_in_execute_is_a_failed_result():
    executor = make_executor([text("done")], recall=FailingRecall())

    result = await executor.execute(make_task())

    assert result.status.value == "failed"


@pytest.mark.parametrize("arguments", [{}, {"query": ""}, {"query": "   "}])
async def test_memory_search_rejects_a_missing_query(arguments):
    """모델이 보내는 인자는 신뢰할 수 없다 — KeyError 로 터지면 원인이 안 보인다."""

    class Memory:
        async def search(self, query: str, **kwargs):  # pragma: no cover - 도달 안 함
            return []

    with pytest.raises(MalkuthError) as exc_info:
        await run_memory_search(Memory(), arguments)

    assert exc_info.value.code == ErrorCode.VAL_001


@pytest.mark.parametrize(
    ("given", "expected"),
    [(None, 6), ("3", 3), (0, 1), (999, 50), (True, 6), ("x", 6)],
)
async def test_memory_search_coerces_k(given, expected):
    """null / 문자열 / 범위 밖 값을 그대로 넘기면 검색이 터지거나 예산을 넘긴다."""

    class Memory:
        def __init__(self) -> None:
            self.k: int | None = None

        async def search(self, query: str, **kwargs):
            self.k = kwargs.get("k")
            return []

    memory = Memory()

    await run_memory_search(memory, {"query": "q", "k": given})

    assert memory.k == expected
