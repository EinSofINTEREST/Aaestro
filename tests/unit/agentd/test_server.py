"""Unit tests for the Agent Control API server.

컨테이너 없이 FastAPI TestClient 로 검증한다 — 이 계층의 계약은 HTTP 표면
(엔드포인트·인증·동시성·에러 변환)이지 실행 로직이 아니다.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from aaestro.agentd.echo import EchoExecutor
from aaestro.agentd.executor import MAX_STEER_CHARS, STEER_HEADER, Executor, UserTurn
from aaestro.agentd.server import AgentRuntime, create_app
from aaestro.core.agent import ComponentHealth, HealthState, HealthStatus, TaskResult, TaskStatus
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.events import DoneEvent, TokenEvent
from tests.fixtures.builders import make_task
from tests.fixtures.fake_model import FakeModel, FakeTools, calls, text

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

    assert response.status_code == 422


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

    assert response.status_code == 422
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
    await executor.running.wait()

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
        await executor.running.wait()

        canceled = await client.post("/v1/cancel/t-http", headers=AUTH)
        response = await invoking

    assert canceled.status_code == 200
    assert response.json()["status"] == TaskStatus.CANCELED
