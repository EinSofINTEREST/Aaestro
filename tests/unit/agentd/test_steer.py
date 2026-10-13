"""Unit tests for steering a running task at a turn boundary (#364).

진행 중 태스크에 운영자 지시를 넣는 경로 — 실제 LLM 없이 FakeModel 로만 검증한다 (06 규칙).
지시는 **실행 도중에** 도착해야 하므로 tool·모델 대역이 자기 차례에 steer 를 건다: sleep 으로
타이밍을 맞추지 않는다.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from types import SimpleNamespace

import pytest
import structlog

from aaestro.agentd import executor as executor_module
from aaestro.agentd.executor import (
    MAX_PENDING_STEERS,
    MAX_STEER_BY_CHARS,
    MAX_STEER_CHARS,
    STEER_HEADER,
    AssistantTurn,
    Executor,
    ExecutorConfig,
    ToolTurn,
    UserTurn,
)
from aaestro.core.agent import TaskConfig, TaskKey, TaskStatus
from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.core.events import (
    DoneEvent,
    ErrorEvent,
    SteerEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from tests.fixtures.builders import make_task
from tests.fixtures.fake_model import FakeModel, FakeTools, calls, text

TASK_ID = "task-0001"


@pytest.fixture(autouse=True)
def fresh_logger(monkeypatch):
    """모듈 로거를 테스트마다 새로 물린다.

    structlog 는 설정에 따라 로거를 첫 사용 때 굳힌다 (`cache_logger_on_first_use`). 앞선
    테스트가 이 모듈의 로거를 굳혀 두면 `capture_logs` 가 보지 못한다 — 실행 순서에 기대지 않는다.
    """
    monkeypatch.setattr(executor_module, "log", structlog.get_logger("test"))


class HookedTools(FakeTools):
    """tool 이 도는 동안(= 턴 경계 직전) 훅을 부르는 registry 대역."""

    def __init__(self) -> None:
        super().__init__()
        self.during: dict[str, object] = {}

    async def call(self, name, arguments, ctx):
        hook = self.during.get(name)
        if hook is not None:
            await hook()
        return await super().call(name, arguments, ctx)


class HookedModel(FakeModel):
    """모델 호출이 진행되는 동안 훅을 부르는 모델 대역 — 호출 회차(0-based)별."""

    def __init__(self, responses) -> None:
        super().__init__(responses)
        self.during: dict[int, object] = {}

    async def run(self, system, messages, tools):
        response = await super().run(system, messages, tools)
        hook = self.during.get(len(self.calls) - 1)
        if hook is not None:
            await hook()
        return response


def make_executor(responses, **config) -> tuple[Executor, HookedModel, HookedTools]:
    model = HookedModel(responses)
    tools = HookedTools()
    executor = Executor(
        agent="researcher",
        model=model,
        tools=tools,
        render=lambda task: f"prompt:{task.template_name}",
        config=ExecutorConfig(**config) if config else None,
        system="be careful",
    )
    return executor, model, tools


def steering(executor: Executor, *texts: str, by: str = "alice"):
    """실행 도중에 걸 steer — 훅으로 물린다."""

    async def hook() -> None:
        for body in texts:
            await executor.steer(TASK_ID, body, by=by)

    return hook


def wire(messages) -> bytes:
    """대화를 바이트로 — 모델에 가는 내용이 한 글자도 달라지지 않았는지 보는 용도."""
    return json.dumps(
        [{"role": type(m).__name__, **dataclasses.asdict(m)} for m in messages],
        sort_keys=True,
        ensure_ascii=False,
    ).encode()


def steer_turn(*texts: str) -> UserTurn:
    return UserTurn(parts=tuple(f"{STEER_HEADER}\n{body}" for body in texts))


# --- 턴 경계 반영 ------------------------------------------------------------


async def test_steer_after_a_tool_turn_is_the_last_message_of_the_next_call():
    executor, model, tools = make_executor([calls("search"), text("done")])
    tools.during["search"] = steering(executor, "look at the 2024 data instead")

    result = await executor.execute(make_task())

    assert result.status is TaskStatus.COMPLETED
    _system, messages, _tools = model.requests[1]
    assert [type(m) for m in messages] == [UserTurn, AssistantTurn, ToolTurn, UserTurn]
    assert messages[-1] == steer_turn("look at the 2024 data instead")


async def test_steer_goes_to_the_conversation_not_the_system_prompt():
    """system 을 바꾸면 그 태스크의 프롬프트 캐시 prefix 가 깨진다."""
    executor, model, tools = make_executor([calls("search"), text("done")])
    tools.during["search"] = steering(executor, "change course")

    await executor.execute(make_task())

    assert [system for system, _m, _t in model.requests] == ["be careful", "be careful"]


async def test_two_steers_at_one_boundary_merge_into_a_single_user_turn():
    executor, model, tools = make_executor([calls("search"), text("done")])
    tools.during["search"] = steering(executor, "first", "second")

    await executor.execute(make_task())

    _system, messages, _tools = model.requests[1]
    assert messages[-1] == steer_turn("first", "second")
    roles = [type(m) for m in messages]
    assert all(
        not (a is UserTurn and b is UserTurn) for a, b in zip(roles, roles[1:], strict=False)
    )


async def test_steer_during_a_model_call_waits_for_the_boundary():
    """모델 호출 도중에는 끼어들지 않는다 — 그 호출이 받은 메시지도 고쳐 쓰지 않는다."""
    executor, model, _tools = make_executor([calls("search"), calls("fetch"), text("done")])
    model.during[0] = steering(executor, "mid-call")

    await executor.execute(make_task())

    first, second, third = (messages for _s, messages, _t in model.requests)
    assert first == (UserTurn(parts=("prompt:planner",)),)
    assert second[-1] == steer_turn("mid-call")
    # 이미 보낸 것은 그대로 prefix 로 남는다 — 뒤에만 붙는다
    assert third[: len(second)] == second
    assert sum(1 for m in third if m == steer_turn("mid-call")) == 1


async def test_steers_at_separate_boundaries_stay_in_their_own_turns():
    executor, model, tools = make_executor([calls("search"), calls("fetch"), text("done")])
    tools.during["search"] = steering(executor, "one")
    tools.during["fetch"] = steering(executor, "two")

    await executor.execute(make_task())

    _system, messages, _tools = model.requests[2]
    assert [type(m) for m in messages] == [
        UserTurn,
        AssistantTurn,
        ToolTurn,
        UserTurn,
        AssistantTurn,
        ToolTurn,
        UserTurn,
    ]
    assert (messages[3], messages[6]) == (steer_turn("one"), steer_turn("two"))


# --- steer 가 없을 때 ---------------------------------------------------------

# 변경 전(steer 도입 전) 실행기가 같은 시나리오에서 모델에 보낸 messages 의 직렬화 — 고정값이다.
# 이 값이 달라지면 steer 를 쓰지 않는 모든 태스크의 프롬프트(와 캐시 prefix)가 달라진 것이다.
_OPENING = '{"parts": ["prompt:planner"], "role": "UserTurn"}'
_FIRST_TURN = (
    '{"content": "", "raw": null, "role": "AssistantTurn", "tool_calls": '
    '[{"arguments": {}, "id": "c0", "name": "search"}, '
    '{"arguments": {}, "id": "c1", "name": "fetch"}]}, '
    '{"outcomes": [{"call_id": "c0", "content": "found", "is_error": false}, '
    '{"call_id": "c1", "content": "{\\"n\\": 1}", "is_error": false}], "role": "ToolTurn"}'
)
_SECOND_TURN = (
    '{"content": "", "raw": null, "role": "AssistantTurn", "tool_calls": '
    '[{"arguments": {}, "id": "c0", "name": "search"}]}, '
    '{"outcomes": [{"call_id": "c0", "content": "found", "is_error": false}], '
    '"role": "ToolTurn"}'
)
MESSAGES_BEFORE_STEER_EXISTED = [
    f"[{_OPENING}]".encode(),
    f"[{_OPENING}, {_FIRST_TURN}]".encode(),
    f"[{_OPENING}, {_FIRST_TURN}, {_SECOND_TURN}]".encode(),
]


async def test_without_a_steer_the_messages_are_byte_identical_to_before():
    executor, model, tools = make_executor(
        [calls("search", "fetch"), calls("search"), text("done")]
    )
    tools.script("search", result="found").script("fetch", result={"n": 1})

    await executor.execute(make_task())

    assert [wire(messages) for _s, messages, _t in model.requests] == MESSAGES_BEFORE_STEER_EXISTED


async def test_without_a_steer_no_steer_event_is_emitted():
    executor, _model, _tools = make_executor([calls("search"), text("done")])

    events = [event async for event in executor.stream(make_task())]

    assert not any(isinstance(event, SteerEvent) for event in events)


# --- 이벤트 ------------------------------------------------------------------


async def test_stream_reports_the_steer_at_the_boundary_it_was_applied():
    executor, _model, tools = make_executor([calls("search"), text("done")])
    tools.during["search"] = steering(executor, "narrow it down", by="alice")

    events = [event async for event in executor.stream(make_task())]

    assert [type(event) for event in events] == [
        ToolCallEvent,
        ToolResultEvent,
        SteerEvent,
        TokenEvent,
        DoneEvent,
    ]
    steer = events[2]
    assert (steer.text, steer.by, steer.turn) == ("narrow it down", "alice", 0)


async def test_each_merged_steer_gets_its_own_event():
    executor, _model, tools = make_executor([calls("search"), text("done")])
    tools.during["search"] = steering(executor, "first", "second")

    events = [event async for event in executor.stream(make_task())]

    assert [e.text for e in events if isinstance(e, SteerEvent)] == ["first", "second"]


# --- 진행 중이 아닌 태스크 ----------------------------------------------------


async def test_steer_for_an_unknown_task_is_not_found():
    executor, _model, _tools = make_executor([text("done")])

    with pytest.raises(AaestroError) as exc_info:
        await executor.steer("never-started", "hello", by="alice")

    assert exc_info.value.code == ErrorCode.NF_001


async def test_steer_for_a_finished_task_is_not_found():
    executor, _model, _tools = make_executor([text("done")])
    await executor.execute(make_task())

    with pytest.raises(AaestroError) as exc_info:
        await executor.steer(TASK_ID, "too late", by="alice")

    assert exc_info.value.code == ErrorCode.NF_001


async def test_a_replayed_finished_task_does_not_reopen_for_steering():
    """같은 task_id 재호출은 캐시된 결과를 돌려줄 뿐이다 — 루프가 돌지 않으니 받을 곳도 없다."""
    executor, model, _tools = make_executor([text("done")])
    await executor.execute(make_task())
    await executor.execute(make_task())

    with pytest.raises(AaestroError) as exc_info:
        await executor.steer(TASK_ID, "too late", by="alice")

    assert exc_info.value.code == ErrorCode.NF_001
    assert model.turns == 1


async def test_the_stream_consumer_cannot_steer_once_it_has_seen_done():
    """done 을 본 뒤의 steer 가 접수됐다가 사라지면 안 된다 — 종료 이벤트 전에 닫는다."""
    executor, _model, _tools = make_executor([text("done")])
    refused: list[str] = []

    async for event in executor.stream(make_task()):
        if isinstance(event, DoneEvent):
            with pytest.raises(AaestroError) as exc_info:
                await executor.steer(TASK_ID, "one more thing", by="alice")
            refused.append(exc_info.value.code)

    assert refused == [ErrorCode.NF_001]


# --- 반영되지 못한 steer ------------------------------------------------------


async def test_a_steer_that_arrives_during_the_final_model_call_is_dropped_loudly():
    """ "queued" 로 답한 지시가 흔적 없이 사라지면 운영자는 반영됐다고 믿는다."""
    executor, model, _tools = make_executor([text("done")])
    model.during[0] = steering(executor, "SECRET-STEER-BODY", by="alice")

    with structlog.testing.capture_logs() as logs:
        result = await executor.execute(make_task())

    assert result.status is TaskStatus.COMPLETED
    dropped = [entry for entry in logs if entry["event"].startswith("steer dropped")]
    assert len(dropped) == 1
    assert dropped[0]["log_level"] == "warning"
    assert (dropped[0]["agent"], dropped[0]["task_id"]) == ("researcher", TASK_ID)
    assert (dropped[0]["dropped"], dropped[0]["issuers"]) == (1, ["alice"])
    assert dropped[0]["run_id"] == "run-0001"


async def test_a_steer_on_the_last_allowed_turn_is_not_reported_as_applied():
    """다음 모델 호출이 없는 회차에서 꺼내면 반영된 척하게 된다."""
    executor, _model, tools = make_executor([calls("search")], max_turns=1)
    tools.during["search"] = steering(executor, "no next turn")

    with structlog.testing.capture_logs() as logs:
        events = [event async for event in executor.stream(make_task())]

    assert not any(isinstance(event, SteerEvent) for event in events)
    assert isinstance(events[-1], ErrorEvent) and events[-1].error.code == ErrorCode.LLM_005
    assert [entry["dropped"] for entry in logs if entry["event"].startswith("steer dropped")] == [1]


async def test_steer_text_never_reaches_the_logs():
    """본문은 운영자 입력이다 — 로그에는 길이만 남긴다."""
    executor, _model, tools = make_executor([calls("search"), text("done")])
    tools.during["search"] = steering(executor, "SECRET-STEER-BODY", by="alice")

    with structlog.testing.capture_logs() as logs:
        await executor.execute(make_task())

    assert "SECRET-STEER-BODY" not in json.dumps(logs, default=str)
    queued = next(entry for entry in logs if entry["event"] == "steer queued")
    assert (queued["text_chars"], queued["steered_by"]) == (len("SECRET-STEER-BODY"), "alice")
    assert (queued["agent"], queued["task_id"]) == ("researcher", TASK_ID)
    assert queued["run_id"] == "run-0001"
    applied = next(entry for entry in logs if entry["event"] == "steer applied")
    assert (applied["run_id"], applied["issuers"], applied["applied"]) == ("run-0001", ["alice"], 1)


async def test_one_log_key_keeps_one_type():
    """`steered_by` 가 어떤 줄에서는 문자열, 어떤 줄에서는 목록이면 로그 질의가 깨진다."""
    executor, _model, tools = make_executor([calls("search"), text("done")])
    tools.during["search"] = steering(executor, "go", by="alice")

    with structlog.testing.capture_logs() as logs:
        await executor.execute(make_task())

    assert {type(entry["steered_by"]) for entry in logs if "steered_by" in entry} == {str}
    assert {type(entry["issuers"]) for entry in logs if "issuers" in entry} == {list}


# --- 대기열 수명 --------------------------------------------------------------


async def test_the_queue_is_gone_once_the_task_completes():
    executor, _model, tools = make_executor([calls("search"), text("done")])
    tools.during["search"] = steering(executor, "note")

    await executor.execute(make_task())

    assert executor._steers == {}


async def test_the_queue_is_gone_once_the_task_fails():
    executor, _model, tools = make_executor([calls("search"), text("done")])
    tools.fail("search", RuntimeError("boom"))
    tools.during["search"] = steering(executor, "note")

    result = await executor.execute(make_task())

    assert result.status is TaskStatus.FAILED
    assert executor._steers == {}


async def test_the_queue_is_gone_once_the_task_times_out():
    executor, _model, tools = make_executor([calls("search"), text("done")])
    tools.script("search", delay=3600)

    result = await executor.execute(make_task(config=TaskConfig(timeout_s=0.01)))

    assert result.error is not None and result.error.code == ErrorCode.TO_001
    assert executor._steers == {}


async def test_the_queue_is_gone_once_the_task_is_cancelled():
    executor, _model, tools = make_executor([calls("search"), text("done")])
    started = asyncio.Event()

    async def block() -> None:
        started.set()
        await asyncio.Event().wait()

    tools.during["search"] = block
    running = asyncio.create_task(executor.execute(make_task()))
    await started.wait()
    await executor.steer(TASK_ID, "never applied", by="alice")

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert executor._steers == {}


async def test_the_queue_is_gone_when_the_stream_consumer_walks_away():
    executor, _model, _tools = make_executor([calls("search"), text("done")])

    stream = executor.stream(make_task())
    await anext(stream)
    assert TaskKey.from_control(TASK_ID) in executor._steers
    await stream.aclose()

    assert executor._steers == {}


async def test_a_peer_task_with_the_same_id_is_not_reachable_by_an_operator_steer():
    """steer 는 Control API 공간의 태스크에만 닿는다 — 대기열 키는 (호출자, task_id) 다 (#400)."""
    executor, _model, tools = make_executor([calls("search"), text("done")])
    started = asyncio.Event()

    async def block() -> None:
        started.set()
        await asyncio.Event().wait()

    tools.during["search"] = block
    peer_task = make_task().model_copy(update={"caller": "peer"})
    running = asyncio.create_task(executor.execute(peer_task))
    await started.wait()

    with pytest.raises(AaestroError) as exc_info:
        await executor.steer(TASK_ID, "meant for someone else", by="alice")

    assert exc_info.value.code == ErrorCode.NF_001
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert executor._steers == {}


async def test_many_tasks_leave_nothing_behind():
    """상주 컨테이너는 수명이 길다 — 태스크마다 항목이 남으면 끝없이 자란다 (#315 와 같은 부류)."""
    executor, _model, _tools = make_executor([text("done")])

    for index in range(50):
        await executor.execute(make_task(task_id=f"task-{index}"))

    assert executor._steers == {}


async def test_a_stream_that_announced_its_timeout_no_longer_takes_steers():
    """끝났다고 알린 태스크가 steer 를 접수하면 "queued" 가 거짓말이 된다.

    느린 소비자 — 이벤트 하나를 받고 마감을 넘긴 뒤 다음 이벤트를 청한다. 실행기는 그때
    timeout 을 알리는데, 알리기 **전에** 대기열을 닫아야 한다.
    """
    executor, _model, _tools = make_executor([calls("search"), calls("fetch"), text("done")])
    refused: list[str] = []
    seen: list[type] = []

    async for event in executor.stream(make_task(config=TaskConfig(timeout_s=0.01))):
        seen.append(type(event))
        if isinstance(event, ErrorEvent):
            assert event.error.code == ErrorCode.TO_001
            with pytest.raises(AaestroError) as exc_info:
                await executor.steer(TASK_ID, "too late", by="alice")
            refused.append(exc_info.value.code)
        elif len(seen) == 1:
            await asyncio.sleep(0.03)

    assert seen[-1] is ErrorEvent
    assert refused == [ErrorCode.NF_001]


# --- 같은 task_id 가 겹쳐 돌 때 ------------------------------------------------


class Overlap:
    """같은 ``task_id`` 로 겹쳐 도는 두 실행 — 먼저 것은 tool 에서 붙잡혀 있다.

    A2A 로 받은 태스크의 ``task_id`` 는 peer 가 정한다: 진행 중인 id 를 골라 보낼 수 있다.
    """

    def __init__(self) -> None:
        self.first_model = HookedModel([calls("search"), text("first done")])
        self.second_model = HookedModel([calls("fetch"), text("second done")])
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        opening = {"first": self.first_model, "second": self.second_model}

        async def by_prompt(system, messages, tools):
            """한 실행기 안에서 두 실행을 구분한다 — 태스크 입력으로 모델을 고른다."""
            return await opening[messages[0].parts[-1]].run(system, messages, tools)

        self.tools = HookedTools()
        self.tools.during["search"] = self._hold
        self.executor = Executor(
            agent="researcher",
            model=SimpleNamespace(run=by_prompt),
            tools=self.tools,
            render=lambda task: task.input["who"],
        )

    async def _hold(self) -> None:
        self.entered.set()
        await self.release.wait()

    async def start_first(self) -> asyncio.Task:
        running = asyncio.create_task(self.executor.execute(make_task(input={"who": "first"})))
        await self.entered.wait()
        return running

    def second(self):
        return self.executor.execute(make_task(input={"who": "second"}))


async def test_a_later_run_with_the_same_task_id_cannot_take_over_the_steers():
    """나중 실행이 창구를 덮어쓰면 peer 가 운영자 지시를 자기 태스크의 대화로 가로챈다."""
    overlap = Overlap()
    first = await overlap.start_first()
    overlap.tools.during["fetch"] = steering(overlap.executor, "for the first run only")

    await overlap.second()
    overlap.release.set()
    await first

    assert overlap.first_model.requests[1][1][-1] == steer_turn("for the first run only")
    assert all(
        not isinstance(m, UserTurn) or m.parts == ("second",)
        for m in overlap.second_model.requests[1][1]
    )


async def test_a_later_run_finishing_first_does_not_close_the_running_ones_queue():
    """악의 없는 재시도도 겹친다 — 먼저 끝난 쪽이 아직 도는 실행의 창구를 지우면 안 된다."""
    overlap = Overlap()
    first = await overlap.start_first()

    await overlap.second()
    await overlap.executor.steer(TASK_ID, "still running", by="alice")
    overlap.release.set()
    await first

    assert overlap.first_model.requests[1][1][-1] == steer_turn("still running")
    assert overlap.executor._steers == {}


# --- 상한 --------------------------------------------------------------------


async def run_with_steer(text_: str, *, by: str = "alice", times: int = 1) -> list[str]:
    """실행 도중 steer 를 걸어 보고, 거절된 코드들을 돌려준다."""
    executor, _model, tools = make_executor([calls("search"), text("done")])
    refused: list[str] = []

    async def hook() -> None:
        for _ in range(times):
            try:
                await executor.steer(TASK_ID, text_, by=by)
            except AaestroError as err:
                refused.append(err.code)

    tools.during["search"] = hook
    await executor.execute(make_task())
    return refused


@pytest.mark.parametrize(
    ("body", "by"),
    [
        ("", "alice"),
        ("   \n", "alice"),
        ("x" * (MAX_STEER_CHARS + 1), "alice"),
        ("go", ""),
        ("go", "a" * (MAX_STEER_BY_CHARS + 1)),
        ("go", "alice\n2026-01-01 [info] forged line"),
        ("go", "alice\x00"),
        ("go", "alice\x1b[2K"),
    ],
)
async def test_a_malformed_steer_is_refused_with_val_002(body, by):
    assert await run_with_steer(body, by=by) == [ErrorCode.VAL_002]


@pytest.mark.parametrize(
    ("body", "by"),
    [
        ("x" * MAX_STEER_CHARS, "alice"),
        ("go", "a" * MAX_STEER_BY_CHARS),
        ("go", "김 운영자 <ops@example.com>"),
        ("여러 줄\n지시도\n받는다", "alice"),
    ],
)
async def test_a_steer_at_the_limit_is_accepted(body, by):
    """과잉 거절도 결함이다 — 상한 바로 그 값은 받는다."""
    assert await run_with_steer(body, by=by) == []


async def test_only_the_steers_over_the_pending_limit_are_refused():
    refused = await run_with_steer("go", times=MAX_PENDING_STEERS + 2)

    assert refused == [ErrorCode.VAL_002, ErrorCode.VAL_002]


async def test_the_pending_limit_frees_up_at_each_boundary():
    """상한은 **대기 중**인 수다 — 반영되면 다시 받는다."""
    executor, model, tools = make_executor([calls("search"), calls("fetch"), text("done")])
    tools.during["search"] = steering(executor, *["go"] * MAX_PENDING_STEERS)
    tools.during["fetch"] = steering(executor, "again")

    result = await executor.execute(make_task())

    assert result.status is TaskStatus.COMPLETED
    assert model.requests[2][1][-1] == steer_turn("again")
