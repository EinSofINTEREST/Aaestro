"""Anthropic provider binding tests.

**실 API 를 호출하지 않는다** — 스크립트된 클라이언트로만 검증한다 (06 규칙).
CI 에 API key 가 없어도 통과해야 한다.
"""

from __future__ import annotations

import anthropic
import httpx
import pytest

from malkuth.agentd.executor import AssistantTurn, ToolCall, ToolOutcome, ToolTurn, UserTurn
from malkuth.agentd.providers.anthropic import DEFAULT_MAX_TOKENS, AnthropicModel
from malkuth.core.errors import (
    NETWORK_RETRY,
    RATE_LIMIT_RETRY,
    ErrorCategory,
    ErrorCode,
    MalkuthError,
)
from malkuth.core.manifest import ModelConfig
from malkuth.core.skill import SkillSpec

MODEL = "claude-sonnet-5"
OPENING = [UserTurn(parts=("prompt",))]


class Block:
    """provider 응답 블록 대역."""

    def __init__(self, **fields: object) -> None:
        self.__dict__.update(fields)


class Usage:
    def __init__(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class Message:
    def __init__(self, content: list[Block], usage: Usage | None = None) -> None:
        self.content = content
        self.usage = usage or Usage()


class FakeMessages:
    """``client.messages`` 대역 — 요청을 기록하고 스크립트된 응답을 낸다."""

    def __init__(self, result: object) -> None:
        self._result = result
        self.requests: list[dict] = []

    async def create(self, **request: object) -> object:
        self.requests.append(request)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeClient:
    def __init__(self, result: object) -> None:
        self.messages = FakeMessages(result)


def model(result: object, **overrides: object) -> AnthropicModel:
    config = ModelConfig(provider="anthropic", name=MODEL, **overrides)  # type: ignore[arg-type]
    return AnthropicModel(config=config, agent="researcher", client=FakeClient(result))


STATUS_BY_ERROR = {
    anthropic.RateLimitError: 429,
    anthropic.BadRequestError: 400,
    anthropic.InternalServerError: 500,
}


def api_error(kind: type[anthropic.APIStatusError], message: str = "boom"):
    """SDK 에러를 실제 타입과 상태코드로 만든다.

    지금 구현은 status 를 보지 않지만, 모든 에러를 429 로 만들어두면 나중에
    상태코드 분기가 생겼을 때 **테스트가 잘못된 방향으로 통과**한다.
    """
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(STATUS_BY_ERROR[kind], request=request)
    return kind(message, response=response, body=None)


# --- 요청 구성 ----------------------------------------------------------------


async def test_manifest_decides_the_model():
    """모델명을 코드에 하드코딩하지 않는다 (02 Manifest Rules 3)."""
    provider = model(Message([Block(type="text", text="done")]))

    await provider.run("", OPENING, [])

    assert provider.client.messages.requests[0]["model"] == MODEL


async def test_max_tokens_falls_back_to_the_default():
    """SDK 가 필수로 요구하므로 manifest 가 비워두면 기본값을 쓴다."""
    provider = model(Message([Block(type="text", text="done")]))

    await provider.run("", OPENING, [])

    assert provider.client.messages.requests[0]["max_tokens"] == DEFAULT_MAX_TOKENS


async def test_declared_limits_reach_the_request():
    provider = model(Message([Block(type="text", text="done")]), max_tokens=1024, effort="high")

    await provider.run("", OPENING, [])

    request = provider.client.messages.requests[0]
    assert request["max_tokens"] == 1024
    assert request["output_config"] == {"effort": "high"}


async def test_effort_is_omitted_when_undeclared():
    """선언하지 않은 값을 임의로 채우면 manifest 가 계약이 아니게 된다."""
    provider = model(Message([Block(type="text", text="done")]))

    await provider.run("", OPENING, [])

    assert "output_config" not in provider.client.messages.requests[0]


async def test_sampling_parameters_are_never_sent():
    """현재 API 는 temperature 를 받지 않는다 — 보내면 호출이 TypeError 로 죽는다.

    E2E 를 표준 실행기로 돌리자마자 첫 모델 호출에서 드러났다 (#157).
    유닛은 FakeModel 을, E2E 는 echo 대역을 쓰고 있어 한 번도 잡히지 않았다.
    """
    provider = model(Message([Block(type="text", text="done")]), effort="high")

    await provider.run("", OPENING, [])

    request = provider.client.messages.requests[0]
    for rejected in ("temperature", "top_p", "top_k"):
        assert rejected not in request


async def test_tool_schemas_keep_the_mcp_namespace():
    """이름이 곧 출처 판별이다 — 네임스페이스가 깎이면 에러 코드가 뒤바뀐다."""
    spec = SkillSpec(
        name="mcp__filesystem__read_file",
        description="파일을 읽는다",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    provider = model(Message([Block(type="text", text="done")]))

    await provider.run("", OPENING, [spec])

    tool = provider.client.messages.requests[0]["tools"][0]
    assert tool["name"] == "mcp__filesystem__read_file"
    assert tool["input_schema"]["properties"] == {"path": {"type": "string"}}


async def test_tools_are_omitted_when_none_are_bound():
    provider = model(Message([Block(type="text", text="done")]))

    await provider.run("", OPENING, [])

    assert "tools" not in provider.client.messages.requests[0]


# --- 응답 해석 ----------------------------------------------------------------


async def test_text_response_is_final():
    provider = model(Message([Block(type="text", text="answer")]))

    response = await provider.run("", OPENING, [])

    assert response.content == "answer"
    assert response.is_final


async def test_tool_use_becomes_a_tool_call():
    provider = model(
        Message(
            [
                Block(type="text", text="살펴보겠습니다"),
                Block(type="tool_use", id="tu-1", name="search", input={"query": "q"}),
            ]
        )
    )

    response = await provider.run("", OPENING, [])

    assert not response.is_final
    assert response.tool_calls[0].name == "search"
    assert response.tool_calls[0].arguments == {"query": "q"}
    assert response.content == "살펴보겠습니다"


async def test_usage_is_collected():
    """토큰 집계가 빠지면 비용 관측과 quota 감시가 함께 무너진다."""
    provider = model(Message([Block(type="text", text="x")], usage=Usage(120, 45)))

    response = await provider.run("", OPENING, [])

    assert response.usage.input_tokens == 120
    assert response.usage.output_tokens == 45


async def test_unreadable_response_is_llm_004():
    """조용히 빈 응답으로 처리하면 태스크가 성공한 것처럼 끝난다."""
    provider = model(Message([Block(type="tool_use")]))  # id/name 없음

    with pytest.raises(MalkuthError) as exc_info:
        await provider.run("", OPENING, [])

    assert exc_info.value.code == ErrorCode.LLM_004
    assert exc_info.value.category is ErrorCategory.MODEL
    assert not exc_info.value.retryable


# --- 에러 변환 ----------------------------------------------------------------


async def test_rate_limit_is_retryable_llm_001():
    provider = model(api_error(anthropic.RateLimitError))

    with pytest.raises(MalkuthError) as exc_info:
        await provider.run("", OPENING, [])

    assert exc_info.value.code == ErrorCode.LLM_001
    assert exc_info.value.retryable


async def test_rate_limit_carries_its_own_category():
    """MODEL 로 뭉개면 RATE_LIMIT_RETRY 가 겨냥한 유일한 상황에 닿지 못한다.

    05 Layer Rules 는 모델 호출 boundary 가 MODEL/RATE_LIMIT/TIMEOUT/NETWORK 를
    낸다고 규정한다.
    """
    provider = model(api_error(anthropic.RateLimitError))

    with pytest.raises(MalkuthError) as exc_info:
        await provider.run("", OPENING, [])

    assert exc_info.value.category is ErrorCategory.RATE_LIMIT
    # 정책이 실제로 이 에러를 집는지 — 카테고리 단언만으로는 놓치는 계약
    assert RATE_LIMIT_RETRY.should_retry(exc_info.value)


async def test_other_model_failures_keep_the_model_category():
    """rate limit 분리가 나머지를 끌고 가면 안 된다."""
    provider = model(api_error(anthropic.InternalServerError))

    with pytest.raises(MalkuthError) as exc_info:
        await provider.run("", OPENING, [])

    assert exc_info.value.category is ErrorCategory.MODEL
    assert not RATE_LIMIT_RETRY.should_retry(exc_info.value)


async def test_context_overflow_is_llm_002_and_not_retryable():
    """같은 프롬프트로 다시 보내도 길이는 같다 — 재시도는 낭비다."""
    provider = model(api_error(anthropic.BadRequestError, "prompt is too long: 300000 tokens"))

    with pytest.raises(MalkuthError) as exc_info:
        await provider.run("", OPENING, [])

    assert exc_info.value.code == ErrorCode.LLM_002
    assert not exc_info.value.retryable


async def test_other_bad_requests_are_not_disguised_as_context_overflow():
    """설정 오류를 LLM_002 로 덮으면 운영자가 프롬프트만 줄이다 시간을 버린다."""
    provider = model(api_error(anthropic.BadRequestError, "unknown model: typo"))

    with pytest.raises(anthropic.BadRequestError):
        await provider.run("", OPENING, [])


async def test_server_error_is_retryable_llm_003():
    provider = model(api_error(anthropic.InternalServerError))

    with pytest.raises(MalkuthError) as exc_info:
        await provider.run("", OPENING, [])

    assert exc_info.value.code == ErrorCode.LLM_003
    assert exc_info.value.retryable


def sdk_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


async def test_server_error_is_not_claimed_by_the_network_policy():
    """5xx 는 provider 장애다 — 연결 실패의 짧은 backoff 로 두드리지 않는다."""
    provider = model(api_error(anthropic.InternalServerError))

    with pytest.raises(MalkuthError) as exc_info:
        await provider.run("", OPENING, [])

    assert exc_info.value.category is ErrorCategory.MODEL
    assert not NETWORK_RETRY.should_retry(exc_info.value)


async def test_connection_error_is_retryable_network_net_001():
    """MODEL 로 뭉개면 NETWORK_RETRY 와 선언 retry.network 가 한 번도 동작하지 않는다."""
    provider = model(anthropic.APIConnectionError(request=sdk_request()))

    with pytest.raises(MalkuthError) as exc_info:
        await provider.run("", OPENING, [])

    assert exc_info.value.category is ErrorCategory.NETWORK
    assert exc_info.value.code == ErrorCode.NET_001
    assert exc_info.value.retryable
    assert NETWORK_RETRY.should_retry(exc_info.value)


async def test_timeout_is_retryable_timeout_net_002():
    """APITimeoutError 는 APIConnectionError 의 하위 클래스다 — 순서가 틀리면 NET_001 로 묻힌다."""
    provider = model(anthropic.APITimeoutError(request=sdk_request()))

    with pytest.raises(MalkuthError) as exc_info:
        await provider.run("", OPENING, [])

    assert exc_info.value.category is ErrorCategory.TIMEOUT
    assert exc_info.value.code == ErrorCode.NET_002
    assert exc_info.value.retryable
    assert NETWORK_RETRY.should_retry(exc_info.value)


# --- 재시도 계층 --------------------------------------------------------------


def test_sdk_retries_are_disabled():
    """provider 도 재시도하면 backoff 가 곱해져 rate limit 을 더 오래 문다."""
    provider = AnthropicModel(
        config=ModelConfig(provider="anthropic", name=MODEL), agent="researcher"
    )

    assert provider.client.max_retries == 0


async def test_malformed_tool_arguments_are_llm_004():
    """input 이 매핑이 아니면 ValueError 가 executor 로 그대로 샌다."""
    provider = model(Message([Block(type="tool_use", id="tu-1", name="search", input=["query"])]))

    with pytest.raises(MalkuthError) as exc_info:
        await provider.run("", OPENING, [])

    assert exc_info.value.code == ErrorCode.LLM_004


# --- 대화 구조 (#308) ------------------------------------------------------------------


async def test_system_goes_in_its_own_field_not_the_user_message():
    """운영자 지시와 태스크 입력이 한 문자열이면 도구 결과가 지시와 같은 자리에 놓인다."""
    provider = model(Message([Block(type="text", text="done")]))

    await provider.run("You are a researcher.", OPENING, [])

    request = provider.client.messages.requests[0]
    assert request["system"] == "You are a researcher."
    assert request["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "prompt"}]}
    ]


async def test_no_system_field_when_the_promptset_has_none():
    provider = model(Message([Block(type="text", text="done")]))

    await provider.run("", OPENING, [])

    assert "system" not in provider.client.messages.requests[0]


async def test_tool_results_answer_the_tool_use_they_belong_to():
    """각 결과가 자기 호출의 id 를 가리켜야 모델이 어느 호출의 답인지 안다."""
    provider = model(Message([Block(type="text", text="done")]))
    raw = [Block(type="thinking", thinking="", signature="sig"), Block(type="tool_use")]
    conversation = [
        UserTurn(parts=("recalled", "prompt")),
        AssistantTurn(
            content="",
            tool_calls=(ToolCall(id="t1", name="search"), ToolCall(id="t2", name="fetch")),
            raw=raw,
        ),
        ToolTurn(
            outcomes=(
                ToolOutcome(call_id="t1", content="found"),
                ToolOutcome(call_id="t2", content="boom", is_error=True),
            )
        ),
    ]

    await provider.run("sys", conversation, [])

    user, assistant, results = provider.client.messages.requests[0]["messages"]
    assert [p["text"] for p in user["content"]] == ["recalled", "prompt"]
    # provider 가 준 블록(thinking 포함)을 바꾸지 않고 되돌린다
    assert assistant == {"role": "assistant", "content": raw}
    assert results == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "found"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "boom", "is_error": True},
        ],
    }


async def test_an_assistant_turn_without_provider_blocks_is_rebuilt():
    provider = model(Message([Block(type="text", text="done")]))
    turn = AssistantTurn(
        content="let me look", tool_calls=(ToolCall(id="t1", name="search", arguments={"q": 1}),)
    )

    await provider.run("", [*OPENING, turn], [])

    assistant = provider.client.messages.requests[0]["messages"][1]
    assert assistant["content"] == [
        {"type": "text", "text": "let me look"},
        {"type": "tool_use", "id": "t1", "name": "search", "input": {"q": 1}},
    ]


async def test_the_response_keeps_the_provider_blocks_to_send_back():
    blocks = [
        Block(type="thinking", thinking="", signature="sig"),
        Block(type="tool_use", id="t1", name="search", input={"q": "x"}),
    ]
    provider = model(Message(blocks))

    response = await provider.run("", OPENING, [])

    assert response.raw == blocks
    assert response.tool_calls == (ToolCall(id="t1", name="search", arguments={"q": "x"}),)
