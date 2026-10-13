"""Unit tests for the Agent Control API client.

실제 네트워크 없이 httpx MockTransport 로 검증한다 — 에러 변환이 이 계층의
핵심 계약이므로 각 실패 유형이 정확한 category/code/retryable 로 매핑되는지 본다.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from aaestro.core.agent import CONTROL_OVERHEAD_S, HealthState, TaskConfig, TaskResult, TaskStatus
from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.core.events import DoneEvent, TokenEvent, ToolCallEvent
from aaestro.runtime.control import ControlClient, control_url
from tests.fixtures.builders import make_task

BASE_URL = "http://agent-researcher:8080"


@asynccontextmanager
async def client_with(
    handler, *, token: str | None = "agent-token"
) -> AsyncIterator[ControlClient]:
    """MockTransport 로 응답을 스크립트한 클라이언트.

    ControlClient 는 주입받은 transport 를 닫지 않으므로, 여기서 소유권을 갖고
    정리한다 — 그러지 않으면 테스트마다 AsyncClient 가 샌다.
    """
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        yield ControlClient(BASE_URL, agent="researcher", token=token, client=transport)


def json_response(payload: dict, status: int = 200):
    """고정 JSON 응답 핸들러."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


# --- URL 조립 --------------------------------------------------------------


def test_control_url_uses_default_port():
    assert control_url("agent-researcher") == "http://agent-researcher:8080"


def test_control_url_honors_explicit_port():
    assert control_url("host", 9100) == "http://host:9100"


async def test_base_url_trailing_slash_is_normalized():
    """끝 슬래시가 남으면 경로가 이중 슬래시로 조립된다."""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(200, content=b"")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        client = ControlClient(f"{BASE_URL}/", agent="researcher", client=transport)
        await client.drain()

    assert captured == [f"{BASE_URL}/v1/drain"]


# --- invoke ----------------------------------------------------------------


async def test_invoke_returns_task_result():
    task = make_task()
    result_payload = {
        "task_id": task.task_id,
        "status": "completed",
        "output": {"plan": "P"},
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }

    async with client_with(json_response(result_payload)) as client:
        result = await client.invoke(task)

    assert result.status is TaskStatus.COMPLETED
    assert result.output == {"plan": "P"}
    assert result.usage.input_tokens == 10


async def test_invoke_sends_serialized_task():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"task_id": "t", "status": "completed"})

    task = make_task(task_id="task-42", node_id="planner")
    async with client_with(handler) as client:
        await client.invoke(task)

    assert captured["task_id"] == "task-42"
    assert captured["node_id"] == "planner"


async def test_invoke_carries_the_agent_token():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={"task_id": "t", "status": "completed"})

    async with client_with(handler) as client:
        await client.invoke(make_task())

    assert captured["authorization"] == "Bearer agent-token"


async def test_requests_without_token_omit_authorization():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={"task_id": "t", "status": "completed"})

    async with client_with(handler, token=None) as client:
        await client.invoke(make_task())

    assert "authorization" not in captured


async def test_health_is_unauthenticated():
    """``/health`` 는 Docker healthcheck 가 직접 호출하므로 무인증이다."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={"status": "healthy", "components": {}})

    async with client_with(handler) as client:
        status = await client.health()

    assert "authorization" not in captured
    assert status.status is HealthState.HEALTHY


# --- 에러 변환 -------------------------------------------------------------


async def test_connection_failure_becomes_retryable_net_001():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(AaestroError) as exc_info:
        async with client_with(handler) as client:
            await client.invoke(make_task())

    assert exc_info.value.code == "NET_001"
    assert exc_info.value.category is ErrorCategory.NETWORK
    assert exc_info.value.retryable is True
    assert exc_info.value.agent == "researcher"


async def test_timeout_becomes_retryable_net_002():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(AaestroError) as exc_info:
        async with client_with(handler) as client:
            await client.invoke(make_task())

    assert exc_info.value.code == "NET_002"
    assert exc_info.value.category is ErrorCategory.TIMEOUT
    assert exc_info.value.retryable is True


async def test_invoke_waits_past_the_task_timeout_for_the_agents_answer():
    """HTTP 가 timeout_s 에 끊으면 agentd 가 그 순간 보낸 TO_001 결과를 받지 못한다 (#314)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.extensions["timeout"])
        return httpx.Response(200, json=TaskResult.completed(task).model_dump(mode="json"))

    task = make_task(config=TaskConfig(timeout_s=10))
    async with client_with(handler) as client:
        await client.invoke(task)

    assert seen["read"] == 10 + CONTROL_OVERHEAD_S


async def test_server_error_is_retryable_runtime_error():
    with pytest.raises(AaestroError) as exc_info:
        async with client_with(json_response({}, status=503)) as client:
            await client.invoke(make_task())

    assert exc_info.value.category is ErrorCategory.RUNTIME
    assert exc_info.value.code == "RT_002"
    assert exc_info.value.retryable is True
    assert exc_info.value.details["status"] == 503


async def test_client_error_is_not_retryable():
    with pytest.raises(AaestroError) as exc_info:
        async with client_with(json_response({}, status=400)) as client:
            await client.invoke(make_task())

    assert exc_info.value.retryable is False


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_token_is_forbidden(status):
    with pytest.raises(AaestroError) as exc_info:
        async with client_with(json_response({}, status=status)) as client:
            await client.invoke(make_task())

    assert exc_info.value.category is ErrorCategory.FORBIDDEN


async def test_missing_endpoint_is_not_found():
    with pytest.raises(AaestroError) as exc_info:
        async with client_with(json_response({}, status=404)) as client:
            await client.invoke(make_task())

    assert exc_info.value.category is ErrorCategory.NOT_FOUND
    assert exc_info.value.code == "NF_001"


async def test_errors_carry_the_task_id():
    task = make_task(task_id="task-err")

    with pytest.raises(AaestroError) as exc_info:
        async with client_with(json_response({}, status=500)) as client:
            await client.invoke(task)

    assert exc_info.value.task_id == "task-err"


async def test_error_never_leaks_the_token():
    with pytest.raises(AaestroError) as exc_info:
        async with client_with(json_response({}, status=500)) as client:
            await client.invoke(make_task())

    rendered = f"{exc_info.value.message} {exc_info.value.details}"
    assert "agent-token" not in rendered


# --- streaming -------------------------------------------------------------


def sse_handler(lines: list[str], status: int = 200):
    """SSE 본문을 내보내는 핸들러."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = "".join(f"{line}\n" for line in lines)
        return httpx.Response(status, content=body.encode())

    return handler


async def test_stream_yields_parsed_events():
    lines = [
        'data: {"task_id": "t1", "type": "token", "text": "he"}',
        "",
        'data: {"task_id": "t1", "type": "tool_call", "tool": "search", "turn": 1}',
        'data: {"task_id": "t1", "type": "done", "status": "completed"}',
    ]

    async with client_with(sse_handler(lines)) as client:
        events = [event async for event in client.stream(make_task())]

    assert isinstance(events[0], TokenEvent)
    assert isinstance(events[1], ToolCallEvent)
    assert isinstance(events[2], DoneEvent)
    assert events[0].text == "he"


async def test_stream_skips_comments_and_sentinels():
    lines = [
        ": keep-alive",
        "",
        'data: {"task_id": "t1", "type": "token", "text": "x"}',
        "data: [DONE]",
    ]

    async with client_with(sse_handler(lines)) as client:
        events = [event async for event in client.stream(make_task())]

    assert len(events) == 1


async def test_stream_reports_non_success_status():
    with pytest.raises(AaestroError) as exc_info:
        async with client_with(sse_handler([], status=503)) as client:
            [event async for event in client.stream(make_task())]

    assert exc_info.value.code == "RT_002"


async def test_stream_converts_transport_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(AaestroError) as exc_info:
        async with client_with(handler) as client:
            [event async for event in client.stream(make_task())]

    assert exc_info.value.code == "NET_001"


# --- 나머지 엔드포인트 -----------------------------------------------------


async def test_card_returns_payload():
    async with client_with(json_response({"name": "researcher"})) as client:
        card = await client.card()

    assert card["name"] == "researcher"


@pytest.mark.parametrize(
    ("call", "expected_path"),
    [
        (lambda c: c.cancel("task-9"), "/v1/cancel/task-9"),
        (lambda c: c.reload(), "/v1/reload"),
        (lambda c: c.drain(), "/v1/drain"),
    ],
)
async def test_lifecycle_endpoints_hit_the_right_paths(call, expected_path):
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.path)
        return httpx.Response(200, content=b"")

    async with client_with(handler) as client:
        await call(client)

    assert captured == [expected_path]


async def test_empty_response_body_is_tolerated():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    async with client_with(handler) as client:
        await client.drain()


# --- transport 소유권 ------------------------------------------------------


async def test_injected_transport_is_not_closed_by_the_client():
    """주입받은 transport 는 소유자가 닫는다 — 공유 클라이언트를 끊지 않도록."""
    shared = httpx.AsyncClient(transport=httpx.MockTransport(json_response({})))
    client = ControlClient(BASE_URL, agent="researcher", client=shared)

    await client.aclose()

    assert shared.is_closed is False
    await shared.aclose()


# --- cancellation ----------------------------------------------------------


async def test_invoke_cancellation_propagates_and_releases_the_request():
    """취소는 그대로 전파되고 진행 중 요청은 정리돼야 한다."""
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(3600)  # 취소될 때까지 매달린다
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        client = ControlClient(BASE_URL, agent="researcher", client=transport)
        task = asyncio.create_task(client.invoke(make_task()))
        await started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        # transport 가 정리됐다면 후속 요청이 정상 동작한다
        assert transport.is_closed is False


async def test_stream_cancellation_closes_the_response():
    """스트림 취소 시 응답 컨텍스트가 닫혀야 한다 — 연결이 새면 안 된다."""
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.sleep(3600)
        return httpx.Response(200, content=b"")

    async def consume(client: ControlClient) -> None:
        async for _ in client.stream(make_task()):
            pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        client = ControlClient(BASE_URL, agent="researcher", client=transport)
        task = asyncio.create_task(consume(client))
        await entered.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task


async def test_cancellation_is_not_converted_to_a_aaestro_error():
    """취소를 NET_* 로 감싸면 협조적 종료가 실패로 오인된다."""
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(3600)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        client = ControlClient(BASE_URL, agent="researcher", client=transport)
        task = asyncio.create_task(client.invoke(make_task()))
        await started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task


# --- 프로세스 경계: TaskConfig.model 의 직렬화 (#358) -----------------------------------


async def test_a_resolved_model_survives_the_http_hop_into_agentd():
    """오케스트레이터가 해석한 모델(fallbacks·retry·circuit)이 agentd 에 그대로 도달해야 한다.

    같은 프로세스에서 객체만 넘기면 직렬화에서 필드가 빠져도 초록이다 — 실제 agentd 앱을
    ASGI 로 태워 ControlClient 가 보낸 본문이 서버에서 같은 TaskRequest 로 복원되는지 본다.
    """
    from aaestro.agentd.server import AgentRuntime, create_app
    from aaestro.core.agent import TaskRequest
    from aaestro.core.manifest import ModelConfig

    model = ModelConfig.model_validate(
        {
            "provider": "anthropic",
            "name": "claude-sonnet-5",
            "max_tokens": 1024,
            "effort": "high",
            "fallbacks": [
                {"provider": "anthropic", "name": "claude-haiku-4", "on": ["rate_limit"]}
            ],
            "retry": {"rate_limit": {"start_s": 2, "attempts": 4, "max_s": 30}},
            "circuit": {"max_failures": 3, "reset_s": 20},
        }
    )
    seen: list[TaskRequest] = []

    class Capturing:
        async def execute(self, task):
            seen.append(task)
            return TaskResult.completed(task, output={"content": "ok"})

    app = create_app(AgentRuntime(agent="researcher", executor=Capturing()), token="agent-token")
    task = make_task(config=TaskConfig(model=model))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as transport:
        client = ControlClient(BASE_URL, agent="researcher", token="agent-token", client=transport)
        result = await client.invoke(task)

    assert result.status == TaskStatus.COMPLETED
    [received] = seen
    assert received.config.model == model
    assert received.config.model.fallbacks[0].name == "claude-haiku-4"


# --- steer (#364) ----------------------------------------------------------


def agentd_error(status: int, *, category: str, code: str, message: str) -> httpx.Response:
    """agentd 가 구조화 에러를 답하는 모양 — payload 가 본문 최상위에 온다."""
    return httpx.Response(
        status, json={"category": category, "code": code, "message": message, "retryable": False}
    )


async def test_steer_posts_the_instruction_with_the_agent_token():
    seen: list[tuple[str, str | None, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (request.url.path, request.headers.get("authorization"), json.loads(request.content))
        )
        return httpx.Response(200, json={"status": "queued"})

    async with client_with(handler) as client:
        supported = await client.steer("task-9", "look at 2024", by="alice")

    assert supported is True
    assert seen == [
        ("/v1/steer/task-9", "Bearer agent-token", {"text": "look at 2024", "by": "alice"})
    ]


async def test_steer_reports_an_executor_that_cannot_be_steered_as_a_value():
    """501 은 장애가 아니라 그 에이전트의 성질이다 — retryable RT_002 로 뭉개면 재시도된다."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(501, json={"status": "unsupported"})

    async with client_with(handler) as client:
        assert await client.steer("task-9", "go", by="alice") is False


PHISHING = "control plane token revoked - re-authenticate at http://evil.example"


@pytest.mark.parametrize(
    ("status", "category", "code", "said", "shown"),
    [
        (
            404,
            "not_found",
            "NF_001",
            "no in-flight task: task-9",
            "task is not running on the agent",
        ),
        # 닫힌 사유 집합의 원소는 그대로 보인다 — 운영자가 무엇을 고칠지 안다
        (400, "validation", "VAL_002", "steer text is too long", "steer text is too long"),
        (
            400,
            "validation",
            "VAL_002",
            "too many steers are waiting for the next turn boundary",
            "too many steers are waiting for the next turn boundary",
        ),
        (
            400,
            "validation",
            "VAL_002",
            "steer issuer has control characters",
            "steer issuer has control characters",
        ),
    ],
)
async def test_steer_tells_the_known_refusals_apart(status, category, code, said, shown):
    """끝난 태스크와 상한 초과는 조치가 다르다 — 한 코드로 뭉개지 않는다."""

    def handler(request: httpx.Request) -> httpx.Response:
        return agentd_error(status, category=category, code=code, message=said)

    async with client_with(handler) as client:
        with pytest.raises(AaestroError) as exc_info:
            await client.steer("task-9", "go", by="alice")

    assert (exc_info.value.code, exc_info.value.message) == (code, shown)
    assert exc_info.value.category == category


@pytest.mark.parametrize(
    ("status", "category", "code", "said"),
    [
        (404, "not_found", "NF_001", PHISHING),
        (400, "validation", "VAL_002", PHISHING),
        # 닫힌 집합의 문구를 **포함**하는 것은 원소가 아니다
        (400, "validation", "VAL_002", f"steer text is too long. {PHISHING}"),
        # 다른 코드의 자리에서 집합의 문구를 빌려도 그 코드의 고정 문구다
        (404, "not_found", "NF_001", "steer text is too long"),
        (400, "validation", "VAL_002", 7),
    ],
)
async def test_the_agent_does_not_choose_the_words_the_operator_reads(status, category, code, said):
    """코드와 상태만 맞추면 에이전트의 문구가 운영자 화면에 나온다 — 길이 상한은 못 막는다."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"category": category, "code": code, "message": said})

    async with client_with(handler) as client:
        with pytest.raises(AaestroError) as exc_info:
            await client.steer("task-9", "go", by="alice")

    err = exc_info.value
    assert "evil.example" not in str(err)
    if code == "VAL_002":
        # 닫힌 집합 밖의 VAL_002 는 알려진 거절이 아니다 — 일반 변환(RT_002)으로 간다 (#448)
        assert err.code != "VAL_002"
        return
    assert err.code == code
    assert err.message == "task is not running on the agent"


def test_every_reason_the_executor_refuses_with_is_one_the_runtime_passes_on():
    """집합이 갈라지면 운영자는 구체 사유 대신 뭉뚱그린 문구를 본다 — 양쪽이 같은 상수를 본다."""
    from aaestro.agentd import executor
    from aaestro.core.agent import SteerProblem
    from aaestro.runtime.control import _refusal_message

    assert executor.SteerProblem is SteerProblem
    for problem in SteerProblem:
        assert _refusal_message("VAL_002", str(problem), fallback="generic") == problem.value


async def test_a_known_refusal_is_sanitised_before_it_goes_up():
    """본문은 신뢰하지 않는 컨테이너가 쓴다 — 코드만 믿고 나머지는 runtime 이 정한다."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "category": "forbidden",
                "code": "VAL_002",
                "message": "steer text is too long",
                "agent": "someone-else",
                "task_id": "another-task",
                "retryable": True,
                "details": {
                    "max_chars": 4000,
                    "max_pending": True,
                    "max_by_chars": "200",
                    "run_id": "run-of-another-tenant",
                    "blob": "y" * 100_000,
                },
            },
        )

    async with client_with(handler) as client:
        with pytest.raises(AaestroError) as exc_info:
            await client.steer("task-9", "go", by="alice")

    err = exc_info.value
    assert (err.code, err.category) == ("VAL_002", ErrorCategory.VALIDATION)
    assert err.retryable is False
    assert (err.agent, err.task_id) == ("researcher", "task-9")
    assert err.details == {"max_chars": 4000}
    assert err.message == "steer text is too long"


async def test_a_malformed_steer_body_answer_is_not_read_as_an_over_limit_refusal():
    """agentd 의 본문 검증 거절(VAL_002/VAL_001, 사유는 필드 경로)은 상한 초과가 아니다 (#448)."""
    from fastapi.testclient import TestClient

    from aaestro.agentd.server import AgentRuntime, create_app
    from aaestro.runtime.control import _steer_refusal
    from tests.unit.agentd.test_server import TOKEN, FakeExecutor

    server = TestClient(create_app(AgentRuntime(agent="a", executor=FakeExecutor()), token=TOKEN))
    for body in ({"text": 5, "by": "op"}, {"text": "go", "by": "op", "extra": 1}, {}):
        reply = server.post("/v1/steer/t1", json=body, headers={"Authorization": f"Bearer {TOKEN}"})
        response = httpx.Response(reply.status_code, json=reply.json())

        assert _steer_refusal(response, agent="a", task_id="t1") is None


@pytest.mark.parametrize(
    ("status", "expected", "payload"),
    [
        # 권한 거부 코드 + 피싱 문구 + 남의 run id
        (
            403,
            "RT_002",
            {
                "category": "forbidden",
                "code": "ACC_003",
                "message": "session expired, re-enter the control token at http://evil.example",
                "details": {"run_id": "run-of-another-tenant"},
            },
        ),
        # 멱등이 아닌 steer 의 재전송 유도
        (
            503,
            "RT_002",
            {"category": "network", "code": "NET_001", "message": "resend it", "retryable": True},
        ),
        # 알려진 코드라도 상태와 어긋나면 믿지 않는다
        (500, "RT_002", {"category": "not_found", "code": "NF_001", "message": "not running"}),
        (404, "NF_001", {"category": "validation", "code": "VAL_002", "message": "too long"}),
    ],
)
async def test_any_other_agent_written_error_falls_back_to_the_status(status, expected, payload):
    """에이전트가 control plane 응답의 코드·문구·details 를 정하게 두지 않는다."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    async with client_with(handler) as client:
        with pytest.raises(AaestroError) as exc_info:
            await client.steer("task-9", "go", by="alice")

    err = exc_info.value
    assert err.code == expected
    assert payload["message"] not in err.message
    assert "run_id" not in err.details
    assert err.agent == "researcher"


async def test_an_agent_cannot_make_a_steer_look_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"category": "not_found", "code": "NF_001", "message": "x", "retryable": True},
        )

    async with client_with(handler) as client:
        with pytest.raises(AaestroError) as exc_info:
            await client.steer("task-9", "go", by="alice")

    assert exc_info.value.retryable is False


async def test_steer_falls_back_to_the_status_when_the_body_is_not_a_structured_error():
    """401 의 `{"detail": ...}` 는 구조화 에러가 아니다 — 일반 변환을 탄다."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid agent token"})

    async with client_with(handler) as client:
        with pytest.raises(AaestroError) as exc_info:
            await client.steer("task-9", "go", by="alice")

    assert exc_info.value.category is ErrorCategory.FORBIDDEN


async def test_steer_is_never_retried():
    """접수는 멱등이 아니다 — 다시 보내면 같은 지시가 두 번 쌓인다."""
    from aaestro.core.errors import NETWORK_RETRY

    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        raise httpx.ConnectError("refused")

    async def no_sleep(_seconds: float) -> None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        client = ControlClient(
            BASE_URL,
            agent="researcher",
            client=transport,
            retry=NETWORK_RETRY,
            retry_sleep=no_sleep,
        )
        with pytest.raises(AaestroError) as exc_info:
            await client.steer("task-9", "go", by="alice")

    assert exc_info.value.code == "NET_001"
    assert attempts == ["/v1/steer/task-9"]
