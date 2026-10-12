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
