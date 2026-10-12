"""Model API termination with key injection (#293)."""

from __future__ import annotations

import httpx
import pytest

from aaestro.egress.connect import EgressMode
from aaestro.egress.providers import ANTHROPIC_ENDPOINTS, Upstream, create_provider_app
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
