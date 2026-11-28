"""The credential the proxy injects does not come back to the agent (#390).

upstream 대역이 주입된 키 (전체 · 앞 · 뒤 조각) 를 에러 본문과 응답 헤더에 되비춘다. 에이전트가
받는 응답에 그 문자열이 없어야 하고, 어댑터의 context 초과 판별 (``LLM_002``) 은 가림 뒤에도
동작해야 한다.
"""

from __future__ import annotations

import base64
import gzip
import json
import tracemalloc
import zlib

import anthropic
import httpx
import httpx2
import pytest
from fastapi import FastAPI

from aaestro.agentd.providers.anthropic import AnthropicModel
from aaestro.agentd.providers.openai_compat import OpenAICompatModel
from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.core.manifest import ModelConfig
from aaestro.egress.providers import (
    ANTHROPIC_ENDPOINTS,
    ANTHROPIC_REQUEST_HEADERS,
    OPENAI_COMPAT_ENDPOINTS,
    OPENAI_COMPAT_KEY_HEADER,
    OPENAI_COMPAT_KEY_SCHEME,
    Upstream,
    create_provider_app,
)
from aaestro.egress.redact import MAX_ERROR_BODY_BYTES, REDACTED, WITHHELD_MESSAGE, redact
from tests.unit.egress.test_connect import Registry

KEY = "sk-proj-Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z"  # 테스트 값
HEAD, TAIL, MIDDLE = KEY[:8], KEY[-4:], KEY[14:26]
IDENTITY = "cred-researcher"
COMPAT_HOST = "llm.internal.example"


class Echo:
    """upstream 대역 — 정해 둔 상태 · 헤더 · 본문으로 답한다."""

    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.body = body
        self.headers = {"content-type": "application/json", **(headers or {})}

    def handle(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(self.status, headers=self.headers, stream=httpx.ByteStream(self.body))


def proxy_app(echo: Echo) -> FastAPI:
    registry = Registry()
    registry.allowed |= {"api.anthropic.com", COMPAT_HOST}
    return create_provider_app(
        registry,
        {
            "anthropic": Upstream(
                base_url="https://api.anthropic.com",
                target="api.anthropic.com",
                api_key=KEY,
                endpoints=ANTHROPIC_ENDPOINTS,
                request_headers=ANTHROPIC_REQUEST_HEADERS,
            ),
            "openai-compat": Upstream(
                base_url=f"https://{COMPAT_HOST}",
                target=COMPAT_HOST,
                api_key=KEY,
                key_header=OPENAI_COMPAT_KEY_HEADER,
                key_scheme=OPENAI_COMPAT_KEY_SCHEME,
                endpoints=OPENAI_COMPAT_ENDPOINTS,
            ),
        },
        http=httpx.AsyncClient(transport=httpx.MockTransport(echo.handle)),
    )


def proxy(echo: Echo) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=proxy_app(echo))
    return httpx.AsyncClient(transport=transport, base_url="http://egress")


async def call(echo: Echo) -> httpx.Response:
    async with proxy(echo) as client:
        return await client.post("/anthropic/v1/messages", json={}, headers={"x-api-key": IDENTITY})


def leaks(text: str) -> list[str]:
    return [piece for piece in (KEY, HEAD, TAIL, MIDDLE) if piece in text]


@pytest.mark.parametrize("status", [401, 403])
async def test_an_auth_failure_echoing_the_key_reaches_the_agent_without_it(status):
    message = f"Incorrect API key provided: {HEAD}****{TAIL}. Full: {KEY}"
    body = {
        "error": {"message": message, "type": "invalid_request_error", "code": "invalid_api_key"}
    }
    response = await call(Echo(status, json.dumps(body).encode()))

    assert response.status_code == status
    assert leaks(response.text) == []
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error" and error["code"] == "invalid_api_key"
    assert "Incorrect API key" not in error["message"], "401 · 403 메시지는 고정 문구다"


async def test_a_400_keeps_its_message_but_not_the_key():
    message = f"prompt is too long: 250000 tokens > 200000 maximum (key {HEAD}...{TAIL})"
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    response = await call(Echo(400, json.dumps(body).encode()))

    assert response.status_code == 400 and leaks(response.text) == []
    assert response.json()["error"]["message"].startswith("prompt is too long: 250000 tokens")


@pytest.mark.parametrize(
    "body",
    [b"<html>bad key " + KEY.encode() + b"</html>", b"[1, 2]", b"x" * (MAX_ERROR_BODY_BYTES + 1)],
    ids=["html", "json-array", "oversized"],
)
async def test_a_non_json_or_oversized_error_body_is_withheld(body):
    response = await call(Echo(502, body, {"content-type": "text/html"}))

    assert response.status_code == 502 and leaks(response.text) == []
    assert response.json()["error"]["message"] == WITHHELD_MESSAGE
    assert response.headers["content-type"] == "application/json"


async def test_response_headers_are_an_allowlist_and_their_values_are_masked():
    """요청 헤더를 되비추는 upstream — 목록 밖 헤더는 빠지고, 남는 헤더 값의 키도 가린다."""
    echo = Echo(
        200,
        b"{}",
        {
            "x-echo-authorization": f"Bearer {KEY}",
            "x-debug": TAIL,
            "request-id": f"req-1 {KEY}",
            "anthropic-ratelimit-requests-remaining": "41",
            "retry-after": "3",
            "set-cookie": "s=1",
        },
    )
    response = await call(echo)

    assert response.status_code == 200
    assert leaks(str(response.headers)) == []
    assert "x-echo-authorization" not in response.headers and "x-debug" not in response.headers
    assert "set-cookie" not in response.headers
    assert response.headers["request-id"] == f"req-1 {REDACTED}"
    assert response.headers["anthropic-ratelimit-requests-remaining"] == "41"
    assert response.headers["retry-after"] == "3"


async def test_an_error_response_keeps_the_retry_hints_but_not_the_echo():
    echo = Echo(429, b'{"error": {"type": "rate_limit_error", "message": "slow down"}}',
                {"retry-after": "7", "x-echo": KEY, "content-encoding": "identity"})  # fmt: skip
    response = await call(echo)

    assert response.status_code == 429 and response.headers["retry-after"] == "7"
    assert leaks(str(response.headers)) == [] and "x-echo" not in response.headers


async def test_a_successful_stream_passes_byte_for_byte_with_its_encoding():
    """2xx 는 버퍼링하지 않는다 — 압축된 원문도 그대로, 해제는 에이전트 쪽 클라이언트가 한다."""
    events = b"event: message_start\ndata: {}\n\nevent: message_stop\ndata: {}\n\n"
    raw = gzip.compress(events)
    echo = Echo(200, raw, {"content-type": "text/event-stream", "content-encoding": "gzip"})
    async with proxy(echo) as client:
        request = client.build_request(
            "POST", "/anthropic/v1/messages", json={}, headers={"x-api-key": IDENTITY}
        )
        response = await client.send(request, stream=True)
        received = b"".join([chunk async for chunk in response.aiter_raw()])
        await response.aclose()

    assert received == raw
    assert response.headers["content-encoding"] == "gzip"


# --- 어댑터의 context 초과 판별은 가림 뒤에도 동작한다 ----------------------------


async def test_anthropic_still_reads_a_masked_overflow_as_llm_002():
    message = f"prompt is too long: 250000 tokens > 200000 maximum. key={KEY}"
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    app = proxy_app(Echo(400, json.dumps(body).encode()))
    # anthropic SDK 는 httpx2 클라이언트만 받는다 — 같은 프록시 앱을 httpx2 의 ASGI 전송으로 잇는다
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as client:
        model = AnthropicModel(
            config=ModelConfig(provider="anthropic", name="claude-test"),  # type: ignore[arg-type]
            agent="researcher",
            client=anthropic.AsyncAnthropic(
                api_key=IDENTITY,
                base_url="http://egress/anthropic",
                http_client=client,
                max_retries=0,
            ),
        )
        with pytest.raises(AaestroError) as caught:
            await model.run("system", [], [])

    assert caught.value.code == ErrorCode.LLM_002
    assert leaks(str(caught.value.__cause__)) == []


@pytest.mark.parametrize(
    "error",
    [
        {"message": f"bad request for {KEY}", "type": "invalid_request_error",
         "code": "context_length_exceeded"},
        {"message": f"This model's maximum context length is 8192 tokens ({HEAD}...{TAIL})"},
    ],
)  # fmt: skip
async def test_openai_compat_still_reads_a_masked_overflow_as_llm_002(error):
    async with proxy(Echo(400, json.dumps({"error": error}).encode())) as client:
        model = OpenAICompatModel(
            config=ModelConfig(provider="openai-compat", name="m"),  # type: ignore[arg-type]
            agent="researcher",
            base_url="http://egress/openai-compat",
            credential=IDENTITY,
            client=client,
        )
        with pytest.raises(AaestroError) as caught:
            await model.run("system", [], [])

    assert caught.value.code == ErrorCode.LLM_002


# --- 조각의 기준 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        (f"key {HEAD}****{TAIL}", f"key {REDACTED}****{REDACTED}"),
        (f"key {KEY[:4]}...{KEY[-4:]}", f"key {REDACTED}...{REDACTED}"),
        (f"middle {MIDDLE} here", f"middle {REDACTED} here"),
        (f"Bearer {KEY}.", f"Bearer {REDACTED}."),
        # 지우지 않는 것 — 가운데의 짧은 조각, 다른 글자에 붙은 꼬리, 평범한 낱말
        (f"short {KEY[14:18]}", f"short {KEY[14:18]}"),
        (f"tail {TAIL}9", f"tail {TAIL}9"),
        ("context length exceeded: 250000 tokens", "context length exceeded: 250000 tokens"),
    ],
)
def test_the_fragment_rule(text, masked):
    assert redact(text, KEY) == masked


def test_an_empty_secret_masks_nothing():
    assert redact("anything", "") == "anything"


# --- 독립 리뷰 반영: 인코딩된 키 · 압축 폭탄 · 읽기 실패 · 직렬화할 수 없는 수 ---------


def _percent(text: str) -> str:
    return "".join(f"%{byte:02X}" for byte in text.encode())


ENCODED_FORMS = {
    "upper": KEY.upper(),
    "percent": _percent(KEY),
    "base64": base64.b64encode(f"Bearer {KEY}".encode()).decode(),
    "base64-url": base64.urlsafe_b64encode(f"user:{KEY}".encode()).decode(),
    # 감싼 문자열의 앞이 붙고 뒤가 잘린 base64 — 토막 전체는 키의 인코딩 안에 들지 않는다
    "base64-partial": base64.b64encode(f"Bearer {KEY}".encode()).decode()[:-12] + "...",
}


@pytest.mark.parametrize("form", list(ENCODED_FORMS.values()), ids=list(ENCODED_FORMS))
async def test_an_encoded_key_in_an_error_body_is_masked(form):
    body = {"error": {"type": "invalid_request_error", "message": f"context length: {form}"}}
    response = await call(Echo(400, json.dumps(body).encode()))

    assert response.status_code == 400
    assert form not in response.text and leaks(response.text) == []
    assert response.json()["error"]["message"].startswith("context length: ")


@pytest.mark.parametrize("form", list(ENCODED_FORMS.values()), ids=list(ENCODED_FORMS))
async def test_an_encoded_key_in_a_returned_header_is_masked(form):
    response = await call(Echo(200, b"{}", {"x-request-id": f"req-9 {form}"}))

    assert response.status_code == 200
    value = response.headers["x-request-id"]
    assert form not in value and leaks(value) == []
    assert value.startswith("req-9 ")


async def test_a_compression_bomb_is_never_inflated_past_the_cap():
    bomb = gzip.compress(bytes(20 * 1024 * 1024))
    echo = Echo(429, bomb, {"content-encoding": "gzip", "retry-after": "5"})
    tracemalloc.start()
    try:
        response = await call(echo)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert response.status_code == 429 and response.headers["retry-after"] == "5"
    assert response.json()["error"]["message"] == WITHHELD_MESSAGE
    assert peak < 16 * MAX_ERROR_BODY_BYTES, f"peak {peak} bytes"


async def test_a_body_within_the_cap_is_still_inflated():
    body = {"error": {"type": "invalid_request_error", "message": "context length exceeded"}}
    raw = gzip.compress(json.dumps(body).encode())
    response = await call(Echo(400, raw, {"content-encoding": "gzip"}))

    assert response.json() == body and "content-encoding" not in response.headers


class BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"error": {"message": "par'
        raise httpx.ReadError("connection reset")


class Failing(Echo):
    def handle(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(self.status, headers=self.headers, stream=BrokenStream())


@pytest.mark.parametrize(
    "echo",
    [
        Echo(
            429,
            b"\x1f\x8b\x08\x00not-really-gzip",
            {"content-encoding": "gzip", "retry-after": "5"},
        ),
        Echo(429, b'{"x": 1}', {"content-encoding": "br", "retry-after": "5"}),
        Failing(429, b"", {"retry-after": "5"}),
        Echo(429, b'{"quota": 1e999}', {"retry-after": "5"}),
        Echo(429, b'{"quota": NaN}', {"retry-after": "5"}),
        # 짝 없는 서로게이트 — json 은 받지만 UTF-8 응답으로 쓸 수 없다 (값 · 키 둘 다)
        Echo(429, b'{"error": {"message": "bad \\ud800"}}', {"retry-after": "5"}),
        Echo(429, b'{"\\udfff": 1}', {"retry-after": "5"}),
        # 끝이 잘린 압축 — 앞부분만으로도 JSON 이 풀리지만 표준 해제기는 거절한다
        Echo(
            429,
            gzip.compress(b'{"error": {"message": "x"}}')[:-8],
            {"content-encoding": "gzip", "retry-after": "5"},
        ),
        Echo(
            429,
            zlib.compress(b'{"error": {"message": "x"}}')[:-4],
            {"content-encoding": "deflate", "retry-after": "5"},
        ),
    ],
    ids=[
        "broken-gzip",
        "unknown-encoding",
        "read-error",
        "infinite-number",
        "nan",
        "lone-surrogate-value",
        "lone-surrogate-key",
        "truncated-gzip",
        "truncated-deflate",
    ],
)
async def test_an_unreadable_error_body_keeps_the_status_and_retry_hint(echo):
    response = await call(echo)

    assert response.status_code == 429 and response.headers["retry-after"] == "5"
    assert response.json()["error"]["message"] == WITHHELD_MESSAGE
