"""Every A2A request other than a new-task send is refused (#414).

SDK 핸들러는 태스크 조회·목록·구독·취소·push 설정을 **호출자 확인 없이** 연다. 프레임워크가
쓰는 것은 새 태스크를 보내는 두 요청뿐이므로 나머지는 전부 거절한다.

두 층으로 본다:

- **구조** — SDK 핸들러의 ``on_*`` 를 전수 열거해 하나하나가 검사를 거치거나 거절하는지.
  SDK 가 메서드를 추가하면 여기서 빨개진다 (기본은 거절)
- **실제 SDK 경로** — JSON-RPC 를 ASGI 로 직접 보내, 응답이 태스크에 대해 아무것도 말하지
  않는지. SDK 쪽 기능(push · 확장 카드)을 **전부 켠** 서버로 본다: 꺼 둔 기능은 SDK 가 알아서
  거절하므로 우리 거절을 지워도 통과한다
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from a2a.server.agent_execution.active_task import TERMINAL_TASK_STATES
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
from a2a.server.routes.jsonrpc_dispatcher import JsonRpcDispatcher
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryPushNotificationConfigStore, InMemoryTaskStore
from a2a.types import a2a_pb2 as pb
from a2a.utils.errors import A2AError, UnsupportedOperationError
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict
from structlog.testing import capture_logs

from aaestro.core.agent import TaskResult, TraceContext
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.protocols.a2a.allowlist import Edge, issue_token
from aaestro.protocols.a2a.client import A2AServer
from aaestro.protocols.a2a.errors import not_allowed
from aaestro.protocols.a2a.sdk import CALLER_HEADER, TOKEN_HEADER, build_message
from aaestro.protocols.a2a.server import (
    GuardedExecutor,
    GuardedRequestHandler,
    InboundGuard,
    RunningTaskStore,
)
from aaestro.protocols.a2a.tickets import TICKET_HEADER, TicketSource
from tests.fixtures.builders import make_task
from tests.unit.protocols.a2a.test_registry_mode import Stack, _allowlist
from tests.unit.protocols.a2a.test_sdk import (
    BASE_URL,
    CALLEE,
    CALLER,
    SECRET,
    guard_for,
    peer_card,
    valid_headers,
)

OTHER_CALLER = "writer"
MARKER = "the-plan-only-its-caller-may-read"
"""태스크 산출물에 심는 표식 — 응답 어디에도 나오면 안 된다."""

UNSUPPORTED = -32004
"""SDK 의 ``UnsupportedOperationError`` 가 JSON-RPC 로 나가는 코드."""

ADMITTED = {"on_message_send", "on_message_send_stream"}
"""검사를 거쳐 여는 요청 — 프레임워크 클라이언트(``sdk.SdkPeerTransport``)가 쓰는 전부."""

REFUSED = {
    "on_get_task",
    "on_list_tasks",
    "on_cancel_task",
    "on_subscribe_to_task",
    "on_create_task_push_notification_config",
    "on_get_task_push_notification_config",
    "on_list_task_push_notification_configs",
    "on_delete_task_push_notification_config",
    "on_get_extended_agent_card",
}


def sdk_request_methods() -> set[str]:
    """SDK 요청 핸들러(기반 클래스 포함)가 여는 공개 요청 메서드 전부."""
    return {
        name
        for name in dir(DefaultRequestHandler)
        if name.startswith("on_") and callable(getattr(DefaultRequestHandler, name))
    }


# --- 구조: SDK 의 on_* 전수 ---------------------------------------------------------


def test_every_sdk_request_method_is_classified():
    """SDK 가 요청 메서드를 추가하면 여기서 멈춘다 — 분류하지 않은 요청이 열린 채 나가지 않게."""
    assert sdk_request_methods() == ADMITTED | REFUSED


@pytest.mark.parametrize("name", sorted(sdk_request_methods()))
def test_every_sdk_request_method_is_overridden_by_the_guarded_handler(name):
    """재정의가 빠진 메서드는 SDK 기본 구현이 그대로 답한다 — 그것이 #414 였다."""
    assert name in vars(GuardedRequestHandler)


class StubGuard:
    """입구 검사 대역 — 물어본 횟수를 세고, 거부하거나 확인된 호출자를 돌려준다."""

    def __init__(self, *, allow: bool) -> None:
        self.server = SimpleNamespace(agent=CALLEE)
        self.allow = allow
        self.asked = 0

    async def admit(self, headers: Any) -> str | None:
        self.asked += 1
        if not self.allow:
            raise not_allowed("unknown", CALLEE, owner=CALLEE, reason="no ticket")
        return CALLER


@pytest.fixture
def reached(monkeypatch) -> list[str]:
    """SDK 구현에 닿은 요청 — 닿았다면 그 요청은 열린 것이다."""
    hits: list[str] = []

    def stand_in(name: str, *, streaming: bool) -> Any:
        async def answer(self, params, context):
            hits.append(name)

        async def stream(self, params, context):
            hits.append(name)
            yield name

        return stream if streaming else answer

    for name in sdk_request_methods():
        original = inspect.unwrap(getattr(DefaultRequestHandler, name))
        monkeypatch.setattr(
            DefaultRequestHandler,
            name,
            stand_in(name, streaming=inspect.isasyncgenfunction(original)),
        )
    return hits


def guarded(guard: Any) -> GuardedRequestHandler:
    async def unreachable(task):  # pragma: no cover - SDK 구현을 대역으로 바꿔 실행기에 닿지 않는다
        raise AssertionError("executor reached")

    return GuardedRequestHandler(
        guard=guard,
        agent_executor=GuardedExecutor(guard, unreachable),
        agent_card=peer_card(),
    )


async def drive(call: Any) -> Any:
    """요청 하나를 끝까지 돌린다 — 스트림은 소비해야 본문이 실행된다."""
    if inspect.isasyncgen(call):
        return [event async for event in call]
    return await call


def call_context() -> Any:
    return SimpleNamespace(state={"headers": {}})


@pytest.mark.parametrize("name", sorted(sdk_request_methods()))
async def test_no_request_reaches_the_sdk_when_the_check_refuses(name, reached):
    guard = StubGuard(allow=False)
    handler = guarded(guard)

    with pytest.raises(A2AError):
        await drive(getattr(handler, name)(pb.SendMessageRequest(), call_context()))

    assert reached == []
    if name in ADMITTED:
        assert guard.asked == 1, "새 태스크 요청이 입구 검사를 묻지 않았다"


@pytest.mark.parametrize("name", sorted(sdk_request_methods()))
async def test_a_passing_check_opens_only_new_task_sends(name, reached):
    """자격이 유효해도 열리는 것은 새 태스크 요청뿐이다 — 기본은 거절."""
    handler = guarded(StubGuard(allow=True))
    call = getattr(handler, name)(pb.SendMessageRequest(), call_context())

    if name in ADMITTED:
        await drive(call)
        assert reached == [name]
        return
    with pytest.raises(UnsupportedOperationError):
        await drive(call)
    assert reached == []


@pytest.mark.parametrize("name", sorted(ADMITTED))
async def test_a_message_for_an_existing_task_is_refused_before_the_sdk_looks_it_up(name, reached):
    """기존 태스크에 잇는 메시지는 조회와 같은 창구다 — SDK 가 "없음" 과 "이미 끝남" 을 가린다."""
    handler = guarded(StubGuard(allow=True))
    continuing = pb.SendMessageRequest(message=pb.Message(task_id="someone-elses-task"))

    with pytest.raises(UnsupportedOperationError):
        await drive(getattr(handler, name)(continuing, call_context()))

    assert reached == []


# --- 실제 SDK 경로: JSON-RPC 를 직접 보낸다 ---------------------------------------


def open_card() -> pb.AgentCard:
    """SDK 가 스스로 거절할 이유를 없앤 카드 — 남는 거절은 전부 우리 것이다."""
    card = peer_card()
    card.capabilities.push_notifications = True
    card.capabilities.extended_agent_card = True
    return card


async def plan(task):
    return TaskResult.completed(task, output={"plan": MARKER})


def serve_open(guard: InboundGuard, handle: Any = plan) -> FastAPI:
    """SDK 쪽 기능을 전부 켠 수신 앱 — 실제 SDK 라우트."""
    card = open_card()
    handler = GuardedRequestHandler(
        guard=guard,
        agent_executor=GuardedExecutor(guard, handle),
        push_config_store=InMemoryPushNotificationConfigStore(),
        agent_card=card,
        extended_agent_card=card,
    )
    app = FastAPI()
    app.state.task_store = handler.task_store
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/"),
    )
    return app


def wire(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)


async def post(
    http: httpx.AsyncClient,
    method: str,
    params: dict[str, Any],
    headers: dict[str, str],
    request_id: str = "probe",
) -> httpx.Response:
    return await http.post(
        "/",
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        headers={"A2A-Version": "1.0", **headers},
    )


async def rpc(
    http: httpx.AsyncClient,
    method: str,
    params: dict[str, Any],
    headers: dict[str, str],
    request_id: str = "probe",
) -> dict[str, Any]:
    """JSON-RPC 요청 하나 — 답은 (스트리밍 메서드의 실행 전 오류까지) JSON 한 덩이로 온다."""
    return json.loads((await post(http, method, params, headers, request_id)).text)


def new_task() -> dict[str, Any]:
    return MessageToDict(pb.SendMessageRequest(message=build_message(make_task())))


def follow_up(task_id: str) -> dict[str, Any]:
    message = build_message(make_task())
    message.task_id = task_id
    return MessageToDict(pb.SendMessageRequest(message=message))


def push_config(task_id: str) -> dict[str, Any]:
    config = pb.TaskPushNotificationConfig(task_id=task_id, url="http://collector.test/hook")
    return MessageToDict(config)


REQUESTS: dict[str, Any] = {
    "GetTask": lambda task_id: {"id": task_id},
    "ListTasks": lambda task_id: {"includeArtifacts": True},
    "CancelTask": lambda task_id: {"id": task_id},
    "SubscribeToTask": lambda task_id: {"id": task_id},
    "CreateTaskPushNotificationConfig": push_config,
    "GetTaskPushNotificationConfig": lambda task_id: {"taskId": task_id, "id": "config"},
    "ListTaskPushNotificationConfigs": lambda task_id: {"taskId": task_id},
    "DeleteTaskPushNotificationConfig": lambda task_id: {"taskId": task_id, "id": "config"},
    "GetExtendedAgentCard": lambda task_id: {},
    # 기존 태스크를 가리키는 메시지 — 새 태스크 요청과 이름은 같아도 조회 창구다
    "SendMessage": follow_up,
    "SendStreamingMessage": follow_up,
}
"""JSON-RPC 메서드 → 주어진 태스크를 겨냥한 params."""


def test_the_probed_methods_are_everything_the_sdk_routes():
    """SDK 가 JSON-RPC 메서드를 추가하면 아래 실제 경로 시험이 그것을 빠뜨리지 않게."""
    assert set(REQUESTS) == set(JsonRpcDispatcher.METHOD_TO_MODEL)


def assert_says_nothing(answer: dict[str, Any], task_id: str) -> None:
    """거절 응답이 태스크에 대해 아무것도 말하지 않는다 — id · 상태 · 산출물 어느 것도."""
    assert "result" not in answer
    assert answer["error"]["code"] == UNSUPPORTED
    said = json.dumps(answer)
    assert task_id not in said
    assert MARKER not in said
    assert "TASK_STATE" not in said


# per-edge 토큰 모드 ----------------------------------------------------------------


def token_of(caller: str) -> dict[str, str]:
    return {CALLER_HEADER: caller, TOKEN_HEADER: issue_token(SECRET, Edge(caller, CALLEE))}


TOKEN_CREDENTIALS: dict[str, Any] = {
    "no headers": lambda: {},
    "wrong token": lambda: {CALLER_HEADER: CALLER, TOKEN_HEADER: "forged"},
    "another declared caller": lambda: token_of(OTHER_CALLER),
    "the caller that made the task": valid_headers,
}


async def token_mode_task() -> tuple[httpx.AsyncClient, str]:
    """researcher 가 planner 에 맡겨 끝난 태스크 하나 — (서버로 가는 선, SDK task id)."""
    http = wire(serve_open(guard_for((CALLER, CALLEE), (OTHER_CALLER, CALLEE))))
    made = await rpc(http, "SendMessage", new_task(), valid_headers())
    assert MARKER in json.dumps(made), "시험할 태스크가 만들어지지 않았다"
    return http, made["result"]["task"]["id"]


@pytest.mark.parametrize("credentials", sorted(TOKEN_CREDENTIALS))
@pytest.mark.parametrize("method", sorted(REQUESTS))
async def test_token_mode_refuses_every_task_request(method, credentials):
    """자격이 없든, 틀리든, 다른 호출자의 유효한 토큰이든 — 태스크는 보이지 않는다."""
    http, task_id = await token_mode_task()

    answer = await rpc(http, method, REQUESTS[method](task_id), TOKEN_CREDENTIALS[credentials]())

    assert_says_nothing(answer, task_id)


@pytest.mark.parametrize("method", sorted(REQUESTS))
async def test_token_mode_answers_the_same_for_a_task_that_does_not_exist(method):
    """응답이 다르면 그 차이가 존재 여부를 알려 준다."""
    http, task_id = await token_mode_task()
    other = token_of(OTHER_CALLER)

    existing = await rpc(http, method, REQUESTS[method](task_id), other)
    missing = await rpc(http, method, REQUESTS[method]("no-such-task"), other)

    assert existing == missing


async def test_token_mode_still_serves_a_new_task_after_refusals():
    """거절이 새 태스크 창구까지 닫지 않는다 — 과잉 거절도 결함이다."""
    http, task_id = await token_mode_task()
    await rpc(http, "CancelTask", REQUESTS["CancelTask"](task_id), {})

    again = await rpc(http, "SendMessage", new_task(), token_of(OTHER_CALLER))

    assert MARKER in json.dumps(again)


def leaves(value: Any) -> Any:
    """로그 필드 값의 잎 — 직렬화하기 전의 구조를 그대로 훑는다."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield from leaves(key)
            yield from leaves(inner)
    elif isinstance(value, list | tuple | set | frozenset):
        for inner in value:
            yield from leaves(inner)
    else:
        yield value


CLAIMED_NAME = "name-only-a-header-claimed"
RPC_ID = "id-only-the-request-chose"


@pytest.mark.parametrize("method", sorted(REQUESTS))
async def test_a_refusal_log_carries_nothing_the_request_sent(method):
    """거절은 호출자 확인 없이 한다 — 요청이 고른 값을 로그로 옮기지 않는다.

    task id · JSON-RPC id · 헤더의 이름 주장 어느 것도, 어느 필드에도.
    """
    http, task_id = await token_mode_task()
    claimed = {CALLER_HEADER: CLAIMED_NAME, TOKEN_HEADER: "forged"}

    with capture_logs() as logs:
        await rpc(http, method, REQUESTS[method](task_id), claimed, RPC_ID)

    (refusal,) = [entry for entry in logs if entry["event"] == "a2a request refused"]
    assert refusal["a2a_callee"] == CALLEE
    assert "a2a_caller" not in refusal
    for entry in logs:
        for leaf in leaves(entry):
            for sent in (task_id, CLAIMED_NAME, RPC_ID):
                assert sent not in str(leaf), (
                    f"요청이 보낸 값이 로그 필드에 실렸다: {entry['event']}"
                )


# 끝난 태스크를 남기지 않는다 ---------------------------------------------------------

SENDS = 5
"""요청 수에 비례해 쌓이는지 볼 만큼 — 한 건이면 '하나 남음' 과 '안 남음' 만 가른다."""


async def failed(task):
    return TaskResult.failed(
        task, AaestroError(ErrorCategory.INTERNAL, "INTERNAL_001", "peer broke")
    )


async def crashed(task):
    raise RuntimeError("the agent raised")


def too_deep() -> dict[str, Any]:
    """입구는 지나고 실행기에서 거부되는 호출 — 깊이는 본문을 읽어야 알 수 있다."""
    trace = TraceContext(trace_id="trace", depth=99)
    deep = make_task().model_copy(update={"trace": trace})
    return MessageToDict(pb.SendMessageRequest(message=build_message(deep)))


@pytest.mark.parametrize("method", ["SendMessage", "SendStreamingMessage"])
@pytest.mark.parametrize(
    ("handle", "params"),
    [(plan, new_task), (failed, new_task), (crashed, new_task), (plan, too_deep)],
    ids=["completed", "failed", "agent raised", "refused by the executor"],
)
async def test_a_finished_task_is_not_kept(method, handle, params):
    """읽는 창구를 닫았으니 끝난 태스크는 쌓이기만 한다 — 요청이 끝나면 store 에 남지 않는다."""
    app = serve_open(guard_for((CALLER, CALLEE)), handle)
    http = wire(app)

    for _ in range(SENDS):
        answered = await post(http, method, params(), valid_headers())
        assert answered.status_code == 200 and answered.text

    assert len(app.state.task_store) == 0


def test_the_handler_does_not_take_a_task_store():
    """끝난 태스크를 쥐는 store 를 끼우면 다시 쌓인다 — 조립이 고를 수 없게 한다."""
    guard = guard_for((CALLER, CALLEE))

    with pytest.raises(TypeError):
        GuardedRequestHandler(
            guard=guard,
            agent_executor=GuardedExecutor(guard, plan),
            task_store=InMemoryTaskStore(),
            agent_card=peer_card(),
        )


def task_in(state: int) -> pb.Task:
    return pb.Task(id="task", context_id="context", status=pb.TaskStatus(state=state))


@pytest.mark.parametrize("state", sorted(TERMINAL_TASK_STATES))
async def test_the_store_drops_a_task_when_it_finishes(state):
    """응답 뒤에 지우는 것이 아니라 종결 상태를 저장하지 않는다 — 호출자가 끊어도 남지 않는다."""
    store = RunningTaskStore()
    await store.save(task_in(pb.TaskState.TASK_STATE_WORKING), None)
    assert len(store) == 1

    await store.save(task_in(state), None)

    assert len(store) == 0
    assert await store.get("task", None) is None


async def test_the_store_holds_its_own_copy_of_a_running_task():
    """SDK 는 저장한 객체를 계속 고친다 — 같은 객체를 쥐면 store 가 모르는 새 상태가 바뀐다."""
    store = RunningTaskStore()
    running = task_in(pb.TaskState.TASK_STATE_WORKING)
    await store.save(running, None)

    running.status.state = pb.TaskState.TASK_STATE_COMPLETED
    held = await store.get("task", None)

    assert held is not None and held.status.state == pb.TaskState.TASK_STATE_WORKING
    await store.delete("task", None)
    assert len(store) == 0


# per-edge 토큰 모드의 입구 검사 -----------------------------------------------------


async def test_token_mode_verifies_the_caller_at_the_entrance():
    """실행기에서야 거부하면 SDK 가 자격 없는 요청마다 태스크를 만들고 본문을 쥔다."""
    guard = guard_for((CALLER, CALLEE))

    assert await guard.admit(valid_headers()) == CALLER
    for headers in ({}, {CALLER_HEADER: CALLER}, {CALLER_HEADER: CALLER, TOKEN_HEADER: "forged"}):
        with pytest.raises(AaestroError) as refused:
            await guard.admit(headers)
        assert refused.value.code == ErrorCode.A2A_004


@pytest.mark.parametrize("method", ["SendMessage", "SendStreamingMessage"])
@pytest.mark.parametrize(
    "headers", [{}, {CALLER_HEADER: CALLER, TOKEN_HEADER: "forged"}], ids=["none", "forged"]
)
async def test_token_mode_refuses_an_uncredentialed_send_before_the_agent_and_the_store(
    method, headers
):
    """입구에서 갈리면 실행기도 store 도 그 요청을 보지 못한다 — 본문이 계약에 맞든 아니든."""
    reached: list[str] = []

    async def handle(task):  # pragma: no cover - 도달하면 안 된다
        reached.append(task.task_id)
        return TaskResult.completed(task, output={})

    app = serve_open(guard_for((CALLER, CALLEE)), handle)
    saved = watch_saves(app.state.task_store)
    malformed = MessageToDict(
        pb.SendMessageRequest(
            message=pb.Message(message_id="m", role=pb.Role.ROLE_USER, parts=[pb.Part(text="{")])
        )
    )

    for params in (new_task(), malformed):
        answer = await rpc(wire(app), method, params, headers)
        assert f"a2a:{ErrorCode.A2A_004}" in answer["error"]["message"]

    assert reached == []
    assert saved == []


def watch_saves(store: RunningTaskStore) -> list[str]:
    """store 에 닿은 저장 — 끝난 태스크는 남지 않으니 크기로는 '만들었다 지움' 이 안 보인다."""
    saved: list[str] = []
    save = store.save

    async def recording(task, context):
        saved.append(task.id)
        await save(task, context)

    store.save = recording  # type: ignore[method-assign]
    return saved


# 레지스트리 티켓 모드 --------------------------------------------------------------


@pytest.fixture
async def stack():
    """실제 control plane 권한 라우트 — 판정 캐시가 변경 알림을 따라가게 켜 둔다."""
    found = Stack()
    watching = asyncio.create_task(found.verifier.watch())
    try:
        yield found
    finally:
        watching.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watching


async def ticket_of(stack: Stack, agent: str = "researcher") -> dict[str, str]:
    source = TicketSource(
        agent=agent,
        base_url="http://cp",
        credential=stack.credentials[agent],
        http=stack._http(),  # noqa: SLF001
    )
    return {CALLER_HEADER: agent, TICKET_HEADER: await source.ticket_for("planner")}


async def registry_mode_task(stack: Stack) -> tuple[httpx.AsyncClient, str]:
    """실제 레지스트리가 표를 확인하는 planner 에 researcher 가 맡겨 끝난 태스크."""
    guard = InboundGuard(
        server=A2AServer(agent="planner", allowlist=_allowlist()), verifier=stack.verifier
    )
    http = wire(serve_open(guard))
    made = await rpc(http, "SendMessage", new_task(), await ticket_of(stack))
    assert MARKER in json.dumps(made), "시험할 태스크가 만들어지지 않았다"
    return http, made["result"]["task"]["id"]


@pytest.mark.parametrize(
    "credentials", ["no headers", "wrong ticket", "the caller that made the task"]
)
@pytest.mark.parametrize("method", sorted(REQUESTS))
async def test_registry_mode_refuses_every_task_request(method, credentials, stack):
    """표가 없든, 틀리든, 유효하든 — 레지스트리가 허용한 호출자에게도 태스크는 보이지 않는다."""
    http, task_id = await registry_mode_task(stack)
    headers = {
        "no headers": {},
        "wrong ticket": {CALLER_HEADER: "researcher", TICKET_HEADER: "forged"},
        "the caller that made the task": await ticket_of(stack),
    }[credentials]

    answer = await rpc(http, method, REQUESTS[method](task_id), headers)

    assert_says_nothing(answer, task_id)


@pytest.mark.parametrize("method", sorted(REQUESTS))
async def test_registry_mode_answers_the_same_for_a_task_that_does_not_exist(method, stack):
    http, task_id = await registry_mode_task(stack)
    ticket = await ticket_of(stack)

    existing = await rpc(http, method, REQUESTS[method](task_id), ticket)
    missing = await rpc(http, method, REQUESTS[method]("no-such-task"), ticket)

    assert existing == missing
