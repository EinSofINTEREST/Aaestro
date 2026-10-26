"""OpenAI Chat Completions compatible model provider.

``Model`` Protocol 뒤에 Chat Completions 호환 API 를 바인딩한다 — OpenAI 뿐 아니라 base URL 로
가리키는 호환 서버(vLLM, Ollama 등) 전부가 대상이다.

**SDK 가 아니라 HTTP 로 부른다**: 프록시가 종단하는 base URL 하나면 충분하고, provider SDK 의
자체 재시도·전송이 끼지 않는다. 이 모듈에는 재시도 루프가 없다 — 재시도 계층은 agentd 하나다
(05 Retry Layering).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from aaestro.agentd.executor import (
    AssistantTurn,
    Message,
    ModelResponse,
    ToolCall,
    ToolTurn,
    UserTurn,
)
from aaestro.core.agent import ModelUsage
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.markers import TOOL_ERROR_MARK

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from aaestro.core.manifest import ModelConfig

log = structlog.get_logger(__name__)

PROVIDER = "openai-compat"

BASE_URL_ENV = "OPENAI_COMPAT_BASE_URL"
"""호환 API 의 base URL — ``/v1`` **앞까지**. 프록시를 쓰면 runtime 이 프록시 종단 창구를 넣는다."""
API_KEY_ENV = "OPENAI_COMPAT_API_KEY"  # noqa: S105 — 키 이름이지 값이 아니다
"""Bearer 자리에 싣는 값 — 프록시를 쓰면 키가 아니라 에이전트 신원이다 (02 Secrets Injection)."""

CHAT_COMPLETIONS_PATH = "v1/chat/completions"
"""이그레스 프록시가 이 provider 에 여는 유일한 경로와 같아야 한다 (egress/providers.py)."""

# anthropic SDK 의 기본 timeout 과 같은 값 — 형제 어댑터와 같은 요청이 다른 시점에 끊기지 않게
REQUEST_TIMEOUT_S = 600.0
CONNECT_TIMEOUT_S = 5.0

HTTP_BAD_REQUEST = 400
HTTP_RATE_LIMITED = 429
HTTP_SERVER_ERROR = 500
HTTP_MULTIPLE_CHOICES = 300

FINISH_LENGTH = "length"

_OVERFLOW_CODES = frozenset({"context_length_exceeded", "string_above_max_length"})
_OVERFLOW_PHRASES = (
    "context length",
    "context window",
    "maximum context",
    "context_length",
    "too many tokens",
    "prompt is too long",
)

_EMPTY_PARAMETERS: dict[str, Any] = {"type": "object", "properties": {}}


@dataclass(frozen=True)
class _ProviderMessage:
    """이 어댑터가 받은 assistant 메시지 원본.

    ``raw`` 를 맨 dict 로 두면 다른 provider 의 원본(Anthropic 블록 리스트)과 구분되지 않는다 —
    fallback 으로 턴이 넘어왔을 때 남의 원본을 그대로 되돌려 보내지 않으려고 타입으로 표시한다.
    """

    message: Mapping[str, Any]


def _model_error(
    code: ErrorCode,
    message: str,
    *,
    agent: str,
    retryable: bool,
    cause: str,
    category: ErrorCategory = ErrorCategory.MODEL,
) -> AaestroError:
    """provider 실패를 구조화 에러로 변환한다 — 카테고리 기준은 anthropic 어댑터와 같다.

    ``details`` 에는 원인 이름만 싣는다. upstream 의 에러 본문은 싣지 않는다 — 인증 실패 본문이
    키 일부를 되비추는 provider 가 있고, details 는 TaskResult 와 로그로 나간다.
    """
    return AaestroError(
        category=category,
        code=code,
        message=message,
        agent=agent,
        retryable=retryable,
        details={"cause": cause},
    )


def _is_context_overflow(response: httpx.Response) -> bool:
    """400 중 context 초과만 골라낸다 — 나머지는 설정 오류다.

    판별 기준은 여기 한 곳에만 둔다. OpenAI 는 ``error.code`` 로 알려 주지만 호환 서버는 코드 없이
    문구만 주는 경우가 많아(vLLM 등) 문구도 본다. **한계**: 문구는 서버마다 달라, 목록에 없는
    문구로 답하는 서버의 context 초과는 설정 오류로 전파된다 (``context_overflow`` fallback 이
    걸리지 않는다). 입력을 조용히 잘라 200 으로 답하는 서버(Ollama 기본)는 여기서 알 수 없다.
    """
    try:
        body = response.json()
    except ValueError:
        return False
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return False
    if error.get("code") in _OVERFLOW_CODES:
        return True
    text = str(error.get("message", "")).lower()
    return any(phrase in text for phrase in _OVERFLOW_PHRASES)


@dataclass
class OpenAICompatModel:
    """The ``Model`` implementation backed by a Chat Completions compatible API.

    manifest 의 ``spec.model`` 이 모델을 결정하고, base URL 은 설정에서 온다 — 코드에 모델명이나
    주소를 하드코딩하지 않는다 (02 Manifest Rules 3).

    Attributes:
        config: The resolved model declaration.
        agent: Agent name for error context.
        base_url: API base URL up to, not including, ``/v1``.
        credential: Bearer value — the agent identity behind the egress proxy.
        client: Injected HTTP client; one is created when omitted.
    """

    config: ModelConfig
    agent: str
    base_url: str
    credential: str
    client: httpx.AsyncClient | None = None

    def __post_init__(self) -> None:
        if self.config.effort is not None:
            # fallback 은 primary 의 effort 를 물려받는다 — reasoning 을 모르는 호환 모델은
            # reasoning_effort 를 400 으로 거절하므로 보내지 않는다. 조용히 사라지지 않게 남긴다
            log.warning(
                "effort is not forwarded to this provider",
                agent=self.agent,
                provider=PROVIDER,
                model=self.config.name,
                effort=self.config.effort,
            )
        if self.client is None:
            # 프로세스 수명 동안 쓴다 — 형제(AsyncAnthropic)처럼 체인이 어댑터를 한 번 만들어 쥔다
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
            )

    async def run(
        self, system: str, messages: Sequence[Message], tools: Sequence[Any]
    ) -> ModelResponse:
        """Run one model turn.

        한 턴을 실행합니다. 재시도·라우팅 판단이 걸린 실패는 그 판단이 보는 카테고리로
        변환합니다 — 기준은 anthropic 어댑터와 같습니다.

        대화는 provider 의 구조 그대로 보냅니다: 운영자 지시는 ``system`` 메시지, 도구 호출은
        assistant 의 ``tool_calls``, 결과는 그 ``id`` 를 가리키는 ``role: tool`` 메시지 (#308).

        **설정 오류는 그대로 전파합니다**: 인증 실패(401)나 context 초과가 아닌 400 을
        모델 실패로 덮으면 재시도·fallback 이 고칠 수 없는 것을 두드립니다.

        Args:
            system: Operator instructions (the promptset's ``system`` template).
            messages: The conversation so far — 태스크 입력, 모델 응답, 도구 결과.
            tools: Tool specs the model may call.

        Returns:
            The model's response — tool 호출이 없으면 최종 응답입니다.

        Raises:
            AaestroError: RATE_LIMIT/``LLM_001`` rate limited, MODEL/``LLM_002``
                context exceeded, MODEL/``LLM_003`` provider server error (5xx),
                TIMEOUT/``NET_002`` request timed out, NETWORK/``NET_001``
                connection failed, MODEL/``LLM_004`` if the response cannot be
                interpreted.
            httpx.HTTPStatusError: Configuration errors (bad credentials, unknown
                model, invalid arguments) propagate unchanged — 프레임워크가 고칠 수
                있는 실패가 아닙니다.
        """
        response = await self._send(self._request(system, messages, tools))
        self._raise_for_failure(response)
        return self._interpret(response)

    def _request(
        self, system: str, messages: Sequence[Message], tools: Sequence[Any]
    ) -> dict[str, Any]:
        conversation: list[dict[str, Any]] = []
        if system:
            conversation.append({"role": "system", "content": system})
        for message in messages:
            conversation.extend(to_messages(message))
        request: dict[str, Any] = {"model": self.config.name, "messages": conversation}
        # anthropic 과 달리 필수가 아니다 — 선언이 없으면 보내지 않아 서버 기본값을 쓴다
        if self.config.max_tokens is not None:
            request["max_tokens"] = self.config.max_tokens
        if tools:
            request["tools"] = [_to_tool_schema(tool) for tool in tools]
        return request

    async def _send(self, request: dict[str, Any]) -> httpx.Response:
        assert self.client is not None  # noqa: S101 — __post_init__ 이 채운다
        try:
            return await self.client.post(
                f"{self.base_url.rstrip('/')}/{CHAT_COMPLETIONS_PATH}",
                json=request,
                headers={"authorization": f"Bearer {self.credential}"},
            )
        # TimeoutException 은 TransportError 의 하위 클래스라 먼저 잡는다
        except httpx.TimeoutException as err:
            raise _model_error(
                ErrorCode.NET_002,
                "provider request timed out",
                agent=self.agent,
                retryable=True,
                cause=type(err).__name__,
                category=ErrorCategory.TIMEOUT,
            ) from err
        except httpx.TransportError as err:
            # MODEL 로 덮으면 NETWORK_RETRY 와 선언 retry.network 가 한 번도 동작하지 않는다
            raise _model_error(
                ErrorCode.NET_001,
                "provider connection failed",
                agent=self.agent,
                retryable=True,
                cause=type(err).__name__,
                category=ErrorCategory.NETWORK,
            ) from err

    def _raise_for_failure(self, response: httpx.Response) -> None:
        """2xx 가 아닌 응답을 실패로 올린다 — 변환 대상이 아니면 httpx 에러 그대로."""
        status = response.status_code
        cause = f"http_{status}"
        if status == HTTP_RATE_LIMITED:
            raise _model_error(
                ErrorCode.LLM_001,
                "provider rate limited",
                agent=self.agent,
                retryable=True,
                cause=cause,
                # 재시도 전략이 다르다 — 10초 시작 5회 (RATE_LIMIT_RETRY)
                category=ErrorCategory.RATE_LIMIT,
            )
        if status >= HTTP_SERVER_ERROR:
            raise _model_error(
                ErrorCode.LLM_003,
                "provider server error",
                agent=self.agent,
                retryable=True,
                cause=cause,
            )
        if status == HTTP_BAD_REQUEST and _is_context_overflow(response):
            raise _model_error(
                ErrorCode.LLM_002,
                "context length exceeded",
                agent=self.agent,
                # 같은 프롬프트로 다시 보내도 같은 길이다 — 줄여야 한다
                retryable=False,
                cause=cause,
            )
        if status >= HTTP_MULTIPLE_CHOICES:
            # 3xx 도 여기서 올린다 — 리다이렉트를 따라가면 Bearer 가 다른 호스트로 간다
            response.raise_for_status()

    def _usage(self, body: Any) -> ModelUsage:
        """토큰 집계 — 읽을 수 없는 쪽은 0 으로 세되 그 사실을 남긴다.

        답은 멀쩡하다: 집계가 없거나 깨졌다고 태스크를 실패시키지 않는다. ``usage`` 가 없든,
        비었든, 필드가 빠졌든, 숫자가 아니든, 음수든 같은 취급이다.
        """
        reported = body.get("usage") if isinstance(body, dict) else None
        fields = reported if isinstance(reported, dict) else {}
        counts = [_token_count(fields.get(name)) for name in ("prompt_tokens", "completion_tokens")]
        if None in counts:
            # 조용히 0 이 되면 토큰 지표가 이유 없이 내려앉는다
            log.warning(
                "provider response carries no readable token usage, counting zero",
                agent=self.agent,
                provider=PROVIDER,
                model=self.config.name,
            )
        return ModelUsage(input_tokens=counts[0] or 0, output_tokens=counts[1] or 0)

    def _interpret(self, response: httpx.Response) -> ModelResponse:
        """Convert a provider response into the framework's response.

        provider 응답을 프레임워크 표현으로 옮깁니다.

        Raises:
            AaestroError: MODEL/``LLM_004`` if the payload has no usable content.
        """
        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
            if not isinstance(message, dict):
                raise TypeError("message is not an object")
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise TypeError("content is not text")
            tool_calls = tuple(_tool_call(call) for call in message.get("tool_calls") or ())
            usage = self._usage(body)
        except (KeyError, IndexError, TypeError, ValueError) as err:
            # 응답 모양이 계약과 다르면 그대로 흘려보내지 않는다 — 조용히 빈
            # 응답으로 처리하면 태스크가 성공한 것처럼 끝난다. 잘린 tool 인자도 여기로 온다
            raise _model_error(
                ErrorCode.LLM_004,
                "model response could not be interpreted",
                agent=self.agent,
                retryable=False,
                cause=type(err).__name__,
            ) from err

        if choice.get("finish_reason") == FINISH_LENGTH:
            # 출력 상한에서 끊긴 텍스트다 — 형제 어댑터처럼 응답은 돌려주되, 잘렸다는 사실은 남긴다
            log.warning(
                "model output truncated at the token limit",
                agent=self.agent,
                provider=PROVIDER,
                model=self.config.name,
                output_tokens=usage.output_tokens,
            )
        # 원본 메시지를 들고 간다 — provider 고유 필드가 다음 턴에 바뀌지 않은 채 돌아가야 한다
        return ModelResponse(
            content=content or "",
            tool_calls=tool_calls,
            usage=usage,
            raw=_ProviderMessage(_returnable(message)),
        )


def _token_count(value: Any) -> int | None:
    """토큰 수로 읽을 수 있는 값이면 그 수 — 아니면 None. bool 은 수가 아니다."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _tool_call(call: Any) -> ToolCall:
    """provider 의 tool 호출 하나 — 인자는 JSON 문자열이다. 못 읽으면 호출자가 LLM_004 로 올린다."""
    function = call["function"]
    arguments = function["arguments"]
    if isinstance(arguments, str):
        # 인자 없는 호출을 빈 문자열로 내는 호환 서버가 흔하다. JSONDecodeError 는 ValueError 다
        arguments = json.loads(arguments) if arguments.strip() else {}
    if not isinstance(arguments, dict):
        raise TypeError("tool arguments are not an object")
    identifier, name = call["id"], function["name"]
    if not isinstance(identifier, str) or not isinstance(name, str) or not name:
        raise TypeError("tool call has no id or name")
    return ToolCall(id=identifier, name=name, arguments=arguments)


def _returnable(message: Mapping[str, Any]) -> Mapping[str, Any]:
    """다음 턴에 되돌려 보낼 수 있는 모양의 assistant 메시지.

    되돌려 보내는 ``tool_calls`` 의 ``arguments`` 는 항상 JSON 객체 문자열이어야 한다. 받을 때는
    빈 문자열과 객체로 온 인자를 관대하게 읽는데(:func:`_tool_call`), 그것을 그대로 내보내면
    엄격한 서버가 다음 턴을 400 으로 거절한다 — 도구가 이미 실행된 뒤에 태스크가 실패한다.

    그 모양이 아닌 ``arguments`` 만 :func:`_tool_call` 이 읽은 값의 JSON 으로 바꾼다. 나머지는
    그대로다 — 원본을 들고 가는 이유(provider 고유 필드 보존)를 깨지 않는다. 응답 객체를
    제자리에서 고치지 않고, 바꾼 호출만 새로 만든다.
    """
    calls = message.get("tool_calls")
    if not calls:
        return message
    returnable = [_returnable_call(call) for call in calls]
    if all(new is old for new, old in zip(returnable, calls, strict=True)):
        return message
    return {**message, "tool_calls": returnable}


def _returnable_call(call: Any) -> Any:
    """호출 하나 — ``arguments`` 가 비어 있지 않은 문자열이면 받은 객체 그대로 돌려준다."""
    arguments = call["function"]["arguments"]
    if isinstance(arguments, str) and arguments.strip():
        return call
    normalized = json.dumps(arguments if isinstance(arguments, dict) else {})
    return {**call, "function": {**call["function"], "arguments": normalized}}


def to_messages(message: Message) -> list[dict[str, Any]]:
    """One framework conversation turn in the provider's message shape.

    대화 한 칸을 provider 메시지로 옮깁니다. 도구 결과는 호출마다 ``role: tool`` 메시지 하나라
    한 칸이 여러 메시지가 됩니다.
    """
    if isinstance(message, UserTurn):
        return [{"role": "user", "content": [{"type": "text", "text": p} for p in message.parts]}]
    if isinstance(message, AssistantTurn):
        return [_assistant_message(message)]
    if isinstance(message, ToolTurn):
        return [
            {
                "role": "tool",
                "tool_call_id": outcome.call_id,
                # Chat Completions 에는 is_error 자리가 없다 — 표시를 버리면 모델이 실패한 결과를
                # 성공으로 읽는다. 본문 앞에 남긴다
                "content": f"{TOOL_ERROR_MARK}\n{outcome.content}"
                if outcome.is_error
                else outcome.content,
            }
            for outcome in message.outcomes
        ]
    raise TypeError(f"unknown conversation turn: {type(message).__name__}")


def _assistant_message(turn: AssistantTurn) -> dict[str, Any]:
    """이 어댑터가 받은 원본이면 그대로 — 아니면(다른 모델이 만든 턴) 텍스트와 호출로 짓는다."""
    if isinstance(turn.raw, _ProviderMessage):
        return dict(turn.raw.message)
    rebuilt: dict[str, Any] = {"role": "assistant", "content": turn.content or None}
    if turn.tool_calls:
        rebuilt["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(dict(call.arguments))},
            }
            for call in turn.tool_calls
        ]
    return rebuilt


def _to_tool_schema(tool: Any) -> dict[str, Any]:
    """Render a tool spec in the provider's format.

    tool 계약을 provider 형식으로 옮깁니다. ``mcp__`` 네임스페이스는 그대로 유지됩니다 — 이름이
    곧 출처 판별이기 때문입니다 (05 Layer Rules). OpenAI 는 함수 이름을
    ``^[a-zA-Z0-9_-]{1,64}$`` 로 제한하는데, 넘는 이름을 여기서 자르거나 바꾸지 않습니다:
    바꾸면 모델이 부른 이름이 registry 의 이름과 달라집니다. 넘으면 provider 가 400 으로 거절합니다.
    """
    schema = tool if isinstance(tool, dict) else tool.to_tool_schema()
    return {
        "type": "function",
        "function": {
            "name": schema["name"],
            "description": schema.get("description", ""),
            # 빈 스키마를 거절하는 서버가 있다 — 인자 없는 도구는 빈 object 로 선언한다
            "parameters": schema.get("parameters")
            or schema.get("input_schema")
            or _EMPTY_PARAMETERS,
        },
    }


__all__ = [
    "API_KEY_ENV",
    "BASE_URL_ENV",
    "CHAT_COMPLETIONS_PATH",
    "PROVIDER",
    "OpenAICompatModel",
    "to_messages",
]
