"""Model fallback — boundaries around the chain (#359).

``test_model_fallback.py`` 가 체인 자체를 덮는다. 여기서는 체인이 **다른 기능과 만나는 경계**를
본다: 스트리밍, 멀티턴, 멱등 캐시, 취소, HTTP 직렬화, retry 선언.

06 에 따라 실제로 자지 않는다 — 재시도 대기와 breaker 시계를 주입한다.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from prometheus_client import CollectorRegistry

from malkuth.agentd.executor import (
    AssistantTurn,
    Executor,
    ExecutorConfig,
    ExecutorServices,
    ToolTurn,
    UserTurn,
)
from malkuth.agentd.models import ModelChains
from malkuth.agentd.server import AgentRuntime, create_app
from malkuth.core.agent import TaskConfig, TaskStatus
from malkuth.core.errors import NETWORK_RETRY, RATE_LIMIT_RETRY, ErrorCategory, ErrorCode
from malkuth.core.events import DoneEvent, ErrorEvent
from malkuth.core.manifest import CircuitDeclaration, ModelRetry, RetryDeclaration
from malkuth.observability.metrics import Metrics
from malkuth.runtime.control import ControlClient
from tests.fixtures.builders import make_task
from tests.fixtures.fake_model import FakeModel, FakeTools, calls, text
from tests.unit.agentd.test_model_fallback import (
    AGENT,
    ATTEMPTS,
    PROVIDER,
    Clock,
    Rig,
    backup,
    declared,
    model_error,
    rate_limited,
)

BASE_URL = "http://agent-researcher:8080"
TOKEN = "agent-token"  # noqa: S105


# --- 1. 스트리밍 ---------------------------------------------------------------


async def test_stream_falls_back_like_execute_and_reports_the_answering_model():
    """``/v1/stream`` 도 같은 체인을 탄다 — 이벤트의 usage 가 fallback 모델을 가리킨다.

    기본 체인은 선언이 없고, 태스크가 실어 온 선언으로만 fallback 이 생긴다. 스트리밍 경로에서
    그 선언이 읽히지 않으면 primary 의 rate limit 이 그대로 에러 이벤트가 된다.
    """

    def build() -> Rig:
        return Rig(
            {
                "primary": FakeModel([text("unused")]),
                "override": FakeModel([rate_limited()]),
                "spare": FakeModel([text("from spare", input_tokens=4, output_tokens=2)]),
            },
            declared(),
        )

    task_model = declared(backup("rate_limit", name="spare"), name="override")
    executed = await build().executor.execute(make_task(config=TaskConfig(model=task_model)))

    streamed_rig = build()
    events = [
        event
        async for event in streamed_rig.executor.stream(
            make_task(config=TaskConfig(model=task_model))
        )
    ]

    done = events[-1]
    assert isinstance(done, DoneEvent), events
    assert done.output == executed.output == {"content": "from spare"}
    assert done.usage == executed.usage
    assert done.usage.model == "spare"
    assert streamed_rig.fakes["primary"].turns == 0
    assert streamed_rig.fakes["override"].turns == ATTEMPTS


async def test_stream_without_a_matching_trigger_ends_in_the_primary_error_event():
    spare = FakeModel([text("unused")])
    rig = Rig(
        {"primary": FakeModel([rate_limited()]), "spare": spare},
        declared(backup("server_error", name="spare")),
    )

    events = [event async for event in rig.executor.stream(make_task())]

    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].error.code == ErrorCode.LLM_001
    assert spare.turns == 0


# --- 2. 멀티턴 -----------------------------------------------------------------


async def test_a_mid_task_switch_sends_the_same_conversation_and_sums_both_turns():
    """턴 2 에서 전환해도 fallback 은 턴 1 의 assistant/tool 턴을 포함한 같은 대화를 받는다."""
    primary = FakeModel([calls("search", input_tokens=5, output_tokens=2), rate_limited()])
    fallback = FakeModel([text("final", input_tokens=7, output_tokens=3)])
    rig = Rig({"primary": primary, "backup": fallback}, declared(backup("rate_limit")))

    result = await rig.executor.execute(make_task())

    assert result.status is TaskStatus.COMPLETED
    assert primary.turns == 1 + ATTEMPTS
    assert fallback.turns == 1
    system, messages, tools = fallback.requests[0]
    assert (system, messages, tools) == primary.requests[1]
    assert [type(message) for message in messages] == [UserTurn, AssistantTurn, ToolTurn]
    assert messages[1].tool_calls[0].name == "search"
    assert result.usage.model == "backup"
    assert result.usage.input_tokens == 5 + 7
    assert result.usage.output_tokens == 2 + 3


# --- 3. 멱등 캐시 --------------------------------------------------------------


async def test_a_replayed_task_returns_the_fallback_result_without_calling_any_model():
    primary = FakeModel([rate_limited()])
    fallback = FakeModel([text("from backup")])
    rig = Rig({"primary": primary, "backup": fallback}, declared(backup("rate_limit")))
    task = make_task(task_id="idem-fallback")

    first = await rig.executor.execute(task)
    second = await rig.executor.execute(task)

    assert second == first
    assert second.usage.model == "backup"
    assert (primary.turns, fallback.turns) == (ATTEMPTS, 1)


# --- 4. 취소 -------------------------------------------------------------------


class Blocking:
    """호출이 시작되면 알리고 풀어 주지 않는 모델 — 취소만이 끝낸다."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self, system, messages, tools):
        self.started.set()
        await asyncio.Event().wait()


async def test_cancelling_during_the_fallback_call_propagates_and_cleans_up():
    cleaned = asyncio.Event()
    blocked = Blocking()
    chains = ModelChains(
        agent=AGENT,
        factory=lambda config: {"primary": FakeModel([rate_limited()]), "backup": blocked}[
            config.name
        ],
    )
    waits: list[float] = []

    async def sleep(delay: float) -> None:
        waits.append(delay)

    executor = Executor(
        agent=AGENT,
        model=chains.for_config(declared(backup("rate_limit"))),
        tools=FakeTools(),
        render=lambda _task: "prompt",
        config=ExecutorConfig(retry_sleep=sleep),
        services=ExecutorServices(on_cleanup=cleaned.set),
    )

    task = asyncio.create_task(executor.execute(make_task()))
    await asyncio.wait_for(blocked.started.wait(), timeout=5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    assert len(waits) == ATTEMPTS - 1


# --- 5. HTTP 왕복 + 체인 ---------------------------------------------------------


async def test_a_serialized_declaration_drives_the_chain_inside_agentd():
    """직렬화를 건넌 선언(fallbacks·retry·circuit)으로 서버 쪽 Executor 가 체인을 만든다.

    같은 프로세스에서 객체만 넘기면 필드가 빠져도 초록이다 — 실제 앱을 ASGI 로 태운다.
    """
    fakes = {
        "primary": FakeModel([text("manifest default")]),
        "override": FakeModel([rate_limited()]),
        "spare": FakeModel([text("from spare", input_tokens=3)]),
    }
    built: list[str] = []
    waits: list[float] = []

    def factory(config):
        built.append(config.name)
        return fakes[config.name]

    async def sleep(delay: float) -> None:
        waits.append(delay)

    chains = ModelChains(
        agent=AGENT,
        factory=factory,
        metrics=Metrics(registry=CollectorRegistry()),
        clock=Clock(),
    )
    executor = Executor(
        agent=AGENT,
        model=chains.for_config(declared(name="primary")),
        tools=FakeTools(),
        render=lambda _task: "prompt",
        config=ExecutorConfig(retry_sleep=sleep),
        services=ExecutorServices(chains=chains.for_config),
    )
    app = create_app(AgentRuntime(agent=AGENT, executor=executor), token=TOKEN)
    # retry.rate_limit.attempts=2 는 기본(5)과 다르다 — 선언이 건너왔다면 대기는 한 번이다
    model = declared(
        backup("rate_limit", name="spare"),
        name="override",
        circuit=CircuitDeclaration(max_failures=3, reset_s=20),
        retry=ModelRetry(rate_limit=RetryDeclaration(start_s=2, attempts=2, max_s=30)),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as transport:
        client = ControlClient(BASE_URL, agent=AGENT, token=TOKEN, client=transport)
        result = await client.invoke(make_task(config=TaskConfig(model=model)))

    assert result.status is TaskStatus.COMPLETED
    assert result.output == {"content": "from spare"}
    assert result.usage.model == "spare"
    assert fakes["primary"].turns == 0, "manifest 기본 모델이 불렸다"
    assert fakes["override"].turns == 2
    assert len(waits) == 1
    assert waits[0] <= 2
    assert built.count("override") == 1 and built.count("spare") == 1
    assert (PROVIDER, "override") in chains._breakers
    assert chains._breakers[(PROVIDER, "override")].max_failures == 3


# --- 6. retry 선언 경계 ----------------------------------------------------------


def only_network_declared(**extra: object):
    return declared(retry=ModelRetry(network=RetryDeclaration(start_s=0.5, attempts=2)), **extra)


async def test_a_declared_network_retry_does_not_change_the_rate_limit_schedule():
    """``retry.network`` 만 선언했다면 rate limit 은 기본 정책을 쓴다."""
    rig = Rig({"primary": FakeModel([rate_limited()])}, only_network_declared())
    waits: list[float] = []

    async def sleep(delay: float) -> None:
        waits.append(delay)

    rig.executor._config = ExecutorConfig(retry_sleep=sleep)
    result = await rig.executor.execute(make_task())

    assert result.error is not None
    assert result.error.code == ErrorCode.LLM_001
    assert rig.fakes["primary"].turns == RATE_LIMIT_RETRY.max_attempts
    assert len(waits) == RATE_LIMIT_RETRY.max_attempts - 1
    for attempt, wait in enumerate(waits, start=1):
        assert wait <= RATE_LIMIT_RETRY.delay_for(attempt)


async def test_a_declared_network_retry_follows_the_declaration():
    unreachable = model_error(ErrorCategory.NETWORK, ErrorCode.NET_001)
    rig = Rig({"primary": FakeModel([unreachable])}, only_network_declared())

    result = await rig.executor.execute(make_task())

    assert result.error is not None
    assert result.error.code == ErrorCode.NET_001
    # 선언 attempts=2 (기본은 NETWORK_RETRY.max_attempts)
    assert NETWORK_RETRY.max_attempts != 2
    assert rig.fakes["primary"].turns == 2
    assert len(rig.waits) == 1
    assert rig.waits[0] <= 0.5
