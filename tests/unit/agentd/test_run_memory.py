"""agentd carries each task's run to the Memory Service (#610).

실행기 + agentd 의 기억 배선 (``memory_search`` tool · auto-recall) + 실제 레지스트리 ·
Memory Service 를 ASGI 로 잇는다 (#609 의 스택). 한 클라이언트를 동시 태스크가 함께 쓰므로
run 은 태스크마다 실린다.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from aaestro.agentd.__main__ import _task_recall
from aaestro.agentd.executor import Executor, ModelResponse, ToolCall
from aaestro.agentd.tools import AgentToolRegistry
from aaestro.catalog import Catalog
from aaestro.memory.client import MEMORY_RUN_HEADER, HttpMemoryAccess
from aaestro.memory.tool import MEMORY_SEARCH_TOOL
from aaestro.modules.registry import ModuleRegistry
from tests.fixtures.builders import async_render, make_task
from tests.fixtures.fake_model import FakeModel, text
from tests.unit.memory.test_run_header import REPO_ROOT, fact, watched

QUERY = "관찰"


def search_call(query: str = QUERY) -> ModelResponse:
    return ModelResponse(
        tool_calls=(ToolCall(id="c0", name=MEMORY_SEARCH_TOOL, arguments={"query": query}),)
    )


def executor_for(memory: HttpMemoryAccess, model: FakeModel) -> Executor:
    return Executor(
        agent="planner",
        model=model,
        tools=AgentToolRegistry(agent="planner", memory=memory),
        render=async_render(lambda task: f"prompt:{task.template_name}"),
    )


def tool_result(model: FakeModel) -> str:
    """두 번째 모델 호출이 받은 ``memory_search`` 결과."""
    _, conversation, _ = model.requests[1]
    return conversation[-1].outcomes[0].content


def indexed(stack) -> None:
    """색인은 비동기다 (09 Write Path) — 큐를 비워 저장한 기억이 검색되게 한다."""
    assert stack.memory.indexer.drain() > 0


async def test_a_node_tasks_memory_search_reaches_its_runs_scratch():
    async with watched() as stack:
        run = stack.start()
        await stack.settled()
        writer = stack.agent("planner", run_id=run)
        stored = await writer.append("scratch", **fact("run 안의 관찰 하나"))
        assert stored.space == f"run:{run}:scratch"
        indexed(stack)

        # agentd 의 클라이언트는 run 을 쥐지 않는다 — 태스크가 싣는다
        model = FakeModel([search_call(), text("done")])
        shared = stack.agent("planner", run_id=None).access
        result = await executor_for(shared, model).execute(make_task(run_id=run))

        assert result.status.value == "completed"
        found = json.loads(tool_result(model))
        assert any(
            item["space"] == f"run:{run}:scratch" and item["content"] == "run 안의 관찰 하나"
            for item in found
        ), found


async def test_task_recall_carries_the_tasks_run():
    async with watched() as stack:
        run = stack.start()
        await stack.settled()
        writer = stack.agent("planner", run_id=run)
        await writer.append("scratch", **fact(QUERY))
        indexed(stack)

        manifest = Catalog.under(REPO_ROOT).agent("planner")
        recall = _task_recall(
            manifest, ModuleRegistry.under(REPO_ROOT), stack.agent("planner", run_id=None).access
        )
        assert recall is not None

        context = await recall(make_task(run_id=run, input={"query": QUERY}))

        assert f"[memory:run:{run}:scratch " in context, context


async def test_concurrent_tasks_of_different_runs_each_carry_their_own_run():
    """두 태스크가 같은 클라이언트로 **동시에** 검색한다 — 둘 다 요청이 나간 뒤에야 답을 보낸다."""
    async with watched() as stack:
        first, second = stack.start("run-a"), stack.start("run-b")
        await stack.settled()
        for run in (first, second):
            writer = stack.agent("planner", run_id=run)
            await writer.append("scratch", **fact(f"{QUERY} {run}"))
            indexed(stack)

        app = httpx.ASGITransport(app=stack.memory.app)
        seen: list[str | None] = []
        both_in_flight = asyncio.Event()

        async def barrier(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get(MEMORY_RUN_HEADER))
            if len(seen) == 2:
                both_in_flight.set()
            await both_in_flight.wait()
            return await app.handle_async_request(request)

        credential = stack.registry.issue_identity(
            "planner", "dep-planner-shared", graph="research-pipeline"
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(barrier)) as http:
            shared = HttpMemoryAccess(base_url="http://memory", token=credential, client=http)
            models = {run: FakeModel([search_call(), text("done")]) for run in (first, second)}
            await asyncio.gather(
                *(
                    executor_for(shared, models[run]).execute(
                        make_task(task_id=f"task-{run}", run_id=run)
                    )
                    for run in (first, second)
                )
            )

        assert sorted(seen) == [first, second]
        for run, other in ((first, second), (second, first)):
            spaces = {item["space"] for item in json.loads(tool_result(models[run]))}
            assert f"run:{run}:scratch" in spaces
            assert f"run:{other}:scratch" not in spaces
