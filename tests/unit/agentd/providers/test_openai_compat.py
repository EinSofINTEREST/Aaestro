"""OpenAI Chat Completions compatible provider binding tests.

**실 API 를 호출하지 않는다** — 스크립트된 전송 대역으로만 검증한다 (06 규칙).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from aaestro.agentd.executor import AssistantTurn, ToolCall, ToolOutcome, ToolTurn, UserTurn
from aaestro.agentd.providers.openai_compat import OpenAICompatModel
from aaestro.core.errors import (
    NETWORK_RETRY,
    RATE_LIMIT_RETRY,
    AaestroError,
    ErrorCategory,
    ErrorCode,
)
from aaestro.core.manifest import ModelConfig
from aaestro.core.skill import SkillSpec

MODEL = "gpt-compatible"
BASE_URL = "http://egress:8081/openai-compat"
IDENTITY = "cred-researcher"
OPENING = [UserTurn(parts=("prompt",))]
USAGE = {"prompt_tokens": 120, "completion_tokens": 45}


def completion(
    content: str | None = "done",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    **message_extra: Any,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content, **message_extra}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-1",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": USAGE,
    }


def function_call(identifier: str, name: str, arguments: Any) -> dict[str, Any]:
    return {
        "id": identifier,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


class Upstream:
    """전송 대역 — 요청을 기록하고 스크립트된 응답을 낸다."""

    def __init__(self, result: httpx.Response | Exception | dict[str, Any]) -> None:
        self._result = result
        self.seen: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if isinstance(self._result, Exception):
            raise self._result
        if isinstance(self._result, dict):
            return httpx.Response(200, json=self._result)
        return self._result

    @property
    def body(self) -> dict[str, Any]:
        return json.loads(self.seen[0].content)


def model(result: Any, **overrides: Any) -> tuple[OpenAICompatModel, Upstream]:
    upstream = Upstream(result)
    config = ModelConfig(provider="openai-compat", name=MODEL, **overrides)
    adapter = OpenAICompatModel(
        config=config,
        agent="researcher",
        base_url=BASE_URL,
        credential=IDENTITY,
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle)),
    )
    return adapter, upstream


def failure(status: int, error: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(status, json={"error": error or {"message": "boom"}})


# --- 요청 구성 ---------------------------------------------------------------


async def test_run_posts_to_the_chat_completions_path_with_the_bearer_identity():
    adapter, upstream = model(completion())

    await adapter.run("", OPENING, [])

    [sent] = upstream.seen
    assert (sent.method, str(sent.url)) == ("POST", f"{BASE_URL}/v1/chat/completions")
    assert sent.headers["authorization"] == f"Bearer {IDENTITY}"


async def test_run_tolerates_a_trailing_slash_in_the_base_url():
    adapter, upstream = model(completion())
    adapter.base_url = f"{BASE_URL}/"

    await adapter.run("", OPENING, [])

    assert str(upstream.seen[0].url) == f"{BASE_URL}/v1/chat/completions"


async def test_run_manifest_decides_the_model():
    adapter, upstream = model(completion())

    await adapter.run("", OPENING, [])

    assert upstream.body["model"] == MODEL


async def test_run_undeclared_limits_are_not_sent():
    """anthropic 과 달리 max_tokens 가 필수가 아니다 — 선언이 없으면 서버 기본값을 쓴다."""
    adapter, upstream = model(completion())

    await adapter.run("", OPENING, [])

    assert "max_tokens" not in upstream.body


async def test_run_declared_max_tokens_reaches_the_request():
    adapter, upstream = model(completion(), max_tokens=2048)

    await adapter.run("", OPENING, [])

    assert upstream.body["max_tokens"] == 2048


async def test_run_declared_effort_is_never_sent():
    """fallback 은 primary 의 effort 를 물려받는다 — 400 으로 거절되면 전환이 무너진다."""
    adapter, upstream = model(completion(), effort="high")

    await adapter.run("", OPENING, [])

    assert "reasoning_effort" not in upstream.body and "effort" not in json.dumps(upstream.body)


def test_declared_effort_is_reported_once_when_the_adapter_is_built():
    """보내지 않는다는 사실이 조용하면 운영자가 선언이 왜 효과가 없는지 찾지 못한다."""
    with capture_logs() as logs:
        adapter, _ = model(completion(), effort="high")

    [entry] = logs
    assert entry["log_level"] == "warning"
    assert entry["event"] == "effort is not forwarded to this provider"
    assert (entry["agent"], entry["provider"], entry["model"], entry["effort"]) == (
        "researcher",
        "openai-compat",
        MODEL,
        "high",
    )
    assert adapter.config.effort == "high"


def test_undeclared_effort_logs_nothing_when_the_adapter_is_built():
    with capture_logs() as logs:
        model(completion())

    assert logs == []


async def test_run_system_is_its_own_message_not_part_of_the_user_turn():
    adapter, upstream = model(completion())

    await adapter.run("operator rules", [UserTurn(parts=("recalled", "prompt"))], [])

    system, user = upstream.body["messages"]
    assert system == {"role": "system", "content": "operator rules"}
    assert [part["text"] for part in user["content"]] == ["recalled", "prompt"]


async def test_run_no_system_message_when_the_promptset_has_none():
    adapter, upstream = model(completion())

    await adapter.run("", OPENING, [])

    assert [message["role"] for message in upstream.body["messages"]] == ["user"]


async def test_run_tool_schemas_keep_the_mcp_namespace():
    adapter, upstream = model(completion())
    spec = SkillSpec(
        name="mcp__fs__read_file",
        description="read a file",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )

    await adapter.run("", OPENING, [spec])

    assert upstream.body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "mcp__fs__read_file",
                "description": "read a file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]


async def test_run_a_tool_without_a_schema_declares_an_empty_object():
    adapter, upstream = model(completion())

    await adapter.run("", OPENING, [SkillSpec(name="ping", description="", parameters={})])

    assert upstream.body["tools"][0]["function"]["parameters"] == {
        "type": "object",
        "properties": {},
    }


async def test_run_an_over_long_tool_name_is_sent_unchanged():
    """자르면 모델이 부른 이름이 registry 의 이름과 달라진다 — 거절은 provider 가 한다."""
    name = f"mcp__{'s' * 30}__{'t' * 40}"
    adapter, upstream = model(completion())

    await adapter.run("", OPENING, [SkillSpec(name=name, description="", parameters={})])

    assert upstream.body["tools"][0]["function"]["name"] == name


async def test_run_tools_are_omitted_when_none_are_bound():
    adapter, upstream = model(completion())

    await adapter.run("", OPENING, [])

    assert "tools" not in upstream.body


async def test_run_tool_results_answer_the_call_they_belong_to():
    """도구 호출을 평탄화하지 않는다 — assistant 의 tool_calls 와 role: tool 로 보낸다 (#308)."""
    adapter, upstream = model(completion())
    conversation = [
        *OPENING,
        AssistantTurn(
            content="",
            tool_calls=(
                ToolCall(id="c1", name="search", arguments={"q": "x"}),
                ToolCall(id="c2", name="fetch"),
            ),
        ),
        ToolTurn(
            outcomes=(
                ToolOutcome(call_id="c1", content="found"),
                ToolOutcome(call_id="c2", content="boom", is_error=True),
            )
        ),
    ]

    await adapter.run("", conversation, [])

    _, assistant, first, second = upstream.body["messages"]
    assert assistant == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            function_call("c1", "search", '{"q": "x"}'),
            function_call("c2", "fetch", "{}"),
        ],
    }
    assert first == {"role": "tool", "tool_call_id": "c1", "content": "found"}
    # is_error 자리가 없는 API 다 — 표시가 사라지면 모델이 실패를 성공으로 읽는다
    assert second == {"role": "tool", "tool_call_id": "c2", "content": "[tool error]\nboom"}


async def test_run_its_own_assistant_message_goes_back_unchanged():
    """provider 고유 필드가 다음 턴에 그대로 돌아가야 한다."""
    calls = [function_call("c1", "search", '{"q":"x"}')]
    adapter, upstream = model(completion(None, tool_calls=calls, reasoning_content="thinking"))
    first = await adapter.run("", OPENING, [])

    turn = AssistantTurn(first.content, first.tool_calls, raw=first.raw)
    await adapter.run("", [*OPENING, turn], [])

    assert json.loads(upstream.seen[1].content)["messages"][1] == {
        "role": "assistant",
        "content": None,
        "tool_calls": calls,
        "reasoning_content": "thinking",
    }


async def returned_assistant(first_response: dict[str, Any]) -> dict[str, Any]:
    """첫 응답을 받은 뒤, 그 턴을 되돌려 보낸 다음 요청의 assistant 메시지."""
    adapter, upstream = model(first_response)
    first = await adapter.run("", OPENING, [])
    turn = AssistantTurn(first.content, first.tool_calls, raw=first.raw)
    results = ToolTurn(
        outcomes=tuple(ToolOutcome(call_id=call.id, content="ok") for call in first.tool_calls)
    )

    await adapter.run("", [*OPENING, turn, results], [])

    return json.loads(upstream.seen[1].content)["messages"][1]


@pytest.mark.parametrize(
    ("received", "sent_back"),
    [
        pytest.param("", "{}", id="empty"),
        pytest.param("   ", "{}", id="blank"),
        pytest.param("\n", "{}", id="newline"),
        pytest.param({}, "{}", id="empty-object"),
        pytest.param({"q": "x", "n": 2}, '{"q": "x", "n": 2}', id="object"),
    ],
)
async def test_run_returned_tool_arguments_are_always_a_json_object_string(received, sent_back):
    """받을 때 관대하게 읽은 것을 그대로 내보내면 엄격한 서버가 다음 턴을 400 으로 거절한다.

    그 400 은 설정 오류로 전파된다 — 도구가 이미 실행된 뒤에 태스크가 실패한다.
    """
    response = completion(None, tool_calls=[function_call("c1", "ping", received)])

    assistant = await returned_assistant(response)

    [call] = assistant["tool_calls"]
    assert call["function"]["arguments"] == sent_back
    assert isinstance(json.loads(call["function"]["arguments"]), dict)
    assert (call["id"], call["type"], call["function"]["name"]) == ("c1", "function", "ping")


async def test_run_returned_tool_arguments_match_what_the_tool_was_called_with():
    adapter, _ = model(completion(None, tool_calls=[function_call("c1", "ping", {"q": "x"})]))

    first = await adapter.run("", OPENING, [])

    [sent] = first.raw.message["tool_calls"]
    assert json.loads(sent["function"]["arguments"]) == first.tool_calls[0].arguments


async def test_run_well_formed_tool_calls_and_provider_fields_go_back_byte_for_byte():
    """과잉 변경 방지 — 이미 맞는 인자 문자열은 공백 하나도 다시 쓰지 않는다."""
    untouched = '{ "q":"x",  "nested": {"a": [1,2]} }'
    calls = [
        {**function_call("c1", "search", untouched), "index": 0, "vendor_field": {"k": 1}},
        function_call("c2", "ping", ""),
    ]
    calls[0]["function"]["vendor_note"] = "keep"
    response = completion(None, tool_calls=calls, reasoning_content="thinking", refusal=None)

    assistant = await returned_assistant(response)

    kept, fixed = assistant["tool_calls"]
    assert kept == calls[0] and kept["function"]["arguments"] == untouched
    assert fixed == function_call("c2", "ping", "{}")
    assert (assistant["reasoning_content"], assistant["refusal"]) == ("thinking", None)
    assert assistant["role"] == "assistant" and assistant["content"] is None


async def test_run_the_response_message_is_not_modified_in_place():
    """응답 객체는 공유된다 — 보관용으로 고칠 때 받은 dict 를 건드리지 않는다."""
    received = {
        "role": "assistant",
        "content": None,
        "tool_calls": [function_call("c1", "ping", "")],
    }
    snapshot = json.dumps(received)

    from aaestro.agentd.providers.openai_compat import _returnable

    kept = _returnable(received)

    assert json.dumps(received) == snapshot
    assert kept is not received and kept["tool_calls"][0]["function"]["arguments"] == "{}"


def test_a_message_that_needs_no_change_is_kept_as_the_same_object():
    from aaestro.agentd.providers.openai_compat import _returnable

    text_only = {"role": "assistant", "content": "done"}
    well_formed = {"role": "assistant", "tool_calls": [function_call("c1", "ping", "{}")]}

    assert _returnable(text_only) is text_only
    assert _returnable(well_formed) is well_formed


async def test_run_blank_arguments_string_is_a_call_without_arguments():
    """보낼 때 "{}" 로 맞추는 것과 같은 기준으로 읽는다 — 공백뿐인 문자열도 인자 없는 호출이다."""
    adapter, _ = model(completion(None, tool_calls=[function_call("c1", "ping", "  \n")]))

    response = await adapter.run("", OPENING, [])

    assert response.tool_calls == (ToolCall(id="c1", name="ping", arguments={}),)


async def test_run_another_providers_assistant_turn_is_rebuilt():
    """fallback 으로 넘어온 턴 — 남의 원본(Anthropic 블록 리스트)을 그대로 보내지 않는다."""
    adapter, upstream = model(completion())
    foreign = [object(), object()]
    turn = AssistantTurn(
        content="let me look",
        tool_calls=(ToolCall(id="toolu_1", name="search", arguments={"q": 1}),),
        raw=foreign,
    )

    await adapter.run("", [*OPENING, turn], [])

    assert upstream.body["messages"][1] == {
        "role": "assistant",
        "content": "let me look",
        "tool_calls": [function_call("toolu_1", "search", '{"q": 1}')],
    }


# --- 응답 해석 ---------------------------------------------------------------


async def test_run_text_response_is_final():
    adapter, _ = model(completion("answer"))

    response = await adapter.run("", OPENING, [])

    assert (response.content, response.is_final) == ("answer", True)


async def test_run_tool_calls_parse_their_json_arguments():
    adapter, _ = model(
        completion(
            None,
            tool_calls=[function_call("c1", "mcp__fs__read_file", '{"path": "a.txt"}')],
            finish_reason="tool_calls",
        )
    )

    response = await adapter.run("", OPENING, [])

    assert response.tool_calls == (
        ToolCall(id="c1", name="mcp__fs__read_file", arguments={"path": "a.txt"}),
    )
    assert response.content == "" and not response.is_final


async def test_run_empty_arguments_string_is_a_call_without_arguments():
    """인자 없는 호출을 빈 문자열로 내는 호환 서버가 흔하다."""
    adapter, _ = model(completion(None, tool_calls=[function_call("c1", "ping", "")]))

    response = await adapter.run("", OPENING, [])

    assert response.tool_calls == (ToolCall(id="c1", name="ping", arguments={}),)


ANSWER = {"role": "assistant", "content": "answer"}


@pytest.mark.parametrize(
    ("usage", "counted"),
    [
        pytest.param(..., (0, 0), id="missing"),
        pytest.param(None, (0, 0), id="null"),
        pytest.param({}, (0, 0), id="empty"),
        pytest.param("12 tokens", (0, 0), id="not-an-object"),
        pytest.param({"prompt_tokens": 7}, (7, 0), id="completion-missing"),
        pytest.param({"completion_tokens": 3}, (0, 3), id="prompt-missing"),
        pytest.param({"prompt_tokens": None, "completion_tokens": 3}, (0, 3), id="field-null"),
        pytest.param({"prompt_tokens": "7", "completion_tokens": 3}, (0, 3), id="field-text"),
        pytest.param({"prompt_tokens": -5, "completion_tokens": 3}, (0, 3), id="field-negative"),
        pytest.param({"prompt_tokens": True, "completion_tokens": 3}, (0, 3), id="field-bool"),
        pytest.param({"prompt_tokens": 7.5, "completion_tokens": 3}, (0, 3), id="field-float"),
    ],
)
async def test_run_unreadable_usage_counts_zero_and_warns(usage, counted):
    """집계가 없거나 깨졌다고 멀쩡한 답을 버리지 않는다 — 대신 조용하지 않게 남긴다."""
    payload: dict[str, Any] = {"choices": [{"message": ANSWER}]}
    if usage is not ...:
        payload["usage"] = usage
    adapter, _ = model(payload)

    with capture_logs() as logs:
        response = await adapter.run("", OPENING, [])

    assert response.content == "answer"
    assert (response.usage.input_tokens, response.usage.output_tokens) == counted
    [entry] = logs
    assert entry["log_level"] == "warning"
    assert entry["event"] == "provider response carries no readable token usage, counting zero"
    assert (entry["agent"], entry["provider"], entry["model"]) == (
        "researcher",
        "openai-compat",
        MODEL,
    )


async def test_run_zero_usage_is_a_readable_count_and_does_not_warn():
    """0 은 읽은 값이다 — 못 읽은 것과 같은 경고를 내면 경고가 뜻을 잃는다."""
    adapter, _ = model(
        {"choices": [{"message": ANSWER}], "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
    )

    with capture_logs() as logs:
        await adapter.run("", OPENING, [])

    assert logs == []


async def test_run_usage_is_collected():
    adapter, _ = model(completion())

    response = await adapter.run("", OPENING, [])

    assert (response.usage.input_tokens, response.usage.output_tokens) == (120, 45)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"choices": [], "usage": USAGE}, id="no-choices"),
        pytest.param({"usage": USAGE}, id="choices-missing"),
        pytest.param({"choices": [{"message": "text"}], "usage": USAGE}, id="message-not-object"),
        pytest.param(
            {"choices": [{"message": {"content": [{"type": "text"}]}}], "usage": USAGE},
            id="content-not-text",
        ),
        pytest.param(
            completion(None, tool_calls=[function_call("c1", "search", '{"q": ')]),
            id="arguments-truncated-json",
        ),
        pytest.param(
            completion(None, tool_calls=[function_call("c1", "search", "[1, 2]")]),
            id="arguments-not-an-object",
        ),
        pytest.param(
            completion(None, tool_calls=[function_call("c1", "", "{}")]), id="tool-name-empty"
        ),
        pytest.param(
            completion(None, tool_calls=[{"id": "c1", "type": "function"}]), id="function-missing"
        ),
    ],
)
async def test_run_unreadable_response_raises_llm_004(payload):
    adapter, _ = model(payload)

    with pytest.raises(AaestroError) as exc_info:
        await adapter.run("", OPENING, [])

    error = exc_info.value
    assert (error.category, error.code) == (ErrorCategory.MODEL, ErrorCode.LLM_004)
    assert error.retryable is False and error.agent == "researcher"


async def test_run_non_json_success_body_raises_llm_004():
    adapter, _ = model(httpx.Response(200, text="<html>gateway</html>"))

    with pytest.raises(AaestroError) as exc_info:
        await adapter.run("", OPENING, [])

    assert exc_info.value.code == ErrorCode.LLM_004


async def test_run_truncated_text_is_returned_with_a_warning():
    """형제 어댑터처럼 응답은 돌려주되, 출력 상한에서 끊겼다는 사실을 남긴다."""
    adapter, _ = model(completion("half an ans", finish_reason="length"))

    with capture_logs() as logs:
        response = await adapter.run("", OPENING, [])

    assert response.content == "half an ans"
    [entry] = logs
    assert entry["log_level"] == "warning"
    assert (entry["agent"], entry["provider"], entry["model"]) == (
        "researcher",
        "openai-compat",
        MODEL,
    )
    assert entry["output_tokens"] == 45


async def test_run_complete_response_logs_nothing():
    adapter, _ = model(completion())

    with capture_logs() as logs:
        await adapter.run("", OPENING, [])

    assert logs == []


# --- 실패 변환 ---------------------------------------------------------------


async def test_run_rate_limited_raises_retryable_llm_001_in_its_own_category():
    adapter, _ = model(failure(429))

    with pytest.raises(AaestroError) as exc_info:
        await adapter.run("", OPENING, [])

    error = exc_info.value
    assert (error.category, error.code, error.retryable) == (
        ErrorCategory.RATE_LIMIT,
        ErrorCode.LLM_001,
        True,
    )
    # 카테고리가 틀리면 RATE_LIMIT_RETRY 가 이 실패를 집지 않는다
    assert error.category in RATE_LIMIT_RETRY.retryable_categories


@pytest.mark.parametrize(
    "error",
    [
        pytest.param({"code": "context_length_exceeded", "message": "x"}, id="openai-code"),
        pytest.param(
            {"message": "This model's maximum context length is 8192 tokens"}, id="vllm-phrase"
        ),
        pytest.param({"message": "Prompt is too long for the context window"}, id="phrase-only"),
    ],
)
async def test_run_context_overflow_raises_llm_002_and_is_not_retryable(error):
    adapter, _ = model(failure(400, error))

    with pytest.raises(AaestroError) as exc_info:
        await adapter.run("", OPENING, [])

    assert (exc_info.value.category, exc_info.value.code, exc_info.value.retryable) == (
        ErrorCategory.MODEL,
        ErrorCode.LLM_002,
        False,
    )


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(
            failure(400, {"code": "invalid_request_error", "message": "unknown parameter"}),
            id="other-400",
        ),
        pytest.param(httpx.Response(400, text="bad request"), id="400-not-json"),
        pytest.param(httpx.Response(400, json=["unexpected"]), id="400-not-an-object"),
        pytest.param(failure(401, {"message": "Incorrect API key provided"}), id="401"),
        pytest.param(failure(403, {"message": "denied", "code": "ACC_001"}), id="403-proxy"),
        pytest.param(failure(404, {"message": "model not found"}), id="404"),
        pytest.param(
            failure(413, {"code": "context_length_exceeded"}), id="overflow-code-outside-400"
        ),
    ],
)
async def test_run_configuration_errors_propagate_unchanged(response):
    """anthropic 어댑터와 같은 기준 — 재시도·fallback 이 못 고치는 실패를 덮지 않는다."""
    adapter, _ = model(response)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await adapter.run("", OPENING, [])

    assert exc_info.value.response.status_code == response.status_code


async def test_run_redirect_is_not_followed():
    """리다이렉트를 따라가면 Bearer 가 다른 호스트로 간다."""
    adapter, upstream = model(httpx.Response(307, headers={"location": "https://elsewhere/v1"}))

    with pytest.raises(httpx.HTTPStatusError):
        await adapter.run("", OPENING, [])

    assert len(upstream.seen) == 1


@pytest.mark.parametrize("status", [500, 502, 503, 529])
async def test_run_server_error_raises_retryable_llm_003(status):
    adapter, _ = model(failure(status))

    with pytest.raises(AaestroError) as exc_info:
        await adapter.run("", OPENING, [])

    error = exc_info.value
    assert (error.category, error.code, error.retryable) == (
        ErrorCategory.MODEL,
        ErrorCode.LLM_003,
        True,
    )
    # 5xx 는 네트워크 정책의 몫이 아니다 — anthropic 어댑터와 같다
    assert error.category not in NETWORK_RETRY.retryable_categories


async def test_run_connection_failure_raises_retryable_network_net_001():
    adapter, _ = model(httpx.ConnectError("refused"))

    with pytest.raises(AaestroError) as exc_info:
        await adapter.run("", OPENING, [])

    error = exc_info.value
    assert (error.category, error.code, error.retryable) == (
        ErrorCategory.NETWORK,
        ErrorCode.NET_001,
        True,
    )
    assert isinstance(error.__cause__, httpx.ConnectError)
    assert error.category in NETWORK_RETRY.retryable_categories


async def test_run_timeout_raises_retryable_timeout_net_002():
    adapter, _ = model(httpx.ReadTimeout("slow"))

    with pytest.raises(AaestroError) as exc_info:
        await adapter.run("", OPENING, [])

    error = exc_info.value
    assert (error.category, error.code, error.retryable) == (
        ErrorCategory.TIMEOUT,
        ErrorCode.NET_002,
        True,
    )
    assert error.category in NETWORK_RETRY.retryable_categories


async def test_run_a_failure_is_sent_once():
    """어댑터 안에는 재시도가 없다 — 재시도 계층은 agentd 하나다 (05 Retry Layering)."""
    adapter, upstream = model(failure(429))

    with pytest.raises(AaestroError):
        await adapter.run("", OPENING, [])

    assert len(upstream.seen) == 1


@pytest.mark.parametrize(
    "response",
    [
        failure(429, {"message": "rate limited for key sk-upstream-secret"}),
        failure(500, {"message": "crashed while using sk-upstream-secret"}),
        failure(400, {"code": "context_length_exceeded", "message": "sk-upstream-secret"}),
    ],
)
async def test_run_converted_errors_carry_neither_the_upstream_body_nor_the_credential(response):
    """인증 본문이 키를 되비추는 provider 가 있다 — details 는 TaskResult 와 로그로 나간다."""
    adapter, _ = model(response)

    with pytest.raises(AaestroError) as exc_info:
        await adapter.run("", OPENING, [])

    rendered = f"{exc_info.value} {exc_info.value.details} {exc_info.value.payload()}"
    assert "sk-upstream-secret" not in rendered and IDENTITY not in rendered


async def test_run_propagated_error_text_does_not_carry_the_credential():
    adapter, _ = model(failure(401, {"message": "Incorrect API key provided: sk-upstream-secret"}))

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await adapter.run("", OPENING, [])

    assert "sk-upstream-secret" not in str(exc_info.value) and IDENTITY not in str(exc_info.value)


def test_default_client_bounds_every_request_with_a_timeout():
    adapter = OpenAICompatModel(
        config=ModelConfig(provider="openai-compat", name=MODEL),
        agent="researcher",
        base_url=BASE_URL,
        credential=IDENTITY,
    )

    assert adapter.client is not None
    timeout = adapter.client.timeout
    assert (timeout.connect, timeout.read) == (5.0, 600.0)
    assert adapter.client.follow_redirects is False
