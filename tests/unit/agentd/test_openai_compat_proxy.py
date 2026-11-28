"""An ``openai-compat`` agent talking to its provider through the egress proxy (#361).

에이전트(실행 루프 + 어댑터) → 이그레스 프록시(종단 · Bearer 주입) → provider 대역을 한 프로세스
안에서 실제 HTTP 메시지로 잇는다. 실 LLM 은 부르지 않는다 (06) — 대역은 요청 내용으로 답을 정한다.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx
import httpx2
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from aaestro.agentd.bootstrap import model_factory
from aaestro.agentd.executor import (
    Executor,
    ExecutorConfig,
    ExecutorServices,
    UserTurn,
)
from aaestro.agentd.models import ModelChains
from aaestro.agentd.providers.anthropic import AnthropicModel
from aaestro.agentd.providers.egress_refusal import EGRESS_ERROR_CODE_HEADER
from aaestro.agentd.providers.openai_compat import API_KEY_ENV, BASE_URL_ENV
from aaestro.core.agent import TaskStatus
from aaestro.core.errors import AaestroError
from aaestro.core.manifest import (
    CircuitDeclaration,
    FallbackModel,
    ModelConfig,
    ModelRetry,
    RetryDeclaration,
)
from aaestro.core.skill import SkillSpec
from aaestro.egress.providers import (
    ANTHROPIC_ENDPOINTS,
    ANTHROPIC_REQUEST_HEADERS,
    OPENAI_COMPAT_ENDPOINTS,
    OPENAI_COMPAT_KEY_HEADER,
    OPENAI_COMPAT_KEY_SCHEME,
    Upstream,
    create_provider_app,
)
from tests.fixtures.builders import async_render, make_task
from tests.fixtures.fake_model import FakeTools
from tests.unit.agentd.providers.test_anthropic import Block, api_error
from tests.unit.agentd.providers.test_anthropic import Message as AnthropicMessage
from tests.unit.egress.test_connect import Registry

AGENT = "researcher"
IDENTITY = "cred-researcher"
PROVIDER_KEY = "sk-compat-provider-key"
LOGICAL_HOST = "llm.internal.example"
ANTHROPIC_HOST = "api.anthropic.com"
TOOL = "mcp__kb__lookup"
CLAUDE = "claude-primary"
RATE_LIMIT_ATTEMPTS = 2
LOOKUP = SkillSpec(
    name=TOOL,
    description="look a term up",
    parameters={"type": "object", "properties": {"term": {"type": "string"}}},
)


class CompatProvider:
    """Chat Completions 호환 대역 — 도구 결과가 없으면 도구를 부르고, 있으면 그 결과로 답한다."""

    def __init__(self, *, arguments: Any = None) -> None:
        self.arguments = json.dumps({"term": "aaestro"}) if arguments is None else arguments
        """도구 호출에 싣는 ``arguments`` — 기본은 JSON 문자열, 느슨한 서버 흉내는 여기서."""
        self.strict = True
        """되돌아온 assistant 의 ``arguments`` 가 JSON 객체 문자열이 아니면 400 — 엄격한 서버."""
        self.requests: list[dict[str, Any]] = []
        """답한 요청 — rate limit 으로 돌려보낸 것은 세지 않는다."""
        self.attempts = 0
        self.rate_limit_from: int | None = None
        """이 번째 요청부터 429 로 답한다 (0 이면 처음부터)."""
        self.authorizations: list[list[str]] = []
        self.spoof: int | None = None
        """이 상태 코드로 프록시의 거절을 흉내 낸다 — 프록시 표시 헤더와 같은 모양의 본문 (#398)."""
        self.app = FastAPI()
        self.app.post("/v1/chat/completions")(self._complete)
        self.app.post("/v1/messages")(self._messages)

    def _spoofed(self) -> JSONResponse:
        # 프록시가 그 상태에 싣는 코드 그대로 — 403 은 ACC_001, 503 은 ACC_002
        status = self.spoof or 500
        code = "ACC_002" if status == 503 else "ACC_001"
        return JSONResponse(
            {"type": "error", "error": {"type": "permission_error", "code": code}},
            status_code=status,
            headers={EGRESS_ERROR_CODE_HEADER: code},
        )

    async def _messages(self) -> Any:
        """Anthropic 경로 — 흉내 낸 거절만 답한다 (정상 답은 SDK 대역이 낸다)."""
        self.attempts += 1
        return self._spoofed()

    async def _complete(self, request: Request) -> Any:
        self.attempts += 1
        if self.spoof is not None:
            return self._spoofed()
        if self.rate_limit_from is not None and self.attempts > self.rate_limit_from:
            return JSONResponse({"error": {"message": "rate limited"}}, status_code=429)
        self.authorizations.append(request.headers.getlist("authorization"))
        payload = await request.json()
        if self.strict and not _arguments_are_object_strings(payload):
            return JSONResponse(
                {"error": {"message": "tool_calls.function.arguments must be a JSON object"}},
                status_code=400,
            )
        self.requests.append(payload)
        results = [m for m in payload["messages"] if m["role"] == "tool"]
        if results:
            message: dict[str, Any] = {
                "role": "assistant",
                "content": f"the answer is {results[-1]['content']}",
            }
            finish = "stop"
        else:
            advertised = [tool["function"]["name"] for tool in payload.get("tools", [])]
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": advertised[0], "arguments": self.arguments},
                    }
                ],
            }
            finish = "tool_calls"
        return {
            "id": "chatcmpl-fake",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


def _arguments_are_object_strings(payload: dict[str, Any]) -> bool:
    for message in payload["messages"]:
        for call in message.get("tool_calls") or []:
            arguments = call["function"]["arguments"]
            if not isinstance(arguments, str):
                return False
            try:
                parsed = json.loads(arguments)
            except ValueError:
                return False
            if not isinstance(parsed, dict):
                return False
    return True


class Stack:
    """에이전트 실행기 → 프록시 → provider 대역."""

    def __init__(
        self,
        config: ModelConfig,
        *,
        bound: dict[str, Any] | None = None,
        provider: CompatProvider | None = None,
    ) -> None:
        self.provider = provider or CompatProvider()
        self.registry = Registry()
        self.registry.allowed.update({LOGICAL_HOST, ANTHROPIC_HOST})
        self.bound = bound or {}
        proxy = create_provider_app(
            self.registry,
            {
                "openai-compat": Upstream(
                    base_url=f"https://{LOGICAL_HOST}",
                    target=LOGICAL_HOST,
                    api_key=PROVIDER_KEY,
                    key_header=OPENAI_COMPAT_KEY_HEADER,
                    key_scheme=OPENAI_COMPAT_KEY_SCHEME,
                    endpoints=OPENAI_COMPAT_ENDPOINTS,
                ),
                "anthropic": Upstream(
                    base_url=f"https://{ANTHROPIC_HOST}",
                    target=ANTHROPIC_HOST,
                    api_key=PROVIDER_KEY,
                    request_headers=ANTHROPIC_REQUEST_HEADERS,
                    endpoints=ANTHROPIC_ENDPOINTS,
                ),
            },
            http=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.provider.app)),
        )
        self._proxy = proxy
        self._to_proxy = httpx.ASGITransport(app=proxy)
        self._bind = model_factory(
            AGENT, {BASE_URL_ENV: "http://egress:8081/openai-compat", API_KEY_ENV: IDENTITY}
        )
        self.tools = FakeTools().script(TOOL, "42")
        self.waits: list[float] = []

        async def sleep(delay: float) -> None:
            self.waits.append(delay)

        chains = ModelChains(agent=AGENT, factory=self._factory)
        self.chain = chains.for_config(config)
        self.executor = Executor(
            agent=AGENT,
            model=self.chain,
            tools=self.tools,
            render=async_render(lambda _task: "what is aaestro?"),
            tool_schemas=[LOOKUP],
            config=ExecutorConfig(retry_sleep=sleep),
            services=ExecutorServices(chains=chains.for_config),
            system="operator rules",
        )

    def _factory(self, config: ModelConfig) -> Any:
        if config.name in self.bound:
            # SDK 클라이언트를 대역으로 갈아 끼운 어댑터 — 테이블은 실 SDK 를 세운다
            return self.bound[config.name]
        if config.provider == "anthropic":
            # 실 SDK 가 실 HTTP 로 프록시를 부른다 — 프록시의 거절이 SDK 예외로 올라오는 경로 (#398)
            return AnthropicModel(
                config=config,
                agent=AGENT,
                client=anthropic.AsyncAnthropic(
                    api_key=IDENTITY,
                    base_url="http://egress:8081/anthropic",
                    # SDK 는 자기가 묶은 httpx2 클라이언트만 받는다
                    http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self._proxy)),
                    max_retries=0,
                ),
            )
        # 실제 조립과 같은 테이블로 어댑터를 고르고, 전송만 프로세스 안의 프록시로 돌린다
        adapter = self._bind(config)
        adapter.client = httpx.AsyncClient(transport=self._to_proxy)
        return adapter


COMPAT = ModelConfig(provider="openai-compat", name="gpt-compatible")


async def test_execute_openai_compat_agent_completes_a_tool_round_trip_through_the_proxy():
    stack = Stack(COMPAT)

    result = await stack.executor.execute(make_task())

    assert result.status == TaskStatus.COMPLETED, result.error
    assert "the answer is 42" in json.dumps(result.output)
    assert stack.tools.calls == [TOOL]
    first, second = stack.provider.requests
    assert first["messages"][0] == {"role": "system", "content": "operator rules"}
    assert [tool["function"]["name"] for tool in first["tools"]] == [TOOL]
    # 도구 호출은 평탄화되지 않는다 — assistant 의 tool_calls 와 그것을 가리키는 role: tool
    assistant, answer = second["messages"][-2:]
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert answer == {"role": "tool", "tool_call_id": "call_1", "content": "42"}
    assert (result.usage.input_tokens, result.usage.output_tokens) == (20, 10)


@pytest.mark.parametrize("loose", ["", {}], ids=["empty-string", "object"])
async def test_execute_loosely_shaped_tool_arguments_do_not_break_the_next_turn(loose):
    """느슨한 인자를 그대로 되돌려 보내면, 도구가 실행된 **다음에** 태스크가 400 으로 죽는다."""
    stack = Stack(COMPAT, provider=CompatProvider(arguments=loose))

    result = await stack.executor.execute(make_task())

    assert result.status == TaskStatus.COMPLETED, result.error
    assert stack.tools.calls == [TOOL]
    _, second = stack.provider.requests
    [call] = second["messages"][-2]["tool_calls"]
    assert call["function"]["arguments"] == "{}"
    assert second["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": "42"}


async def test_execute_provider_sees_only_the_proxy_key_never_the_agent_identity():
    stack = Stack(COMPAT)

    await stack.executor.execute(make_task())

    assert stack.provider.authorizations == [[f"Bearer {PROVIDER_KEY}"]] * 2
    # 판정은 에이전트 신원과 provider 의 논리 호스트로 — 호출마다
    assert stack.registry.asked == [(IDENTITY, LOGICAL_HOST)] * 2


async def test_execute_revoked_model_api_fails_the_task_without_reaching_the_provider():
    stack = Stack(COMPAT)
    stack.registry.allowed.discard(LOGICAL_HOST)

    result = await stack.executor.execute(make_task())

    assert result.status == TaskStatus.FAILED
    assert stack.provider.requests == []
    assert result.error is not None
    rendered = result.error.model_dump_json()
    assert PROVIDER_KEY not in rendered and IDENTITY not in rendered


# --- 교차 provider 전환 (#359 · #361) — 실행기 + 두 어댑터, 전송만 대역 ------------------


class ScriptedAnthropic:
    """Anthropic SDK 클라이언트 대역 — 결과를 순서대로 내고, 끝나면 마지막을 되풀이한다."""

    def __init__(self, *results: object) -> None:
        self._results = results
        self.requests: list[dict[str, Any]] = []
        self.messages = self

    async def create(self, **request: Any) -> object:
        self.requests.append(request)
        result = self._results[min(len(self.requests), len(self._results)) - 1]
        if isinstance(result, Exception):
            raise result
        return result


def anthropic_model(client: ScriptedAnthropic) -> AnthropicModel:
    return AnthropicModel(
        config=ModelConfig(provider="anthropic", name=CLAUDE), agent=AGENT, client=client
    )


def chain_config(primary: tuple[str, str], fallback: tuple[str, str]) -> ModelConfig:
    return ModelConfig(
        provider=primary[0],
        name=primary[1],
        fallbacks=(FallbackModel(provider=fallback[0], name=fallback[1], on=("rate_limit",)),),
        retry=ModelRetry(rate_limit=RetryDeclaration(start_s=1, attempts=RATE_LIMIT_ATTEMPTS)),
    )


ANTHROPIC_FIRST = chain_config(("anthropic", CLAUDE), ("openai-compat", "gpt-compatible"))
COMPAT_FIRST = chain_config(("openai-compat", "gpt-compatible"), ("anthropic", CLAUDE))


async def test_execute_rate_limited_anthropic_hands_the_same_turn_to_openai_compat():
    """primary 가 재시도를 다 쓰면 같은 턴이 fallback 으로 간다."""
    claude = ScriptedAnthropic(api_error(anthropic.RateLimitError))
    stack = Stack(ANTHROPIC_FIRST, bound={CLAUDE: anthropic_model(claude)})

    result = await stack.executor.execute(make_task())

    assert result.status == TaskStatus.COMPLETED, result.error
    assert "the answer is 42" in json.dumps(result.output)
    # 전환은 턴 단위다 — 두 턴(도구 호출, 최종 답) 모두 primary 를 선언한 횟수만큼 두드린다
    turns = 2
    assert len(claude.requests) == RATE_LIMIT_ATTEMPTS * turns
    assert len(stack.waits) == (RATE_LIMIT_ATTEMPTS - 1) * turns
    assert len(stack.provider.requests) == 2
    assert stack.provider.authorizations == [[f"Bearer {PROVIDER_KEY}"]] * 2


async def test_execute_anthropic_tool_turn_is_continued_by_openai_compat_mid_conversation():
    """도구를 부른 것은 anthropic, 그 결과를 받아 끝내는 것은 openai-compat."""
    claude = ScriptedAnthropic(
        AnthropicMessage(
            [
                Block(type="thinking", thinking="", signature="sig"),
                Block(type="tool_use", id="toolu_1", name=TOOL, input={"term": "aaestro"}),
            ]
        ),
        api_error(anthropic.RateLimitError),
    )
    stack = Stack(ANTHROPIC_FIRST, bound={CLAUDE: anthropic_model(claude)})

    result = await stack.executor.execute(make_task())

    assert result.status == TaskStatus.COMPLETED, result.error
    [request] = stack.provider.requests
    assistant, answer = request["messages"][-2:]
    # Anthropic 블록 리스트를 그대로 보내지 않고 Chat Completions 모양으로 다시 짓는다
    assert assistant == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "toolu_1",
                "type": "function",
                "function": {"name": TOOL, "arguments": '{"term": "aaestro"}'},
            }
        ],
    }
    assert answer == {"role": "tool", "tool_call_id": "toolu_1", "content": "42"}


async def test_execute_rate_limited_openai_compat_hands_the_same_turn_to_anthropic():
    claude = ScriptedAnthropic(AnthropicMessage([Block(type="text", text="claude answers")]))
    stack = Stack(COMPAT_FIRST, bound={CLAUDE: anthropic_model(claude)})
    stack.provider.rate_limit_from = 0

    result = await stack.executor.execute(make_task())

    assert result.status == TaskStatus.COMPLETED, result.error
    assert "claude answers" in json.dumps(result.output)
    assert stack.provider.attempts == RATE_LIMIT_ATTEMPTS
    [request] = claude.requests
    assert request["system"][0]["text"] == "operator rules"
    assert [message["role"] for message in request["messages"]] == ["user"]


async def test_execute_openai_compat_tool_turn_is_continued_by_anthropic_mid_conversation():
    """반대 방향 — openai-compat 의 원본 메시지가 Anthropic content 로 새지 않는다."""
    claude = ScriptedAnthropic(AnthropicMessage([Block(type="text", text="claude answers")]))
    stack = Stack(COMPAT_FIRST, bound={CLAUDE: anthropic_model(claude)})
    stack.provider.rate_limit_from = 1

    result = await stack.executor.execute(make_task())

    assert result.status == TaskStatus.COMPLETED, result.error
    assert len(stack.provider.requests) == 1
    [request] = claude.requests
    _, assistant, results = request["messages"]
    assert assistant == {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "call_1", "name": TOOL, "input": {"term": "aaestro"}}
        ],
    }
    assert results == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": "42",
                # 마지막 user 블록의 증분 캐시 breakpoint (#554)
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }


# --- 프록시의 거절은 provider 실패가 아니다 (#398) — 두 어댑터 모두 실 HTTP 로 프록시를 지난다 ---

CLAUDE_VIA_PROXY = ModelConfig(provider="anthropic", name=CLAUDE)
NETWORK_ATTEMPTS = 2
PRIMARY_HOST = {"openai-compat": LOGICAL_HOST, "anthropic": ANTHROPIC_HOST}


def guarded(primary: ModelConfig, fallback: ModelConfig) -> ModelConfig:
    """server_error 로 넘어가는 fallback, 한 번에 열리는 circuit, 짧은 network 재시도."""
    return primary.model_copy(
        update={
            "fallbacks": (
                FallbackModel(provider=fallback.provider, name=fallback.name, on=("server_error",)),
            ),
            "retry": ModelRetry(network=RetryDeclaration(start_s=1, attempts=NETWORK_ATTEMPTS)),
            "circuit": CircuitDeclaration(max_failures=1, reset_s=60),
        }
    )


BOTH = pytest.mark.parametrize(
    ("primary", "fallback"),
    [(COMPAT, CLAUDE_VIA_PROXY), (CLAUDE_VIA_PROXY, COMPAT)],
    ids=["openai-compat", "anthropic"],
)


@BOTH
async def test_execute_revoked_model_api_mid_task_is_acc_001(primary, fallback):
    """권한 회수는 설정 오류 (INTERNAL) 도 provider 장애도 아니다 — 비재시도 ACC_001."""
    stack = Stack(guarded(primary, fallback))
    stack.registry.allowed.clear()

    result = await stack.executor.execute(make_task())

    assert result.status == TaskStatus.FAILED
    assert result.error is not None
    assert (result.error.category, result.error.code) == ("forbidden", "ACC_001")
    assert result.error.retryable is False
    # 재시도 · fallback 없이 한 번 — 다른 모델도 같은 프록시가 막는다
    assert stack.registry.asked == [(IDENTITY, PRIMARY_HOST[primary.provider])]
    assert stack.provider.attempts == 0
    assert stack.chain.links[0].breaker is not None
    assert stack.chain.links[0].breaker.can_attempt()


@BOTH
async def test_execute_registry_outage_is_retryable_acc_002_without_fallback_or_circuit(
    primary, fallback
):
    stack = Stack(guarded(primary, fallback))
    stack.registry.down = True

    result = await stack.executor.execute(make_task())

    assert result.error is not None
    assert (result.error.category, result.error.code) == ("network", "ACC_002")
    assert result.error.retryable is True
    # agentd 의 network 재시도는 돌지만, fallback 으로 넘어가지 않는다
    assert stack.registry.asked == [(IDENTITY, PRIMARY_HOST[primary.provider])] * NETWORK_ATTEMPTS
    assert len(stack.waits) == NETWORK_ATTEMPTS - 1
    # 한 번에 열리는 circuit 이 닫힌 채다 — 레지스트리가 돌아오면 바로 같은 모델을 쓴다
    breaker = stack.chain.links[0].breaker
    assert breaker is not None and breaker.can_attempt()


@BOTH
async def test_execute_upstream_mimicking_a_proxy_refusal_is_a_provider_answer(primary, fallback):
    """upstream 이 프록시의 헤더 · 본문을 흉내 내도 ACC_001 이 아니다 — 헤더는 프록시만 낸다."""
    stack = Stack(primary)
    stack.provider.spoof = 403

    result = await stack.executor.execute(make_task())

    assert stack.provider.attempts == 1, "upstream 까지 닿았다"
    assert result.error is not None
    assert result.error.code != "ACC_001"


@BOTH
async def test_execute_upstream_503_mimicking_acc_002_still_falls_back(primary, fallback):
    stack = Stack(guarded(primary, fallback))
    stack.provider.spoof = 503

    result = await stack.executor.execute(make_task())

    assert result.error is not None
    assert result.error.code == "LLM_003", "provider 장애로 읽혀 fallback 까지 갔다"
    hosts = {target for _, target in stack.registry.asked}
    assert hosts == {LOGICAL_HOST, ANTHROPIC_HOST}


@BOTH
async def test_run_proxy_refusal_keeps_the_http_failure_as_its_cause(primary, fallback):
    """거절로 바꾼 뒤에도 원 응답 (상태 · 요청) 이 원인 체인에 남는다 (05 Error Context)."""
    stack = Stack(primary)
    stack.registry.allowed.clear()
    adapter = stack._factory(primary)

    with pytest.raises(AaestroError) as caught:
        await adapter.run("operator rules", [UserTurn(parts=("hi",))], [])

    assert caught.value.code == "ACC_001"
    cause = caught.value.__cause__
    expected = (
        httpx.HTTPStatusError if primary.provider == "openai-compat" else anthropic.APIStatusError
    )
    assert isinstance(cause, expected)
    assert cause.response.status_code == 403
