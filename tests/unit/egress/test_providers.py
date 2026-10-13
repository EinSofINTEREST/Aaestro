"""Model API termination with key injection (#293)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from aaestro.egress.connect import EgressMode
from aaestro.egress.providers import (
    ANTHROPIC_ENDPOINTS,
    ANTHROPIC_REQUEST_HEADERS,
    OPENAI_COMPAT_ENDPOINTS,
    OPENAI_COMPAT_KEY_HEADER,
    OPENAI_COMPAT_KEY_SCHEME,
    Upstream,
    create_provider_app,
)
from tests.unit.egress.test_connect import Registry

KEY = "sk-real-provider-key"


class Provider:
    """upstream 대역 — 받은 요청을 남기고 스트리밍으로 답한다."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "request-id": "req-1"},
            stream=httpx.ByteStream(b"event: message_start\n\ndata: {}\n\n"),
        )


def app_for(registry: Registry, provider: Provider, mode: EgressMode = EgressMode.ENFORCE):
    registry.allowed.add("api.anthropic.com")
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider.handle))
    app = create_provider_app(
        registry,
        {
            "anthropic": Upstream(
                base_url="https://api.anthropic.com",
                logical_host="api.anthropic.com",
                api_key=KEY,
                endpoints=ANTHROPIC_ENDPOINTS,
                request_headers=ANTHROPIC_REQUEST_HEADERS,
            )
        },
        mode=mode,
        http=upstream,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://egress")


async def call(client: httpx.AsyncClient, *, key: str | None = "cred-researcher", **headers):
    if key is not None:
        headers["x-api-key"] = key
    return await client.post(
        "/anthropic/v1/messages?beta=true",
        json={"model": "claude-opus-5", "messages": []},
        headers={"anthropic-version": "2023-06-01", **headers},
    )


async def test_the_agent_identity_is_swapped_for_the_provider_key():
    """에이전트는 키를 모른다 — 신원을 떼고 진짜 키를 붙여 보낸다."""
    provider = Provider()
    async with app_for(Registry(), provider) as client:
        response = await call(client, authorization="Bearer should-be-dropped")

    assert response.status_code == 200
    assert response.text.startswith("event: message_start")
    [sent] = provider.seen
    assert sent.headers["x-api-key"] == KEY
    assert "authorization" not in sent.headers, "신원이 provider 로 새어 나갔다"
    assert "cred-researcher" not in str(sent.headers)
    assert str(sent.url) == "https://api.anthropic.com/v1/messages?beta=true"
    assert sent.headers["anthropic-version"] == "2023-06-01"


async def test_a_bearer_identity_is_accepted_too():
    provider = Provider()
    async with app_for(Registry(), provider) as client:
        response = await call(client, key=None, authorization="Bearer cred-researcher")

    assert response.status_code == 200 and provider.seen[0].headers["x-api-key"] == KEY


async def test_a_revoked_model_api_is_refused_without_reaching_the_provider():
    registry = Registry()
    provider = Provider()
    async with app_for(registry, provider) as client:
        registry.allowed.discard("api.anthropic.com")
        response = await call(client)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACC_001"
    assert provider.seen == []


async def test_record_mode_forwards_a_denied_call():
    registry = Registry()
    provider = Provider()
    async with app_for(registry, provider, EgressMode.RECORD) as client:
        registry.allowed.discard("api.anthropic.com")
        response = await call(client)

    assert response.status_code == 200 and len(provider.seen) == 1


@pytest.mark.parametrize(
    ("key", "down", "status"),
    [(None, False, 401), ("stolen", False, 401), ("cred-researcher", True, 503)],
)
async def test_no_identity_unknown_identity_and_no_decision_never_reach_the_provider(
    key, down, status
):
    registry = Registry()
    provider = Provider()
    async with app_for(registry, provider, EgressMode.RECORD) as client:
        registry.down = down
        response = await call(client, key=key)

    assert response.status_code == status and provider.seen == []


async def test_an_unknown_provider_is_not_found():
    async with app_for(Registry(), Provider()) as client:
        response = await client.post("/openai/v1/chat", headers={"x-api-key": "cred-researcher"})

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("DELETE", "/anthropic/v1/files/file-1"),
        ("POST", "/anthropic/v1/organizations/keys"),
        ("GET", "/anthropic/v1/messages"),
    ],
)
async def test_only_model_api_endpoints_get_the_key(method, path):
    """호스트 판정만으로 경로를 열면 모델 호출 허락이 프록시 키로 다른 API 를 부르는 권한이 된다."""
    provider = Provider()
    async with app_for(Registry(), provider) as client:
        response = await client.request(method, path, headers={"x-api-key": "cred-researcher"})

    assert response.status_code == 403 and response.json()["error"]["code"] == "ACC_001"
    assert provider.seen == []


async def test_counting_tokens_and_listing_models_are_model_api_calls():
    provider = Provider()
    async with app_for(Registry(), provider) as client:
        counted = await client.post(
            "/anthropic/v1/messages/count_tokens", json={}, headers={"x-api-key": "cred-researcher"}
        )
        listed = await client.get("/anthropic/v1/models", headers={"x-api-key": "cred-researcher"})

    assert (counted.status_code, listed.status_code) == (200, 200)


def plaintext_app(provider: Provider, addresses: list[str]):
    registry = Registry()
    registry.allowed.add("api.anthropic.com")

    async def resolver(host, port):
        return addresses

    app = create_provider_app(
        registry,
        {
            "anthropic": Upstream(
                base_url="http://fake-provider:8000",
                logical_host="api.anthropic.com",
                api_key=KEY,
                endpoints=ANTHROPIC_ENDPOINTS,
                request_headers=ANTHROPIC_REQUEST_HEADERS,
            )
        },
        http=httpx.AsyncClient(transport=httpx.MockTransport(provider.handle)),
        resolver=resolver,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://egress")


async def test_a_plaintext_upstream_is_pinned_to_its_private_address():
    """평문은 사설로 풀린 주소로만, 그 주소에 붙는다 — 판정과 연결 사이에 이름이 바뀌지 못하게."""
    provider = Provider()
    async with plaintext_app(provider, ["172.20.0.9"]) as client:
        response = await call(client)

    assert response.status_code == 200
    [sent] = provider.seen
    assert (sent.url.host, sent.url.port) == ("172.20.0.9", 8000)
    assert sent.headers["host"] == "fake-provider:8000"


async def test_a_plaintext_upstream_that_resolves_publicly_never_gets_the_key():
    """사설 목록에 있는 이름이라도 공인 주소로 풀리면 키를 평문으로 보내지 않는다 (#305 리뷰)."""
    provider = Provider()
    async with plaintext_app(provider, ["93.184.216.34"]) as client:
        response = await call(client)

    assert response.status_code == 502
    assert provider.seen == []


# --- OpenAI 호환 provider (#361) --------------------------------------------

COMPAT_HOST = "llm.internal.example"
COMPAT_KEY = "sk-compat-provider-key"


def compat_asgi(registry: Registry, provider: Provider):
    registry.allowed.add(COMPAT_HOST)
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider.handle))
    return create_provider_app(
        registry,
        {
            "openai-compat": Upstream(
                base_url=f"https://{COMPAT_HOST}",
                logical_host=COMPAT_HOST,
                api_key=COMPAT_KEY,
                key_header=OPENAI_COMPAT_KEY_HEADER,
                key_scheme=OPENAI_COMPAT_KEY_SCHEME,
                endpoints=OPENAI_COMPAT_ENDPOINTS,
            )
        },
        http=upstream,
    )


def compat_app(registry: Registry, provider: Provider):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=compat_asgi(registry, provider)),
        base_url="http://egress",
    )


IDENTITY = {"authorization": "Bearer cred-researcher"}


async def raw_call(app, method: str, path: str) -> tuple[int, bytes]:
    """경로를 **원문 그대로** 앱에 넣는다 — httpx 는 보내기 전에 ``..`` 을 접어 버린다."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"egress"), (b"authorization", b"Bearer cred-researcher")],
        "server": ("egress", 80),
        "client": ("10.0.0.1", 1),
    }
    delivered = asyncio.Event()
    status: list[int] = []
    body = bytearray()

    async def receive():
        if delivered.is_set():
            # 응답이 끝날 때까지 연결을 유지한다 — 바로 끊으면 스트리밍 응답이 취소된다
            await asyncio.Event().wait()
        delivered.set()
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status.append(message["status"])
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    return status[0], bytes(body)


async def test_the_bearer_identity_is_replaced_with_the_bearer_provider_key():
    """같은 헤더 자리다 — 에이전트의 신원을 떼고 그 자리에 진짜 키를 싣는다."""
    provider = Provider()
    async with compat_app(Registry(), provider) as client:
        response = await client.post(
            "/openai-compat/v1/chat/completions", json={"model": "m"}, headers=IDENTITY
        )

    assert response.status_code == 200
    [sent] = provider.seen
    assert sent.headers.get_list("authorization") == [f"Bearer {COMPAT_KEY}"]
    assert "cred-researcher" not in str(sent.headers)
    assert str(sent.url) == f"https://{COMPAT_HOST}/v1/chat/completions"


async def test_an_x_api_key_identity_never_reaches_the_openai_compat_upstream():
    provider = Provider()
    async with compat_app(Registry(), provider) as client:
        await client.post(
            "/openai-compat/v1/chat/completions",
            json={},
            headers={"x-api-key": "cred-researcher", "Authorization": "Bearer something-else"},
        )

    [sent] = provider.seen
    assert "x-api-key" not in sent.headers
    assert sent.headers.get_list("authorization") == [f"Bearer {COMPAT_KEY}"]


async def test_the_provider_key_is_not_returned_to_the_agent_on_a_refusal():
    registry = Registry()
    provider = Provider()
    async with compat_app(registry, provider) as client:
        registry.allowed.discard(COMPAT_HOST)
        response = await client.post(
            "/openai-compat/v1/chat/completions", json={}, headers=IDENTITY
        )

    assert response.status_code == 403 and response.json()["error"]["code"] == "ACC_001"
    assert COMPAT_KEY not in response.text and provider.seen == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/openai-compat/v1/chat/completions"),
        ("POST", "/openai-compat/v1/completions"),
        ("POST", "/openai-compat/v1/files"),
        ("POST", "/openai-compat/v1/fine_tuning/jobs"),
        ("GET", "/openai-compat/v1/models"),
        ("DELETE", "/openai-compat/v1/models/ft-1"),
        # 매칭 우회 시도 — 정확히 일치하는 경로만 연다 (정규화해서 맞춰 보지 않는다)
        ("POST", "/openai-compat/v1/chat/completions/../files"),
        ("POST", "/openai-compat/v1/files/../chat/completions"),
        ("POST", "/openai-compat/v1/chat/completions/.."),
        ("POST", "/openai-compat/./v1/chat/completions"),
        ("POST", "/openai-compat/v1/chat/completions/%2e%2e/files"),
        ("POST", "/openai-compat/v1/chat/completions%2f..%2ffiles"),
        ("POST", "/openai-compat/V1/Chat/Completions"),
        ("POST", "/openai-compat/v1//chat/completions"),
        ("POST", "/openai-compat/v1/chat/completions;x=1"),
        ("POST", "/openai-compat/v1/chat/completions?x"),
        ("POST", "/openai-compat/v1/chat/completions#x"),
    ],
)
async def test_only_chat_completions_gets_the_openai_compat_key(method, path):
    provider = Provider()

    status, body = await raw_call(compat_asgi(Registry(), provider), method, path)

    assert status == 403 and json.loads(body)["error"]["code"] == "ACC_001"
    assert provider.seen == [] and COMPAT_KEY.encode() not in body


@pytest.mark.parametrize(
    "path",
    [
        "/openai-compat//v1/chat/completions",
        "/openai-compat/v1/chat/completions/",
        "/openai-compat/v1/chat/completions//",
        "/openai-compat///v1/chat/completions///",
    ],
)
async def test_the_matched_path_is_the_path_that_is_forwarded(path):
    """판정한 것과 보내는 것이 같아야 한다 — 앞뒤 슬래시를 붙여도 upstream 은 한 경로만 본다."""
    provider = Provider()

    status, _ = await raw_call(compat_asgi(Registry(), provider), "POST", path)

    assert status == 200
    [sent] = provider.seen
    assert sent.url.raw_path == b"/v1/chat/completions"


async def test_a_trailing_slash_reaches_anthropic_as_the_declared_path_with_its_query():
    """같은 코드가 anthropic upstream 에도 적용된다 — 쿼리는 그대로 따라간다."""
    provider = Provider()
    async with app_for(Registry(), provider) as client:
        response = await client.post(
            "/anthropic/v1/messages/?beta=true&x=a%2Fb",
            json={},
            headers={"x-api-key": "cred-researcher"},
        )

    assert response.status_code == 200
    [sent] = provider.seen
    assert sent.url.raw_path == b"/v1/messages?beta=true&x=a%2Fb"


async def test_the_provider_name_is_matched_exactly():
    provider = Provider()

    status, _ = await raw_call(
        compat_asgi(Registry(), provider), "POST", "/OPENAI-COMPAT/v1/chat/completions"
    )

    assert status == 404 and provider.seen == []


async def test_a_query_string_does_not_change_which_endpoint_is_matched():
    """쿼리는 매칭에 들지 않고 그대로 전달된다 — 다른 경로를 열지 못한다."""
    provider = Provider()
    async with compat_app(Registry(), provider) as client:
        allowed = await client.post(
            "/openai-compat/v1/chat/completions?x=/../files", json={}, headers=IDENTITY
        )
        refused = await client.post(
            "/openai-compat/v1/files?path=v1/chat/completions", json={}, headers=IDENTITY
        )

    assert (allowed.status_code, refused.status_code) == (200, 403)
    [sent] = provider.seen
    assert sent.url.path == "/v1/chat/completions"


# --- 에이전트 사이에 상태가 건너가지 않는다 (#361 리뷰) ----------------------------


class SessionProvider:
    """세션을 고정하는 게이트웨이 대역 — 답마다 쿠키를 심고, 받은 요청을 남긴다."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return httpx.Response(
            200,
            headers=[
                ("content-type", "application/json"),
                ("set-cookie", f"session=FROM-{len(self.seen)}; Path=/"),
                ("set-cookie", "route=sticky; Path=/"),
                ("request-id", "req-1"),
            ],
            stream=httpx.ByteStream(b"{}"),
        )


async def test_an_upstream_cookie_is_not_carried_into_another_agents_call():
    """클라이언트 하나를 모든 에이전트가 쓴다 — 쿠키를 기억하면 A 의 세션이 B 의 호출에 실린다."""
    registry = Registry()
    registry.identities["cred-writer"] = "writer"
    provider = SessionProvider()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider.handle))
    # 주입한 클라이언트가 이미 쿠키를 쥐고 있어도 실리지 않아야 한다
    upstream.cookies.set("session", "PRELOADED", domain=COMPAT_HOST)
    registry.allowed.add(COMPAT_HOST)
    app = create_provider_app(
        registry,
        {
            "openai-compat": Upstream(
                base_url=f"https://{COMPAT_HOST}",
                logical_host=COMPAT_HOST,
                api_key=COMPAT_KEY,
                key_header=OPENAI_COMPAT_KEY_HEADER,
                key_scheme=OPENAI_COMPAT_KEY_SCHEME,
                endpoints=OPENAI_COMPAT_ENDPOINTS,
            )
        },
        http=upstream,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://egress"
    ) as client:
        for credential in ("cred-researcher", "cred-writer", "cred-researcher"):
            response = await client.post(
                "/openai-compat/v1/chat/completions",
                json={},
                headers={"authorization": f"Bearer {credential}"},
            )
            assert response.status_code == 200

    assert len(provider.seen) == 3
    assert all("cookie" not in sent.headers for sent in provider.seen)
    assert len(upstream.cookies.jar) == 0, "프록시 클라이언트가 upstream 쿠키를 기억했다"


async def test_cookies_cross_the_proxy_in_neither_direction():
    provider = SessionProvider()
    async with compat_app_with(provider) as client:
        response = await client.post(
            "/openai-compat/v1/chat/completions",
            json={},
            headers={**IDENTITY, "cookie": "session=CHOSEN-BY-AGENT"},
        )

    assert response.status_code == 200
    assert "set-cookie" not in response.headers, "upstream 의 세션이 에이전트에게 건너갔다"
    assert response.headers["request-id"] == "req-1", "쿠키만 떼고 나머지 응답 헤더는 돌려준다"
    assert "cookie" not in provider.seen[0].headers


def compat_app_with(provider) -> httpx.AsyncClient:
    return compat_app(Registry(), provider)


AGENT_CHOSEN_HEADERS = {
    "openai-organization": "org-of-the-agents-choice",
    "openai-project": "proj-x",
    "api-key": "agent-supplied",
    "x-goog-api-key": "agent-supplied",
    "x-forwarded-for": "10.0.0.9",
    "x-stainless-lang": "python",
    "x-anything-new": "1",
    "anthropic-version": "2023-06-01",
}


async def test_only_listed_request_headers_reach_the_openai_compat_upstream():
    """허용 목록이다 — 에이전트가 고른 헤더가 운영자의 키와 함께 upstream 에 닿으면 안 된다."""
    provider = Provider()
    async with compat_app(Registry(), provider) as client:
        await client.post(
            "/openai-compat/v1/chat/completions",
            content=b'{"model": "m"}',
            headers={
                **IDENTITY,
                **AGENT_CHOSEN_HEADERS,
                "content-type": "application/json",
                "accept": "application/json",
                "accept-encoding": "gzip",
                "user-agent": "agentd-test",
            },
        )

    [sent] = provider.seen
    assert set(sent.headers) == {
        # host · connection · content-length 는 프록시의 전송 계층이 붙인다
        "host",
        "connection",
        "content-length",
        "content-type",
        "accept",
        "accept-encoding",
        "user-agent",
        "authorization",
    }
    assert sent.headers["authorization"] == f"Bearer {COMPAT_KEY}"
    # 길이는 전달한 본문에서 다시 계산된다
    assert sent.headers["content-length"] == str(len(b'{"model": "m"}'))
    assert sent.content == b'{"model": "m"}'


async def test_anthropic_keeps_its_protocol_headers_and_drops_the_rest():
    """SDK 가 보내는 헤더 가운데 API 가 읽는 것만 — ``x-stainless-*`` 통계는 보내지 않는다."""
    provider = Provider()
    async with app_for(Registry(), provider) as client:
        response = await call(
            client,
            **{
                "anthropic-beta": "tools-2099-01-01",
                "x-stainless-lang": "python",
                "x-stainless-retry-count": "0",
                "openai-organization": "org-x",
                "cookie": "a=b",
            },
        )

    assert response.status_code == 200
    [sent] = provider.seen
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert sent.headers["anthropic-beta"] == "tools-2099-01-01"
    assert sent.headers["x-api-key"] == KEY
    leaked = {"x-stainless-lang", "x-stainless-retry-count", "openai-organization", "cookie"}
    assert leaked.isdisjoint(sent.headers)


def test_an_upstream_passes_only_the_common_headers_unless_it_declares_more():
    bare = Upstream(base_url="https://x.example", logical_host="x.example", api_key="k")

    assert bare.request_headers == {"content-type", "accept", "accept-encoding", "user-agent"}
    assert ANTHROPIC_REQUEST_HEADERS - bare.request_headers == {
        "anthropic-version",
        "anthropic-beta",
    }
