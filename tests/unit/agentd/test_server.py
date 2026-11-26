"""Unit tests for the Agent Control API server.

컨테이너 없이 FastAPI TestClient 로 검증한다 — 이 계층의 계약은 HTTP 표면
(엔드포인트·인증·동시성·에러 변환)이지 실행 로직이 아니다.
"""

from __future__ import annotations

import asyncio
import contextvars
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from aaestro.agentd import server as server_module
from aaestro.agentd.echo import EchoExecutor
from aaestro.agentd.executor import MAX_STEER_CHARS, Executor, ExecutorServices, UserTurn
from aaestro.agentd.server import AgentRuntime, _sse_frame, create_app
from aaestro.core.agent import (
    ComponentHealth,
    HealthState,
    HealthStatus,
    ModelUsage,
    TaskConfig,
    TaskResult,
    TaskStatus,
)
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.events import (
    DoneEvent,
    ErrorEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from aaestro.core.markers import STEER_HEADER
from tests.fixtures.builders import make_task
from tests.fixtures.fake_model import FakeModel, FakeTools, calls, text
from tests.fixtures.waiting import reached, spin, until

TOKEN = "agent-token"
AUTH = {"authorization": f"Bearer {TOKEN}"}


class FakeExecutor:
    """태스크 실행을 스크립트하는 executor 대역."""

    def __init__(self, result=None, *, delay: float = 0.0, error: Exception | None = None) -> None:
        self._result = result
        self._delay = delay
        self._error = error
        self.calls: list[str] = []
        self.running = asyncio.Event()

    async def execute(self, task):
        self.calls.append(task.task_id)
        self.running.set()
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._result or TaskResult.completed(task, output={"content": "done"})

    async def stream(self, task):
        self.calls.append(task.task_id)
        yield TokenEvent(task_id=task.task_id, text="hi")
        yield DoneEvent(task_id=task.task_id, output={"content": "done"})


def make_client(
    executor=None, *, raise_server_exceptions: bool = True, **runtime_kwargs
) -> tuple[TestClient, AgentRuntime]:
    """토큰이 걸린 앱과 런타임.

    ``raise_server_exceptions=False`` 는 실제 서버와 같이 동작한다 — 기본값은
    디버깅용으로 예외를 다시 던져 exception handler 를 건너뛴다.
    """
    runtime = AgentRuntime(
        agent="researcher", executor=executor or FakeExecutor(), **runtime_kwargs
    )
    client = TestClient(
        create_app(runtime, token=TOKEN), raise_server_exceptions=raise_server_exceptions
    )
    return client, runtime


def task_payload(**overrides) -> dict:
    """직렬화된 TaskRequest 본문."""
    return make_task(**overrides).model_dump(mode="json")


# --- 인증 ------------------------------------------------------------------


def test_health_is_unauthenticated():
    """Docker healthcheck 가 직접 호출하므로 토큰을 요구하지 않는다."""
    client, _ = make_client()

    response = client.get("/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/v1/invoke", True),
        ("post", "/v1/stream", True),
        ("get", "/v1/card", False),
        ("post", "/v1/cancel/t1", False),
        ("post", "/v1/steer/t1", False),
        ("post", "/v1/reload", False),
        ("post", "/v1/drain", False),
    ],
)
def test_authenticated_endpoints_reject_missing_token(method, path, body):
    client, _ = make_client()

    kwargs = {"json": task_payload()} if body else {}
    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 401


def test_wrong_token_is_rejected():
    client, _ = make_client()

    response = client.get("/v1/card", headers={"authorization": "Bearer wrong"})

    assert response.status_code == 401


def test_valid_token_is_accepted():
    client, _ = make_client()

    assert client.get("/v1/card", headers=AUTH).status_code == 200


def test_app_without_token_skips_authentication():
    """토큰 미설정 환경(로컬 개발)에서는 인증을 요구하지 않는다."""
    runtime = AgentRuntime(agent="researcher", executor=FakeExecutor())
    client = TestClient(create_app(runtime))

    assert client.get("/v1/card").status_code == 200


# --- invoke ----------------------------------------------------------------


def test_invoke_returns_a_task_result():
    client, _ = make_client()

    response = client.post("/v1/invoke", json=task_payload(task_id="t-1"), headers=AUTH)

    body = response.json()
    assert response.status_code == 200
    assert body["task_id"] == "t-1"
    assert body["status"] == TaskStatus.COMPLETED


def test_invoke_is_synchronous_not_accepted():
    """202+polling 이 아니라 동기 응답이다 (02 API Rules 2)."""
    client, _ = make_client()

    response = client.post("/v1/invoke", json=task_payload(), headers=AUTH)

    assert response.status_code == 200


def test_invoke_response_is_a_serialized_model():
    """ad-hoc dict 가 아니라 TaskResult 직렬화여야 한다."""
    client, _ = make_client()

    body = client.post("/v1/invoke", json=task_payload(), headers=AUTH).json()

    assert set(body) >= {"task_id", "status", "output", "usage", "completed_at"}


def test_malformed_task_is_rejected():
    client, _ = make_client()

    response = client.post("/v1/invoke", json={"task_id": "only"}, headers=AUTH)

    # 422 가 아니라 typed 에러 — 기본 422 는 받은 값을 되비춘다 (#448)
    assert response.status_code == 400
    assert response.json()["code"] == "VAL_001"


# --- 에러 변환 -------------------------------------------------------------


def test_uncaught_exception_becomes_a_structured_error():
    """예상 못한 예외가 500 빈 응답이 되면 원인을 알 수 없다.

    TestClient 기본값은 디버깅을 위해 예외를 다시 던지므로, 실제 서버와 같은
    동작(handler 가 응답으로 변환)을 보려면 raise_server_exceptions=False 가 필요하다.
    """
    client, _ = make_client(FakeExecutor(error=RuntimeError("boom")), raise_server_exceptions=False)

    response = client.post("/v1/invoke", json=task_payload(), headers=AUTH)

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == ErrorCode.INTERNAL_001
    assert body["category"] == ErrorCategory.INTERNAL
    assert body["agent"] == "researcher"


def test_structured_error_keeps_its_payload():
    """#234 — 에이전트가 완주하지 못한 것은 **호출자가 고칠 수 없다**.

    400 으로 답하면 "요청이 잘못됐다" 는 뜻이라 호출자가 입력을 의심하고,
    05 의 사고 대응에서도 서버 결함이 4xx 버킷에 숨는다.
    """
    error = AaestroError(
        category=ErrorCategory.MODEL,
        code=ErrorCode.LLM_005,
        message="max turns exceeded",
    )
    client, _ = make_client(FakeExecutor(error=error))

    response = client.post("/v1/invoke", json=task_payload(), headers=AUTH)

    assert response.status_code == 500
    assert response.json()["code"] == "LLM_005"


def test_retryable_error_reports_service_unavailable():
    """재시도 가능 실패는 503 이어야 호출자가 재시도를 판단할 수 있다."""
    error = AaestroError(
        category=ErrorCategory.TIMEOUT,
        code=ErrorCode.TO_001,
        message="task timeout",
        retryable=True,
    )
    client, _ = make_client(FakeExecutor(error=error))

    response = client.post("/v1/invoke", json=task_payload(), headers=AUTH)

    assert response.status_code == 503


# --- stream ----------------------------------------------------------------


def test_stream_emits_sse_events():
    client, _ = make_client()

    with client.stream("POST", "/v1/stream", json=task_payload(), headers=AUTH) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    assert body.count("data: ") == 2
    assert '"type":"token"' in body.replace(" ", "")


class _UnserializableEvents(FakeExecutor):
    """JSON 왕복은 통과하지만 pydantic 직렬화에서 터지는 결과를 내는 실행기."""

    def __init__(self, value) -> None:
        super().__init__()
        self._value = value

    async def stream(self, task):
        yield ToolResultEvent(task_id=task.task_id, tool="search", result=self._value)
        yield DoneEvent(task_id=task.task_id, output={"content": "done"})


def _deep(n: int):
    value: list = []
    for _ in range(n):
        value = [value]
    return value


@pytest.mark.parametrize("value", [{"v": "\ud800"}, _deep(260)], ids=["surrogate", "deep"])
def test_stream_survives_an_event_that_cannot_be_serialized(value):
    client, _ = make_client(_UnserializableEvents(value))

    with client.stream("POST", "/v1/stream", json=task_payload(), headers=AUTH) as response:
        events = [
            json.loads(line.removeprefix("data: "))
            for line in "".join(response.iter_text()).splitlines()
            if line.startswith("data: ")
        ]

    assert [e["type"] for e in events] == ["tool_result", "done"]
    assert events[0]["result"] is None
    assert events[0]["error"]["code"] == "SKILL_001"
    assert events[0]["error"]["message"] == "tool result not serializable: search"


def test_sse_frame_uses_the_mcp_code_for_an_mcp_tool():
    event = ToolResultEvent(task_id="t", tool="mcp__fs__read", result={"v": "\ud800"})

    body = json.loads(_sse_frame(event, "researcher").decode().removeprefix("data: "))

    assert body["result"] is None
    assert body["error"]["code"] == "MCP_003"


def test_sse_frame_replaces_a_non_tool_event_with_an_internal_error():
    event = DoneEvent(task_id="t", output={"v": "\ud800"})

    body = json.loads(_sse_frame(event, "researcher").decode().removeprefix("data: "))

    assert body["type"] == "error"
    assert body["error"]["code"] == "INTERNAL_001"
    assert body["error"]["category"] == "internal"


def _frame_body(frame: bytes) -> dict:
    return json.loads(frame.decode().removeprefix("data: "))


def test_stream_with_unserializable_tool_call_arguments_keeps_going_without_an_error_event():
    class Events(FakeExecutor):
        async def stream(self, task):
            yield ToolCallEvent(task_id=task.task_id, tool="search", arguments={"a": _deep(260)})
            yield ToolResultEvent(task_id=task.task_id, tool="search", result="ok")
            yield DoneEvent(task_id=task.task_id, output={"content": "done"})

    client, _ = make_client(Events())

    with client.stream("POST", "/v1/stream", json=task_payload(), headers=AUTH) as response:
        events = [
            json.loads(line.removeprefix("data: "))
            for line in "".join(response.iter_text()).splitlines()
            if line.startswith("data: ")
        ]

    assert [e["type"] for e in events] == ["tool_call", "tool_result", "done"]
    assert events[0]["arguments"] == {}


def test_sse_frame_skips_a_non_terminal_event_that_cannot_be_serialized():
    event = TokenEvent(task_id="t", text="\ud800")

    assert _sse_frame(event, "researcher") == b""


def test_sse_frame_replacements_survive_surrogates_in_task_id_and_tool():
    bad = "\ud800x"
    result = ToolResultEvent(task_id=bad, tool=bad, result="ok")
    call = ToolCallEvent(task_id=bad, tool=bad)
    done = DoneEvent(task_id=bad)

    for event, kind in ((result, "tool_result"), (call, "tool_call"), (done, "error")):
        body = _frame_body(_sse_frame(event, "researcher"))
        assert body["type"] == kind
    assert _frame_body(_sse_frame(result, "researcher"))["error"]["code"] == "SKILL_001"


def test_sse_frame_replacement_keeps_the_usage_of_the_event_it_replaces():
    bad = "\ud800x"
    done = DoneEvent(task_id=bad, usage=ModelUsage(input_tokens=7, output_tokens=2))

    body = _frame_body(_sse_frame(done, "researcher"))

    assert body["type"] == "error"
    assert (body["usage"]["input_tokens"], body["usage"]["output_tokens"]) == (7, 2)


def test_sse_frame_falls_back_to_a_fixed_frame_when_the_replacement_fails(monkeypatch):
    def boom(event, agent):
        raise RuntimeError("no")

    monkeypatch.setattr(server_module, "_replacement", boom)

    body = _frame_body(_sse_frame(DoneEvent(task_id="t", output={"v": "\ud800"}), "researcher"))

    assert body["type"] == "error"
    assert body["error"]["code"] == "INTERNAL_001"
    assert _sse_frame(ToolCallEvent(task_id="t", tool="x", arguments={"a": _deep(260)}), "r") == b""


# --- card / cancel / reload / drain -----------------------------------------


def test_card_returns_the_agent_card():
    client, _ = make_client(card={"name": "researcher", "capabilities": {"streaming": True}})

    body = client.get("/v1/card", headers=AUTH).json()

    assert body["name"] == "researcher"


def test_cancel_unknown_task_is_not_found():
    """#234 — 이름 그대로 404 다. 400 은 "요청 형식이 틀렸다" 는 뜻이다."""
    client, _ = make_client()

    response = client.post("/v1/cancel/missing", headers=AUTH)

    assert response.status_code == 404
    assert response.json()["code"] == "NF_001"


# --- steer -------------------------------------------------------------------


class SteerableExecutor(FakeExecutor):
    """steer 를 받는 실행기 대역 — 진행 중인 태스크만 안다."""

    def __init__(self, *, running: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._running = running
        self.steers: list[tuple[str, str, str]] = []

    async def steer(self, task_id, text, *, by):
        if task_id not in self._running:
            raise AaestroError(
                category=ErrorCategory.NOT_FOUND,
                code=ErrorCode.NF_001,
                message=f"no in-flight task: {task_id}",
                task_id=task_id,
            )
        self.steers.append((task_id, text, by))


STEER = {"text": "look at 2024 instead", "by": "alice"}


def test_steer_is_queued_not_applied():
    """응답은 접수다 — 반영을 약속하지 않는다."""
    executor = SteerableExecutor(running=("t1",))
    client, _ = make_client(executor)

    response = client.post("/v1/steer/t1", json=STEER, headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    assert executor.steers == [("t1", "look at 2024 instead", "alice")]


def test_steer_for_a_task_that_is_not_running_is_not_found():
    client, _ = make_client(SteerableExecutor(running=("t1",)))

    response = client.post("/v1/steer/finished-or-unknown", json=STEER, headers=AUTH)

    assert response.status_code == 404
    assert response.json()["code"] == "NF_001"


@pytest.mark.parametrize("executor", [FakeExecutor, EchoExecutor], ids=["custom", "echo"])
def test_an_executor_without_steer_answers_501_instead_of_swallowing_it(executor):
    """200 으로 삼키면 운영자가 방향이 바뀐 줄 안다."""
    client, _ = make_client(executor())

    response = client.post("/v1/steer/t1", json=STEER, headers=AUTH)

    assert response.status_code == 501
    assert response.json() == {"status": "unsupported"}


@pytest.mark.parametrize("body", [{"text": "no issuer"}, {"by": "alice"}, {**STEER, "node": "x"}])
def test_a_malformed_steer_body_is_rejected(body):
    executor = SteerableExecutor(running=("t1",))
    client, _ = make_client(executor)

    response = client.post("/v1/steer/t1", json=body, headers=AUTH)

    assert response.status_code == 400
    assert response.json()["code"] in {"VAL_001", "VAL_002"}
    assert executor.steers == []


class SteeringTools(FakeTools):
    """tool 이 도는 동안 훅을 부른다 — 실행 도중의 HTTP steer 를 결정적으로 건다."""

    hook = None

    async def call(self, name, arguments, ctx):
        if self.hook is not None:
            await self.hook()
        return await super().call(name, arguments, ctx)


@pytest.mark.parametrize("route", ["/v1/invoke", "/v1/stream"])
async def test_a_steer_posted_mid_task_reaches_the_model(route):
    """배선 — HTTP 로 넣은 지시가 진행 중인 태스크의 다음 모델 호출에 실린다.

    `/stream` 태스크는 서버의 취소 추적표에 없다: 진행 여부를 실행기에 묻지 않으면 404 가 된다.
    """
    model = FakeModel([calls("search"), text("done")])
    tools = SteeringTools()
    executor = Executor(agent="researcher", model=model, tools=tools, render=lambda task: "prompt")
    app = create_app(AgentRuntime(agent="researcher", executor=executor), token=TOKEN)
    answers: list[tuple[int, dict]] = []

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent", headers=AUTH
    ) as http:

        async def steer_over_http() -> None:
            response = await http.post("/v1/steer/task-0001", json=STEER)
            answers.append((response.status_code, response.json()))

        tools.hook = steer_over_http
        ran = await http.post(route, json=task_payload())
        late = await http.post("/v1/steer/task-0001", json=STEER)

    assert ran.status_code == 200
    assert answers == [(200, {"status": "queued"})]
    _system, messages, _tools = model.requests[1]
    assert messages[-1] == UserTurn(parts=(f"{STEER_HEADER}\nlook at 2024 instead",))
    # 끝난 태스크는 더 받지 않는다
    assert (late.status_code, late.json()["code"]) == (404, "NF_001")


async def test_a_refused_steer_keeps_its_code_over_http():
    """상한 초과는 400(VAL_002) — 실행기의 거절이 그대로 나간다."""
    model = FakeModel([calls("search"), text("done")])
    tools = SteeringTools()
    executor = Executor(agent="researcher", model=model, tools=tools, render=lambda task: "prompt")
    app = create_app(AgentRuntime(agent="researcher", executor=executor), token=TOKEN)
    answers: list[tuple[int, str]] = []

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent", headers=AUTH
    ) as http:

        async def oversized() -> None:
            body = {"text": "x" * (MAX_STEER_CHARS + 1), "by": "alice"}
            response = await http.post("/v1/steer/task-0001", json=body)
            answers.append((response.status_code, response.json()["code"]))

        tools.hook = oversized
        await http.post("/v1/invoke", json=task_payload())

    assert answers == [(400, "VAL_002")]


def test_reload_invokes_the_hook():
    reloaded = asyncio.Event()

    async def reload() -> None:
        reloaded.set()

    client, _ = make_client(reload=reload)

    response = client.post("/v1/reload", headers=AUTH)

    assert response.json()["status"] == "reloaded"
    assert reloaded.is_set()


def test_reload_without_a_hook_says_it_did_not_reload():
    """#274 — 훅 없이 ``reloaded`` 로 답하면 호출자는 반영된 줄 안다.

    이전 테스트가 바로 그 거짓을 고정하고 있었다.
    """
    client, _ = make_client()

    response = client.post("/v1/reload", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["status"] == "unsupported"


def test_reload_replaces_the_card_the_hook_returns():
    """card 의 skill 목록은 실제로 로드된 도구와 같아야 한다 (03 AgentCard 1)."""

    async def reload() -> dict:
        return {"name": "test-agent", "skills": [{"name": "fresh"}]}

    client, _ = make_client(reload=reload)

    client.post("/v1/reload", headers=AUTH)

    assert client.get("/v1/card", headers=AUTH).json()["skills"] == [{"name": "fresh"}]


def test_a_failed_reload_is_a_typed_error_and_keeps_the_card():
    from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

    async def reload() -> dict:
        raise AaestroError(
            category=ErrorCategory.MODULE, code=ErrorCode.MOD_001, message="promptset not found"
        )

    client, _ = make_client(reload=reload)
    before = client.get("/v1/card", headers=AUTH).json()

    response = client.post("/v1/reload", headers=AUTH)

    assert response.json()["code"] == ErrorCode.MOD_001
    assert client.get("/v1/card", headers=AUTH).json() == before


def test_drain_marks_the_runtime_draining():
    client, runtime = make_client()

    response = client.post("/v1/drain", headers=AUTH)

    assert response.json()["status"] == "drained"
    assert runtime.draining is True


def test_draining_agent_rejects_new_tasks():
    """drain 중 새 태스크를 받으면 정지가 끝나지 않는다."""
    client, _ = make_client()
    client.post("/v1/drain", headers=AUTH)

    response = client.post("/v1/invoke", json=task_payload(), headers=AUTH)

    assert response.status_code == 503
    assert response.json()["code"] == "RT_005"


def test_draining_agent_rejects_streaming_too():
    client, _ = make_client()
    client.post("/v1/drain", headers=AUTH)

    response = client.post("/v1/stream", json=task_payload(), headers=AUTH)

    assert response.status_code == 503


def test_draining_reports_degraded_health():
    client, _ = make_client()
    client.post("/v1/drain", headers=AUTH)

    assert client.get("/v1/health").json()["status"] == HealthState.DEGRADED


def test_health_reflects_component_state():
    def health() -> HealthStatus:
        return HealthStatus.aggregate({"mcp:fs": ComponentHealth(state=HealthState.DEGRADED)})

    client, _ = make_client(health=health)

    body = client.get("/v1/health").json()

    assert body["status"] == HealthState.DEGRADED
    assert "mcp:fs" in body["components"]


# --- 동시성 상한 ------------------------------------------------------------


async def test_concurrency_is_capped_by_the_semaphore():
    """direct 요청과 그래프 태스크가 같은 큐를 공유한다 (02 Direct Request Rules 5)."""
    runtime = AgentRuntime(
        agent="researcher", executor=FakeExecutor(delay=0.05), max_concurrent_tasks=2
    )

    assert runtime.semaphore._value == 2

    async with runtime.semaphore, runtime.semaphore:
        assert runtime.semaphore.locked()


async def test_cancel_stops_an_inflight_task():
    runtime = AgentRuntime(agent="researcher", executor=FakeExecutor(delay=5))
    task = make_task(task_id="t-cancel")
    running = asyncio.create_task(runtime.executor.execute(task))
    runtime.track(task.key, running)

    assert runtime.cancel("t-cancel") is True

    with pytest.raises(asyncio.CancelledError):
        await running


async def test_cancel_reports_false_for_unknown_task():
    runtime = AgentRuntime(agent="researcher", executor=FakeExecutor())

    assert runtime.cancel("nope") is False


async def test_drain_waits_for_inflight_tasks():
    """진행 중 태스크를 마친 뒤 정지한다 — 즉시 중단이 아니다."""
    executor = FakeExecutor(delay=0.05)
    runtime = AgentRuntime(agent="researcher", executor=executor)
    task = make_task(task_id="t-drain")
    running = asyncio.create_task(runtime.executor.execute(task))
    runtime.track(task.key, running)
    await reached(executor.running, running)

    await runtime.drain()

    assert running.done()
    assert runtime.draining is True


async def test_completed_tasks_are_untracked():
    """추적 목록이 무한히 자라면 메모리가 샌다."""
    runtime = AgentRuntime(agent="researcher", executor=FakeExecutor())
    task = make_task(task_id="t-done")
    running = asyncio.create_task(runtime.executor.execute(task))
    runtime.track(task.key, running)

    await running
    await asyncio.sleep(0)  # done callback 이 돌 기회를 준다

    assert runtime.cancel("t-done") is False


# --- 확인된 호출자 (#279) ---------------------------------------------------------------


@pytest.mark.parametrize("path", ["/v1/invoke", "/v1/stream"])
def test_a_caller_claimed_through_the_control_api_is_dropped(path):
    """확인된 호출자는 A2A 수신 입구만 채운다 — 본문의 이름으로 권한 에이전트를 속이지 못한다."""
    seen = []

    class Recording(FakeExecutor):
        async def execute(self, task):
            seen.append(task.caller)
            return await super().execute(task)

        async def stream(self, task):
            seen.append(task.caller)
            async for event in super().stream(task):
                yield event

    client, _ = make_client(Recording())

    client.post(path, json={**task_payload(), "caller": "permission-agent"}, headers=AUTH)

    assert seen == [None]


# --- 진행 중 태스크의 키 (#400) -----------------------------------------------------------


async def test_cancel_does_not_reach_a_peer_task_with_the_same_id():
    """취소는 Control API 의 것이다 — peer 가 고른 id 가 같다고 그 태스크가 대상이 되면 안 된다."""
    runtime = AgentRuntime(agent="researcher", executor=FakeExecutor(delay=5))
    peer_task = make_task(task_id="t-shared", caller="writer")
    running = asyncio.create_task(runtime.executor.execute(peer_task))
    runtime.track(peer_task.key, running)

    assert runtime.cancel("t-shared") is False

    await asyncio.sleep(0)
    assert not running.cancelled()
    running.cancel()


async def test_a_peer_task_does_not_take_the_place_of_an_inflight_node_task():
    """id 만으로 키를 잡으면 peer 의 동명 태스크가 노드 태스크 자리를 덮어 취소가 빗나간다."""
    runtime = AgentRuntime(agent="researcher", executor=FakeExecutor(delay=5))
    node_task = make_task(task_id="t-shared")
    peer_task = make_task(task_id="t-shared", caller="writer")
    node_running = asyncio.create_task(runtime.executor.execute(node_task))
    peer_running = asyncio.create_task(runtime.executor.execute(peer_task))
    runtime.track(node_task.key, node_running)
    runtime.track(peer_task.key, peer_running)

    assert runtime.cancel("t-shared") is True

    with pytest.raises(asyncio.CancelledError):
        await node_running
    assert not peer_running.cancelled()
    peer_running.cancel()


class GatedExecutor(FakeExecutor):
    """문이 열릴 때까지 도는 invoke."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def execute(self, task):
        self.calls.append(task.task_id)
        self.running.set()
        await self.gate.wait()
        return TaskResult.completed(task, output={"content": "done"})


async def test_a_peer_task_shares_the_concurrency_cap():
    """A2A 로 받은 태스크도 같은 큐다 — peer 가 상한 없이 태스크를 쌓지 못한다 (#416)."""
    executor = GatedExecutor()
    runtime = AgentRuntime(agent="researcher", executor=executor, max_concurrent_tasks=1)
    running = asyncio.create_task(runtime.run(make_task(caller="writer")))
    await reached(executor.running, running)

    assert runtime.semaphore.locked()
    executor.gate.set()
    assert (await running).status == TaskStatus.COMPLETED


async def test_drain_waits_for_a_peer_task():
    """drain 이 진행 중인 peer 태스크를 기다리지 않으면 정지가 그것을 끊는다 (#416)."""
    executor = GatedExecutor()
    runtime = AgentRuntime(agent="researcher", executor=executor)
    running = asyncio.create_task(runtime.run(make_task(caller="writer")))
    await reached(executor.running, running)

    draining = asyncio.create_task(runtime.drain())
    await spin(10)
    assert not draining.done()
    executor.gate.set()
    await draining

    assert running.done()


async def test_a_draining_agent_refuses_a_peer_task_as_a_retryable_failure():
    """drain 중의 새 peer 태스크는 실행하지 않고 RT_005 로 답한다 — 실패도 결과로 (A2A 응답)."""
    executor = FakeExecutor()
    runtime = AgentRuntime(agent="researcher", executor=executor)
    await runtime.drain()

    result = await runtime.run(make_task(caller="writer"))

    assert result.status == TaskStatus.FAILED
    assert result.error.code == "RT_005"
    assert result.error.retryable is True
    assert executor.calls == []


async def test_a_task_whose_deadline_passes_while_queued_does_not_run():
    """마감은 칸 대기를 포함한다 — 호출자가 포기한 태스크가 칸이 난 뒤에 돌지 않는다 (#416)."""
    executor = GatedExecutor()
    runtime = AgentRuntime(agent="researcher", executor=executor, max_concurrent_tasks=1)
    hog = asyncio.create_task(runtime.run(make_task(task_id="hog")))
    await reached(executor.running, hog)
    late = make_task(task_id="late", caller="writer", config=TaskConfig(timeout_s=0.05))

    result = await asyncio.wait_for(
        runtime.run(late), timeout=5
    )  # 마감이 대기를 못 끊으면 여기서 실패

    assert result.status == TaskStatus.FAILED
    assert result.error.code == "TO_001"
    assert result.error.retryable is True
    executor.gate.set()
    await hog
    assert executor.calls == ["hog"]


async def test_calls_joined_on_the_same_key_hold_one_slot():
    """같은 키의 겹친 호출은 칸을 잡지 않고 진행 중인 실행을 기다린다 (#416)."""
    executor = GatedExecutor()
    runtime = AgentRuntime(agent="researcher", executor=executor, max_concurrent_tasks=2)
    task = make_task(task_id="t-dup")
    first = asyncio.create_task(runtime.run(task))
    await reached(executor.running, first)
    second = asyncio.create_task(runtime.run(task))
    await spin(10)

    assert runtime.semaphore._value == 1
    executor.gate.set()
    results = await asyncio.gather(first, second)
    assert results[0] == results[1]
    assert executor.calls == ["t-dup"]


async def test_a_joined_call_waits_only_until_its_own_deadline():
    """긴 마감 실행에 합류한 짧은 마감 재시도는 자기 마감에 TO_001 — 첫 요청은 그대로 끝난다."""
    executor = GatedExecutor()
    runtime = AgentRuntime(agent="researcher", executor=executor)
    task = make_task(task_id="t-dup", config=TaskConfig(timeout_s=30))
    first = asyncio.create_task(runtime.run(task))
    await reached(executor.running, first)
    retry = task.model_copy(update={"config": TaskConfig(timeout_s=0.05)})

    result = await asyncio.wait_for(runtime.run(retry), timeout=5)

    assert result.status == TaskStatus.FAILED
    assert result.error.code == "TO_001"
    executor.gate.set()
    assert (await first).status == TaskStatus.COMPLETED
    assert executor.calls == ["t-dup"]


async def test_drain_waits_for_a_queued_call_and_does_not_run_it():
    """칸을 기다리던 호출도 drain 이 본다 — drain 뒤에 칸을 얻으면 실행하지 않고 RT_005 (#416)."""
    executor = GatedExecutor()
    runtime = AgentRuntime(agent="researcher", executor=executor, max_concurrent_tasks=1)
    hog = asyncio.create_task(runtime.run(make_task(task_id="hog")))
    await reached(executor.running, hog)
    queued = asyncio.create_task(runtime.run(make_task(task_id="queued", caller="writer")))
    await spin(10)

    draining = asyncio.create_task(runtime.drain())
    await spin(10)
    assert not draining.done()
    executor.gate.set()
    await draining

    assert queued.done()
    assert queued.result().error.code == "RT_005"
    assert executor.calls == ["hog"]


async def test_a_caller_leaving_raises_and_stops_the_execution_nobody_waits_for():
    """바깥 요청이 취소되면 CancelledError 가 그대로 올라가고, 기다리는 쪽이 없는 실행은 끊긴다."""
    executor = GatedExecutor()
    runtime = AgentRuntime(agent="researcher", executor=executor)
    calling = asyncio.create_task(runtime.run(make_task(task_id="t-leave")))
    await reached(executor.running, calling)

    calling.cancel()

    with pytest.raises(asyncio.CancelledError):
        await calling
    await until(lambda: not runtime._inflight)  # gate 는 닫힌 채다 — 끝났다면 끊긴 것이다


class CleansUpSlowly(FakeTools):
    """첫 호출은 끝나지 않고, 취소되면 정리 문이 열릴 때까지 정리한다 — 둘째 호출은 바로 끝난다."""

    def __init__(self) -> None:
        super().__init__()
        self.cleaning = asyncio.Event()
        self.cleaned = asyncio.Event()

    async def call(self, name, arguments, ctx):
        self.started.append(name)
        if len(self.started) == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cleaning.set()
                await self.cleaned.wait()
                raise
        self.calls.append(name)
        return "ok"


async def test_a_retry_right_after_the_last_caller_left_runs_instead_of_joining_the_dying_run():
    """끊기는 중인 실행에 잇지 않는다 — 실행기 층이 뒤에 세우고, 취소로 끝나면 다시 돈다 (#416)."""
    tools = CleansUpSlowly()
    executor = Executor(
        agent="researcher",
        model=FakeModel([calls("post"), calls("post"), text("done")]),
        tools=tools,
        render=lambda task: "prompt",
    )
    runtime = AgentRuntime(agent="researcher", executor=executor)
    task = make_task(task_id="t-retry")
    first = asyncio.create_task(runtime.run(task))
    await until(lambda: tools.started == ["post"])
    first.cancel()  # 호출자가 떠난다 — 기다리는 쪽이 없으니 실행이 끊기기 시작한다
    with pytest.raises(asyncio.CancelledError):
        await first
    await until(tools.cleaning.is_set)

    retry = asyncio.create_task(runtime.run(task))
    await spin(10)
    assert not retry.done()  # 앞선 실행의 정리가 끝나기를 기다린다
    tools.cleaned.set()

    assert (await asyncio.wait_for(retry, timeout=5)).status == TaskStatus.COMPLETED
    assert tools.calls == ["post"]


class HandsOverAtTheEnd(FakeExecutor):
    """첫 실행이 끝나는 바로 그 단계에 콜백을 걸고, 뒤 실행은 문이 열릴 때까지 돈다."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.at_end = None

    async def execute(self, task):
        self.calls.append(task.task_id)
        if len(self.calls) == 1:
            # 실행 태스크의 완료 콜백보다 먼저 줄 선다 — 끝났지만 콜백 전인 창을 결정적으로 만든다
            asyncio.get_running_loop().call_soon(self.at_end)
            return TaskResult.completed(task, output={"run": 1})
        self.running.set()
        await self.gate.wait()
        return TaskResult.completed(task, output={"run": len(self.calls)})


async def test_a_late_done_callback_does_not_clear_the_newer_run():
    """옛 실행의 늦은 완료 콜백이 새 실행 자리를 지우면 같은 키의 다음 호출이 또 돈다 (#416)."""
    executor = HandsOverAtTheEnd()
    runtime = AgentRuntime(agent="researcher", executor=executor)
    task = make_task(task_id="t-late")
    loop = asyncio.get_running_loop()
    joined = []
    # 끝난 옛 실행을 보고 새 실행을 세운다 — eager 라 콜백 전에 _runs 자리를 잡는다
    executor.at_end = lambda: joined.append(asyncio.eager_task_factory(loop, runtime.run(task)))

    await runtime.run(task)
    (second,) = joined
    await reached(executor.running, second)
    third = asyncio.create_task(runtime.run(task))
    await spin(10)
    executor.gate.set()

    results = await asyncio.gather(second, third)
    assert executor.calls == ["t-late", "t-late"]  # 셋째는 둘째 실행에 잇는다
    assert results[0] == results[1]


async def test_cancel_stops_every_call_waiting_on_the_same_key():
    """같은 키의 겹친 호출은 한 실행을 기다린다 — 마지막 것만 끊으면 실행은 그대로 돈다 (#416)."""
    runtime = AgentRuntime(agent="researcher", executor=FakeExecutor())
    key = make_task(task_id="t-dup").key
    first = asyncio.create_task(asyncio.Event().wait())
    second = asyncio.create_task(asyncio.Event().wait())
    runtime.track(key, first)
    runtime.track(key, second)

    assert runtime.cancel("t-dup") is True

    await asyncio.wait({first, second}, timeout=5)
    assert first.cancelled()
    assert second.cancelled()


async def test_the_cancel_endpoint_stops_the_task_the_invoke_endpoint_is_running():
    """엔드포인트 둘이 같은 키를 써야 한다 — 한쪽만 바꾸면 취소가 조용히 아무것도 못 찾는다."""
    import httpx

    executor = FakeExecutor(delay=5)
    runtime = AgentRuntime(agent="researcher", executor=executor)
    app = create_app(runtime, token=TOKEN)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        # 본문의 호출자 주장은 버려진다 — 취소가 찾는 것은 Control API 의 태스크다
        body = {**task_payload(task_id="t-http"), "caller": "writer"}
        invoking = asyncio.create_task(client.post("/v1/invoke", json=body, headers=AUTH))
        await reached(executor.running, invoking)

        canceled = await client.post("/v1/cancel/t-http", headers=AUTH)
        response = await invoking

    assert canceled.status_code == 200
    assert response.json()["status"] == TaskStatus.CANCELED


async def test_the_cancel_endpoint_stops_a_streaming_task_and_ends_the_stream():
    """`/stream` 태스크도 취소된다 — 스트림은 끊기지 않고 canceled 종료 이벤트로 끝난다 (#395)."""
    cleanups: list[None] = []
    model = FakeModel([calls("slow"), text("done")])
    tools = FakeTools().script("slow", delay=5)
    executor = Executor(
        agent="researcher",
        model=model,
        tools=tools,
        render=lambda task: "prompt",
        services=ExecutorServices(on_cleanup=lambda: cleanups.append(None)),
    )
    app = create_app(AgentRuntime(agent="researcher", executor=executor), token=TOKEN)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent", headers=AUTH
    ) as http:
        streaming = asyncio.create_task(http.post("/v1/stream", json=task_payload()))
        await until(lambda: tools.started == ["slow"])

        canceled = await http.post("/v1/cancel/task-0001")
        response = await streaming

    frames = [json.loads(line[5:]) for line in response.text.splitlines() if line]
    assert canceled.status_code == 200
    assert frames[-1]["type"] == "done"
    assert frames[-1]["status"] == TaskStatus.CANCELED
    assert model.turns == 1
    assert len(cleanups) == 1


# --- /stream 취소 · drain 이 스트림 전체를 본다 (#395) ---------------------------------


class StreamCall:
    """ASGI 로 `/v1/stream` 을 직접 돌린다 — 프레임 전송을 붙잡아 느린 소비자를 재현한다."""

    def __init__(self, app, payload: dict, *, hold: bool) -> None:
        self.frames: list[dict] = []
        self.release = asyncio.Event()
        if not hold:
            self.release.set()
        self.gone = asyncio.Event()
        self._body = json.dumps(payload).encode()
        self._delivered = False
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/stream",
            "raw_path": b"/v1/stream",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"authorization", f"Bearer {TOKEN}".encode()),
                (b"content-type", b"application/json"),
            ],
            "client": ("test", 1),
            "server": ("agent", 80),
        }
        self.running = asyncio.create_task(app(scope, self._receive, self._send))

    async def _receive(self) -> dict:
        if not self._delivered:
            self._delivered = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        await self.gone.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            self.frames.append(json.loads(message["body"].decode().removeprefix("data: ")))
            await self.release.wait()


class HangingExecutor(FakeExecutor):
    """첫 이벤트 뒤 문이 열릴 때까지 도는 스트림 — 정리 횟수를 센다."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.working = asyncio.Event()
        self.closed: list[str] = []

    async def stream(self, task):
        try:
            yield TokenEvent(task_id=task.task_id, text="a")
            self.working.set()
            await self.gate.wait()
            yield DoneEvent(task_id=task.task_id, output={"content": "done"})
        finally:
            self.closed.append(task.task_id)


async def test_a_cancel_while_a_frame_is_being_sent_stops_the_stream():
    """느린 소비자가 프레임을 받는 사이에도 실행기는 돈다 — 그 틈의 cancel 도 닿아야 한다."""
    executor = HangingExecutor()
    runtime = AgentRuntime(agent="researcher", executor=executor)
    app = create_app(runtime, token=TOKEN)
    call = StreamCall(app, task_payload(), hold=True)
    await until(lambda: len(call.frames) == 1 and executor.working.is_set())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent", headers=AUTH
    ) as http:
        canceled = await http.post("/v1/cancel/task-0001")
    call.release.set()
    await asyncio.wait_for(call.running, timeout=5)

    assert canceled.status_code == 200
    assert call.frames[-1]["type"] == "done"
    assert call.frames[-1]["status"] == TaskStatus.CANCELED
    assert executor.closed == ["task-0001"]


async def test_a_consumer_leaving_the_stream_cleans_the_executor_up_once():
    """응답이 끊기면 실행기도 끊긴다 — 백그라운드에 남아 돌지 않는다.

    다음 이벤트를 기다리는 응답이 취소되는 경우다 (연결 끊김에 Starlette 가 응답을 취소한다).
    """
    executor = HangingExecutor()
    runtime = AgentRuntime(agent="researcher", executor=executor)
    call = StreamCall(create_app(runtime, token=TOKEN), task_payload(), hold=False)
    await until(lambda: executor.working.is_set())

    call.running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call.running

    assert executor.closed == ["task-0001"]
    assert runtime.cancel("task-0001") is False


async def test_drain_waits_for_a_stream_to_its_end():
    """drain 은 스트림의 한 이벤트가 아니라 스트림 전체를 기다린다."""
    executor = HangingExecutor()
    runtime = AgentRuntime(agent="researcher", executor=executor)
    call = StreamCall(create_app(runtime, token=TOKEN), task_payload(), hold=False)
    await until(lambda: executor.working.is_set())

    draining = asyncio.create_task(runtime.drain())
    await spin(20)
    assert not draining.done()

    executor.gate.set()
    await asyncio.wait_for(draining, timeout=5)
    await asyncio.wait_for(call.running, timeout=5)
    assert (call.frames[-1]["type"], call.frames[-1]["status"]) == ("done", "completed")


async def test_an_executor_stream_keeps_its_context_and_deadline_across_events():
    """실행기 스트림은 한 태스크에서 돈다 — ContextVar 와 asyncio.timeout 이 이벤트를 넘는다."""
    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="unset")

    class Deadlined(FakeExecutor):
        async def stream(self, task):
            marker.set("kept")
            try:
                async with asyncio.timeout(0.05):
                    yield TokenEvent(task_id=task.task_id, text="a")
                    yield TokenEvent(task_id=task.task_id, text=marker.get())
                    await asyncio.Event().wait()
            except TimeoutError:
                yield TokenEvent(task_id=task.task_id, text="deadline")

    call = StreamCall(
        create_app(AgentRuntime(agent="researcher", executor=Deadlined()), token=TOKEN),
        task_payload(),
        hold=False,
    )
    await asyncio.wait_for(call.running, timeout=5)

    assert [frame.get("text") for frame in call.frames] == ["a", "kept", "deadline"]


async def test_a_same_id_stream_finishing_does_not_drop_the_running_invoke():
    """같은 키로 겹친 태스크는 자기 자리만 지운다 — 끝난 스트림이 invoke 를 추적에서 빼지 않는다."""
    executor = HangingExecutor()
    executor.gate.set()
    runtime = AgentRuntime(agent="researcher", executor=executor)
    invoke = make_task()
    invoking = asyncio.create_task(asyncio.Event().wait())
    runtime.track(invoke.key, invoking)

    call = StreamCall(create_app(runtime, token=TOKEN), task_payload(), hold=False)
    await asyncio.wait_for(call.running, timeout=5)
    await asyncio.sleep(0)

    assert runtime.cancel(invoke.task_id) is True
    with pytest.raises(asyncio.CancelledError):
        await invoking


# --- 종료 이벤트는 하나 — 실행기가 만든 뒤에는 완료가 이긴다 (#395) -------------------


def kinds(call: StreamCall) -> list[tuple[str, str | None]]:
    return [(frame["type"], frame.get("status")) for frame in call.frames]


async def test_a_cancel_after_the_executor_finished_keeps_its_single_done():
    """done 을 낸 뒤 정리 중에 온 cancel 은 canceled 를 하나 더 만들지 않는다."""

    class LingersAfterDone(FakeExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.cleaning = asyncio.Event()

        async def stream(self, task):
            yield DoneEvent(task_id=task.task_id, output={"content": "done"})
            self.cleaning.set()
            await asyncio.Event().wait()

    executor = LingersAfterDone()
    runtime = AgentRuntime(agent="researcher", executor=executor)
    call = StreamCall(create_app(runtime, token=TOKEN), task_payload(), hold=False)
    await until(lambda: executor.cleaning.is_set() and len(call.frames) == 1)

    assert runtime.cancel("task-0001") is True
    await asyncio.wait_for(call.running, timeout=5)

    assert kinds(call) == [("done", "completed")]


async def test_a_cancel_while_the_done_waits_for_a_slow_consumer_keeps_the_result():
    """느린 소비자로 큐에 넣지 못한 done 도 실행기의 결과다 — cancel 이 output 을 지우지 않는다."""

    class Finishes(FakeExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.done_made = asyncio.Event()

        async def stream(self, task):
            yield TokenEvent(task_id=task.task_id, text="1")
            yield TokenEvent(task_id=task.task_id, text="2")
            self.done_made.set()
            yield DoneEvent(task_id=task.task_id, output={"content": "kept"})

    executor = Finishes()
    runtime = AgentRuntime(agent="researcher", executor=executor)
    call = StreamCall(create_app(runtime, token=TOKEN), task_payload(), hold=True)
    await until(lambda: executor.done_made.is_set() and len(call.frames) == 1)
    await spin(5)  # producer 가 done 의 put 에서 막힐 자리까지 간다

    assert runtime.cancel("task-0001") is True
    call.release.set()
    await asyncio.wait_for(call.running, timeout=5)

    assert kinds(call)[-1] == ("done", "completed")
    assert call.frames[-1]["output"] == {"content": "kept"}
    assert [kind for kind, _ in kinds(call)].count("done") == 1


async def test_a_failing_custom_stream_is_reported_once():
    """응답이 거둔 실행기 예외를 정리 단계가 'already failed' 로 한 번 더 남기지 않는다."""

    class Breaks(FakeExecutor):
        async def stream(self, task):
            yield TokenEvent(task_id=task.task_id, text="a")
            raise RuntimeError("boom")

    runtime = AgentRuntime(agent="researcher", executor=Breaks())
    with capture_logs() as logs:
        call = StreamCall(create_app(runtime, token=TOKEN), task_payload(), hold=False)
        with pytest.raises(RuntimeError, match="boom"):
            await asyncio.wait_for(call.running, timeout=5)

    closing = [entry for entry in logs if entry["event"] == "executor stream failed while closing"]
    assert closing == []


async def test_a_cancel_while_blocked_on_a_slow_consumer_closes_the_executor_stream():
    """put 에서 끊긴 producer 는 실행기 생성기를 닫는다 — yield 에 멈춘 채 GC 를 기다리지 않게."""

    class Chatty(FakeExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.third = asyncio.Event()
            self.closed: list[str] = []

        async def stream(self, task):
            try:
                yield TokenEvent(task_id=task.task_id, text="1")
                yield TokenEvent(task_id=task.task_id, text="2")
                self.third.set()
                yield TokenEvent(task_id=task.task_id, text="3")
                await asyncio.Event().wait()
            finally:
                self.closed.append(task.task_id)

    executor = Chatty()
    runtime = AgentRuntime(agent="researcher", executor=executor)
    call = StreamCall(create_app(runtime, token=TOKEN), task_payload(), hold=True)
    await until(lambda: executor.third.is_set() and len(call.frames) == 1)
    await spin(5)

    assert runtime.cancel("task-0001") is True
    await until(lambda: executor.closed == ["task-0001"])
    call.release.set()
    await asyncio.wait_for(call.running, timeout=5)

    assert kinds(call)[-1] == ("done", "canceled")


# --- 커스텀 실행기의 종료 경합 (#639) ---------------------------------------------------


async def test_a_consumer_leaving_during_a_canceled_cleanup_lets_the_cleanup_finish():
    """/cancel 로 정리 중인 실행기를 소비자 이탈이 다시 끊지 않는다 — finally 의 await 가 끝난다.

    응답은 기다리다 소비자 이탈의 취소로 먼저 끝날 수 있다 — 정리는 추적된 채 끝까지 돈다.
    """

    class SlowCleanup(FakeExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.working = asyncio.Event()
            self.cleanup_gate = asyncio.Event()
            self.log: list[str] = []

        async def stream(self, task):
            try:
                yield TokenEvent(task_id=task.task_id, text="a")
                self.working.set()
                await asyncio.Event().wait()
            finally:
                self.log.append("cleanup-start")
                try:
                    await self.cleanup_gate.wait()
                except asyncio.CancelledError:
                    self.log.append("cleanup-cut")
                    raise
                self.log.append("cleanup-end")

    executor = SlowCleanup()
    runtime = AgentRuntime(agent="researcher", executor=executor)
    call = StreamCall(create_app(runtime, token=TOKEN), task_payload(), hold=False)
    await until(lambda: executor.working.is_set())
    (producer,) = runtime._inflight[make_task().key]

    assert runtime.cancel("task-0001") is True
    await until(lambda: executor.log == ["cleanup-start"])
    call.running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(call.running, timeout=5)
    await spin(20)
    assert executor.log == ["cleanup-start"]  # 소비자가 떠나도 정리는 끊기지 않고 기다린다

    executor.cleanup_gate.set()
    await until(lambda: len(executor.log) == 2)
    assert executor.log == ["cleanup-start", "cleanup-end"]
    await until(lambda: not runtime._inflight)  # 끝난 producer 는 추적에서 빠진다
    assert producer.cancelling() == 1  # /cancel 의 한 번뿐 — 소비자 이탈은 다시 끊지 않는다


async def test_an_executor_error_after_its_done_is_logged_and_the_stream_ends_normally():
    """종료 이벤트를 낸 뒤의 실행기 예외는 응답을 깨지 않는다 — 기록만 남는다."""

    class BreaksAfterDone(FakeExecutor):
        async def stream(self, task):
            yield DoneEvent(task_id=task.task_id, output={"content": "done"})
            raise RuntimeError("boom")

    runtime = AgentRuntime(agent="researcher", executor=BreaksAfterDone())
    with capture_logs() as logs:
        call = StreamCall(create_app(runtime, token=TOKEN), task_payload(), hold=False)
        await asyncio.wait_for(call.running, timeout=5)

    assert kinds(call) == [("done", "completed")]
    failures = [entry for entry in logs if entry["event"] == "executor stream failed after its end"]
    assert len(failures) == 1
    assert failures[0]["agent"] == "researcher"
    assert failures[0]["task_id"] == "task-0001"


async def test_an_executor_ignoring_generator_exit_still_ends_with_one_canceled_done():
    """GeneratorExit 을 무시하는 실행기도 취소는 canceled done 하나로 끝나고 WARN 이 남는다."""

    class IgnoresClose(FakeExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.third = asyncio.Event()

        async def stream(self, task):
            try:
                yield TokenEvent(task_id=task.task_id, text="1")
                yield TokenEvent(task_id=task.task_id, text="2")
                self.third.set()
                yield TokenEvent(task_id=task.task_id, text="3")
            except GeneratorExit:
                yield TokenEvent(task_id=task.task_id, text="refused")

    executor = IgnoresClose()
    runtime = AgentRuntime(agent="researcher", executor=executor)
    with capture_logs() as logs:
        call = StreamCall(create_app(runtime, token=TOKEN), task_payload(), hold=True)
        await until(lambda: executor.third.is_set() and len(call.frames) == 1)
        await spin(5)  # producer 가 put 에서 막힐 자리까지 간다

        assert runtime.cancel("task-0001") is True
        call.release.set()
        await asyncio.wait_for(call.running, timeout=5)

    assert kinds(call)[-1] == ("done", "canceled")
    assert [kind for kind, _ in kinds(call)].count("done") == 1
    warnings = [entry for entry in logs if entry["event"] == "executor stream ignored close"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["task_id"] == "task-0001"


async def test_a_real_error_while_closing_is_not_taken_for_an_ignored_close():
    """정리 중 실제로 터진 RuntimeError 는 'ignored close' 로 삼키지 않는다 — 실패로 올라간다."""

    class BreaksOnClose(FakeExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.third = asyncio.Event()

        async def stream(self, task):
            try:
                yield TokenEvent(task_id=task.task_id, text="1")
                yield TokenEvent(task_id=task.task_id, text="2")
                self.third.set()
                yield TokenEvent(task_id=task.task_id, text="3")
            finally:
                raise RuntimeError("cleanup broke")

    executor = BreaksOnClose()
    runtime = AgentRuntime(agent="researcher", executor=executor)
    with capture_logs() as logs:
        call = StreamCall(create_app(runtime, token=TOKEN), task_payload(), hold=True)
        await until(lambda: executor.third.is_set() and len(call.frames) == 1)
        await spin(5)

        assert runtime.cancel("task-0001") is True
        call.release.set()
        with pytest.raises(RuntimeError, match="cleanup broke"):
            await asyncio.wait_for(call.running, timeout=5)

    assert [entry for entry in logs if entry["event"] == "executor stream ignored close"] == []


class SlowCleanupExecutor(FakeExecutor):
    """finally 가 문이 열릴 때까지 await 하는 스트림 — 정리가 잘리면 cleanup-cut 을 남긴다."""

    def __init__(self, *, fail_cleanup: bool = False) -> None:
        super().__init__()
        self.working = asyncio.Event()
        self.cleanup_gate = asyncio.Event()
        self.fail_cleanup = fail_cleanup
        self.log: list[str] = []

    async def stream(self, task):
        try:
            yield TokenEvent(task_id=task.task_id, text="a")
            self.working.set()
            await asyncio.Event().wait()
        finally:
            self.log.append("cleanup-start")
            try:
                await self.cleanup_gate.wait()
            except asyncio.CancelledError:
                self.log.append("cleanup-cut")
                raise
            self.log.append("cleanup-end")
            if self.fail_cleanup:
                raise RuntimeError("cleanup broke")


async def test_a_consumer_leaving_alone_cancels_once_and_lets_the_cleanup_finish():
    """소비자만 떠나도 실행은 한 번만 끊긴다 — 응답이 거듭 취소돼도 finally 의 await 가 끝난다."""
    executor = SlowCleanupExecutor()
    runtime = AgentRuntime(agent="researcher", executor=executor)
    free_slots = runtime.semaphore._value
    call = StreamCall(create_app(runtime, token=TOKEN), task_payload(), hold=False)
    await until(lambda: executor.working.is_set())
    (producer,) = runtime._inflight[make_task().key]

    call.running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(call.running, timeout=5)
    await spin(20)
    assert executor.log == ["cleanup-start"]  # 응답은 떠났고 정리는 끊기지 않고 기다린다

    executor.cleanup_gate.set()
    await until(lambda: producer.done())
    assert executor.log == ["cleanup-start", "cleanup-end"]
    assert producer.cancelling() == 1
    assert runtime.cancel("task-0001") is False
    assert runtime.semaphore._value == free_slots


async def test_a_cleanup_failing_after_the_consumer_left_is_logged_once():
    """응답이 떠난 뒤 정리 중 예외는 WARN 한 줄로 거둔다 — 'never retrieved' 로 새지 않는다."""
    import gc

    loop = asyncio.get_running_loop()
    unhandled: list[dict] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        executor = SlowCleanupExecutor(fail_cleanup=True)
        runtime = AgentRuntime(agent="researcher", executor=executor)
        with capture_logs() as logs:
            call = StreamCall(create_app(runtime, token=TOKEN), task_payload(), hold=False)
            await until(lambda: executor.working.is_set())
            call.running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(call.running, timeout=5)
            executor.cleanup_gate.set()
            await until(lambda: not runtime._inflight)
            await spin(5)
        del call
        gc.collect()
        await spin(5)
    finally:
        loop.set_exception_handler(previous)

    failures = [entry for entry in logs if entry["event"] == "executor stream failed while closing"]
    assert len(failures) == 1
    assert (failures[0]["agent"], failures[0]["task_id"]) == ("researcher", "task-0001")
    assert [c for c in unhandled if "never retrieved" in c.get("message", "")] == []


def _late_error():
    return AaestroError(
        category=ErrorCategory.INTERNAL, code=ErrorCode.INTERNAL_001, message="late"
    ).payload()


@pytest.mark.parametrize(
    "after",
    [
        lambda task_id: ErrorEvent(task_id=task_id, error=_late_error()),
        lambda task_id: DoneEvent(task_id=task_id, output={"content": "again"}),
        lambda task_id: TokenEvent(task_id=task_id, text="late"),
    ],
    ids=["error", "done", "token"],
)
async def test_events_after_the_end_are_dropped_with_one_warning(after):
    """종료 이벤트는 하나다 — 실행기가 그 뒤에 더 내는 이벤트는 SSE 에 실리지 않고 WARN 한 번."""

    class KeepsTalking(FakeExecutor):
        async def stream(self, task):
            yield DoneEvent(task_id=task.task_id, output={"content": "first"})
            yield after(task.task_id)
            yield after(task.task_id)

    runtime = AgentRuntime(agent="researcher", executor=KeepsTalking())
    with capture_logs() as logs:
        call = StreamCall(create_app(runtime, token=TOKEN), task_payload(), hold=False)
        await asyncio.wait_for(call.running, timeout=5)

    assert kinds(call) == [("done", "completed")]
    assert call.frames[0]["output"] == {"content": "first"}
    dropped = [e for e in logs if e["event"] == "executor stream event after its end dropped"]
    assert len(dropped) == 1
    assert (dropped[0]["agent"], dropped[0]["task_id"]) == ("researcher", "task-0001")


async def test_a_close_while_the_generator_is_running_elsewhere_is_not_swallowed():
    """'already running' RuntimeError 는 GeneratorExit 무시로 오인하지 않는다 — 그대로 올라간다."""

    class SharedGenerator(FakeExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.third = asyncio.Event()
            self.gate = asyncio.Event()
            self.gen = None

        async def _events(self, task):
            yield TokenEvent(task_id=task.task_id, text="1")
            yield TokenEvent(task_id=task.task_id, text="2")
            self.third.set()
            yield TokenEvent(task_id=task.task_id, text="3")
            await self.gate.wait()  # 다른 태스크가 이 자리에서 생성기를 돌리고 있다

        def stream(self, task):
            self.gen = self._events(task)
            return self.gen

    executor = SharedGenerator()
    runtime = AgentRuntime(agent="researcher", executor=executor)
    with capture_logs() as logs:
        call = StreamCall(create_app(runtime, token=TOKEN), task_payload(), hold=True)
        await until(lambda: executor.third.is_set() and len(call.frames) == 1)
        await spin(5)  # producer 가 "3" 의 put 에서 막힌다
        elsewhere = asyncio.create_task(anext(executor.gen))
        await until(lambda: executor.gen.ag_running)

        assert runtime.cancel("task-0001") is True
        call.release.set()
        with pytest.raises(RuntimeError, match="already running"):
            await asyncio.wait_for(call.running, timeout=5)
        executor.gate.set()
        with pytest.raises(StopAsyncIteration):
            await elsewhere

    assert [e for e in logs if e["event"] == "executor stream ignored close"] == []


async def test_only_the_first_terminal_event_is_kept_while_the_executor_keeps_ending():
    """종료 이벤트를 거듭 내는 실행기도 응답이 쥐는 종료 이벤트는 첫 하나뿐이다 — 쌓이지 않는다."""

    class EndsRepeatedly(FakeExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.ended = asyncio.Event()
            self.gate = asyncio.Event()

        async def stream(self, task):
            yield DoneEvent(task_id=task.task_id, output={"content": "first"})
            for n in range(50):
                yield DoneEvent(task_id=task.task_id, output={"content": f"again-{n}"})
                yield ErrorEvent(task_id=task.task_id, error=_late_error())
            self.ended.set()
            await self.gate.wait()

    executor = EndsRepeatedly()
    runtime = AgentRuntime(agent="researcher", executor=executor)
    call = StreamCall(create_app(runtime, token=TOKEN), task_payload(), hold=False)
    await until(lambda: executor.ended.is_set())
    (producer,) = runtime._inflight[make_task().key]
    # 화이트박스: produce() 가 닫아 쥔 terminal 목록 — 밖에서 누적을 볼 다른 자리가 없다
    terminal = producer.get_coro().cr_frame.f_locals["terminal"]
    assert len(terminal) == 1
    assert terminal[0].output == {"content": "first"}

    executor.gate.set()
    await asyncio.wait_for(call.running, timeout=5)
    assert kinds(call) == [("done", "completed")]
    assert call.frames[0]["output"] == {"content": "first"}


# --- /stream 의 칸 대기 (#416) --------------------------------------------------


async def test_a_stream_whose_deadline_passes_while_queued_ends_with_to_001():
    """/stream 도 마감이 칸 대기를 포함한다 — 칸이 난 뒤에 실행하지 않고 TO_001 로 끝난다."""
    executor = GatedExecutor()
    runtime = AgentRuntime(agent="researcher", executor=executor, max_concurrent_tasks=1)
    hog = asyncio.create_task(runtime.run(make_task(task_id="hog")))
    await reached(executor.running, hog)

    payload = task_payload(task_id="late", config=TaskConfig(timeout_s=0.05))
    call = StreamCall(create_app(runtime, token=TOKEN), payload, hold=False)
    await asyncio.wait_for(call.running, timeout=5)

    assert [(f["type"], f["error"]["code"]) for f in call.frames] == [("error", "TO_001")]
    executor.gate.set()
    await hog
    assert executor.calls == ["hog"]


async def test_drain_waits_for_a_queued_stream_and_does_not_run_it():
    """칸을 기다리는 스트림도 추적된다 — drain 이 기다리고, 칸을 얻으면 RT_005 로 끝난다."""
    executor = GatedExecutor()
    runtime = AgentRuntime(agent="researcher", executor=executor, max_concurrent_tasks=1)
    hog = asyncio.create_task(runtime.run(make_task(task_id="hog")))
    await reached(executor.running, hog)
    call = StreamCall(create_app(runtime, token=TOKEN), task_payload(task_id="q"), hold=False)
    await until(lambda: runtime._inflight.get(make_task(task_id="q").key))

    draining = asyncio.create_task(runtime.drain())
    await spin(10)
    assert not draining.done()
    executor.gate.set()
    await draining
    await asyncio.wait_for(call.running, timeout=5)

    assert [(f["type"], f["error"]["code"]) for f in call.frames] == [("error", "RT_005")]
    assert executor.calls == ["hog"]


# --- PR #708 리뷰 (#416) ---------------------------------------------------------------


async def test_a_delegation_that_calls_back_does_not_deadlock_on_one_slot():
    """칸 1 에서 A → B → A: 나가는 위임 동안 칸을 놓아야 돌아온 태스크가 돈다 (교착 아님)."""
    from aaestro.agentd.slots import lend_slot
    from aaestro.protocols.a2a.allowlist import Allowlist, Edge
    from aaestro.protocols.a2a.client import A2AClient

    holder: dict = {}

    class CallsBack:
        """B 의 대역 — 받은 위임을 A 로 다른 태스크로 돌려보낸다."""

        async def send(self, *, callee, task, token, headers):
            back = make_task(task_id="t-back", caller="writer", config=TaskConfig(timeout_s=0.3))
            return await holder["runtime"].run(back)

    client = A2AClient(
        agent="researcher",
        allowlist=Allowlist(
            edges=frozenset({Edge(caller="researcher", callee="writer")}),
            secret=b"s" * 32,
            max_depth=3,
        ),
        transport=CallsBack(),
        while_waiting=lend_slot,
    )

    class Delegates(FakeExecutor):
        async def execute(self, task):
            self.calls.append(task.task_id)
            if task.task_id == "t-back":
                return TaskResult.completed(task, output={"content": "back"})
            answer = await client.call("writer", task)
            return TaskResult.completed(task, output={"back": answer.status})

    runtime = AgentRuntime(agent="researcher", executor=Delegates(), max_concurrent_tasks=1)
    holder["runtime"] = runtime

    result = await asyncio.wait_for(runtime.run(make_task(task_id="t-out")), timeout=5)

    assert result.output == {"back": TaskStatus.COMPLETED}
    assert runtime.semaphore._value == 1  # 돌아와서 다시 잡은 칸도 놓였다


class CancelsSlowly(FakeExecutor):
    """첫 실행은 끝나지 않고, 취소되면 정리 문이 열릴 때까지 정리한다. 뒤 실행은 바로 끝난다."""

    def __init__(self) -> None:
        super().__init__()
        self.cleaning = asyncio.Event()
        self.cleaned = asyncio.Event()
        self.finished_cleanup = False

    async def execute(self, task):
        self.calls.append(task.task_id)
        if len(self.calls) > 1:
            return TaskResult.completed(task, output={"run": len(self.calls)})
        self.running.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cleaning.set()
            await self.cleaned.wait()
            self.finished_cleanup = True
            raise


async def test_cancel_does_not_cut_the_cleanup_of_a_run_already_canceling():
    """마지막 호출자가 떠나 끊기는 중인 실행에 /cancel 이 다시 걸면 정리가 잘린다 (#416)."""
    executor = CancelsSlowly()
    runtime = AgentRuntime(agent="researcher", executor=executor)
    calling = asyncio.create_task(runtime.run(make_task(task_id="t-c")))
    await reached(executor.running, calling)
    calling.cancel()
    with pytest.raises(asyncio.CancelledError):
        await calling
    await until(executor.cleaning.is_set)

    assert runtime.cancel("t-c") is True
    await spin(10)
    executor.cleaned.set()
    await until(lambda: not runtime._inflight)

    assert executor.finished_cleanup


async def test_a_replacement_run_waits_for_the_dying_run_before_taking_a_slot():
    """끊기는 실행이 정리하며 칸을 쥔 동안 대체 실행이 칸을 하나 더 잡지 않는다 (#416)."""
    executor = CancelsSlowly()
    runtime = AgentRuntime(agent="researcher", executor=executor, max_concurrent_tasks=2)
    task = make_task(task_id="t-r")
    calling = asyncio.create_task(runtime.run(task))
    await reached(executor.running, calling)
    calling.cancel()
    with pytest.raises(asyncio.CancelledError):
        await calling
    await until(executor.cleaning.is_set)

    retry = asyncio.create_task(runtime.run(task))
    another = asyncio.create_task(runtime.run(task))
    await spin(10)
    assert runtime.semaphore._value == 1  # 정리 중인 실행의 칸 하나뿐
    assert executor.calls == ["t-r"]  # 정리 중에 다시 돌지 않는다
    executor.cleaned.set()

    results = await asyncio.wait_for(asyncio.gather(retry, another), timeout=5)
    assert [r.status for r in results] == [TaskStatus.COMPLETED, TaskStatus.COMPLETED]
    assert executor.calls == ["t-r", "t-r"]  # 둘째 재시도는 대체 실행에 합류했다


async def test_a_timeout_error_from_the_executor_is_not_reported_as_a_queue_timeout():
    """실행이 던진 TimeoutError 는 칸 대기 마감이 아니다 — 기존 경계 (INTERNAL_001) 로 간다."""
    client, _ = make_client(FakeExecutor(error=TimeoutError()), raise_server_exceptions=False)

    response = client.post("/v1/invoke", json=task_payload(), headers=AUTH)

    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_001"


class Chatty(FakeExecutor):
    """끝없이 토큰을 낸다 — 소비자가 읽지 않으면 producer 가 큐에서 막힌다."""

    async def stream(self, task):
        self.calls.append(task.task_id)
        n = 0
        while True:
            n += 1
            yield TokenEvent(task_id=task.task_id, text=str(n))


async def test_a_stream_nobody_reads_frees_its_slot_at_the_deadline_and_ends_with_to_001():
    """느린 소비자가 producer 를 큐에 묶어도 마감에 소스를 닫고 칸을 놓는다 (#416)."""
    executor = Chatty()
    runtime = AgentRuntime(agent="researcher", executor=executor, max_concurrent_tasks=1)
    payload = task_payload(task_id="t-s", config=TaskConfig(timeout_s=0.1))
    call = StreamCall(create_app(runtime, token=TOKEN), payload, hold=True)

    await until(lambda: runtime.semaphore._value == 1 and executor.calls == ["t-s"])
    assert not call.running.done()  # 응답은 아직 소비자에게 막혀 있다
    call.release.set()
    await asyncio.wait_for(call.running, timeout=5)

    assert call.frames[-1]["type"] == "error"
    assert call.frames[-1]["error"]["code"] == "TO_001"
