"""Decision provider termination through the egress proxy (#595).

agentd 의 Jev 어댑터가 실제 프록시 앱 (설정 env → ``decision_upstreams`` →
``create_provider_app``) 을 지나 fake upstream 에 닿는 경로를 본다 — 실 provider 호출 없음 (06).
"""

from __future__ import annotations

import json

import httpx
import pytest

from aaestro.access.baselines import decision_target
from aaestro.core.decision import Question, QuestionKind
from aaestro.core.errors import AaestroError
from aaestro.decision.providers.jev import JevDecisionModel
from aaestro.decision.providers.registry import ADAPTERS
from aaestro.egress.__main__ import decision_upstreams, settings
from aaestro.egress.providers import (
    DECISION_ENDPOINTS,
    EGRESS_ERROR_CODE_HEADER,
    create_provider_app,
)
from tests.unit.egress.test_connect import Registry

REGISTRY = {"AAESTRO_ACCESS_URL": "http://control-plane:8700", "AAESTRO_ACCESS_ENFORCER_TOKEN": "e"}
KEY = "jev-operator-key"
IDENTITY = "cred-researcher"
TARGET = decision_target("jev")
QUESTION = Question(name="triage.ok", kind=QuestionKind.PREDICATE, ask="It holds")


def env(providers: dict[str, object], **extra: str) -> dict[str, str]:
    return {
        **REGISTRY,
        "AAESTRO_EGRESS_DECISION_PROVIDERS": json.dumps(providers),
        "AAESTRO_EGRESS_DECISION_KEY_JEV": KEY,
        **extra,
    }


JEV = {"kind": "jev", "base_url": "https://api.typesafe.ai", "locales": ["en"]}


class Upstream:
    """Jev 대역 — 받은 요청을 남기고 확률 하나를 돌려준다."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        # 프록시는 2xx 를 스트림으로 흘린다 — 실제 전송처럼 스트림으로 답한다
        body = json.dumps({"answers": {"q0": {"noul": 0.9}}}).encode()
        return httpx.Response(
            200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(body)
        )


def proxy(registry: Registry, upstream: Upstream, environ: dict[str, str]) -> httpx.AsyncClient:
    """설정 env 로 만든 실제 프록시 앱 — agentd 쪽 HTTP 클라이언트로 돌려준다."""
    app = create_provider_app(
        registry,
        {},
        http=httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle)),
        decision_upstreams=decision_upstreams(settings(environ)),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app))


def adapter(client: httpx.AsyncClient, *, provider: str = "jev") -> JevDecisionModel:
    # agentd 가 쓰는 모양 그대로 — 주소는 프록시 창구, 자격은 신원
    return JevDecisionModel(
        base_url=f"http://egress:8081/decision/{provider}",
        model="jev-1.13",
        credential=IDENTITY,
        timeout_s=5,
        client=client,
        allow_plaintext=True,
    )


async def test_decide_through_the_proxy_swaps_the_identity_for_the_operator_key():
    registry, upstream = Registry(), Upstream()
    registry.allowed.add(TARGET)
    async with proxy(registry, upstream, env({"jev": JEV})) as client:
        [decision] = await adapter(client).decide("state", [QUESTION])

    assert decision.probability == pytest.approx(0.9)
    [sent] = upstream.seen
    assert str(sent.url) == "https://api.typesafe.ai/v1/systemone"
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    assert IDENTITY not in str(sent.headers), "신원이 provider 로 새어 나갔다"
    assert registry.asked == [(IDENTITY, TARGET)]


async def test_an_identity_in_any_identity_header_never_reaches_the_provider():
    registry, upstream = Registry(), Upstream()
    registry.allowed.add(TARGET)
    async with proxy(registry, upstream, env({"jev": JEV})) as client:
        response = await client.post(
            "http://egress/decision/jev/v1/systemone",
            json={},
            headers={"x-api-key": IDENTITY, "authorization": f"Bearer {IDENTITY}"},
        )

    assert response.status_code == 200
    [sent] = upstream.seen
    assert "x-api-key" not in sent.headers and IDENTITY not in str(sent.headers)


async def test_decide_revoked_agent_is_acc_001_without_reaching_the_provider():
    registry, upstream = Registry(), Upstream()  # TARGET 을 허용하지 않는다 — 회수된 상태
    async with proxy(registry, upstream, env({"jev": JEV})) as client:
        response = await client.post(
            "http://egress/decision/jev/v1/systemone",
            json={},
            headers={"authorization": f"Bearer {IDENTITY}"},
        )
        with pytest.raises(AaestroError) as exc:
            await adapter(client).decide("state", [QUESTION])

    assert response.status_code == 403
    assert response.headers[EGRESS_ERROR_CODE_HEADER] == "ACC_001"
    assert exc.value.code == "DEC_001" and exc.value.retryable is False
    assert upstream.seen == []


async def test_decide_registry_down_is_acc_002_and_retryable_for_the_adapter():
    registry, upstream = Registry(), Upstream()
    registry.down = True
    async with proxy(registry, upstream, env({"jev": JEV})) as client:
        response = await client.post(
            "http://egress/decision/jev/v1/systemone",
            json={},
            headers={"authorization": f"Bearer {IDENTITY}"},
        )
        with pytest.raises(AaestroError) as exc:
            await adapter(client).decide("state", [QUESTION])

    assert response.status_code == 503
    assert response.headers[EGRESS_ERROR_CODE_HEADER] == "ACC_002"
    assert exc.value.retryable is True
    assert upstream.seen == []


@pytest.mark.parametrize(
    ("path", "status"),
    [("/decision/jev/v1/other", 403), ("/decision/posek/v1/systemone", 404)],
)
async def test_only_the_adapter_endpoint_of_a_configured_provider_is_open(path, status):
    registry, upstream = Registry(), Upstream()
    registry.allowed.add(TARGET)
    async with proxy(registry, upstream, env({"jev": JEV})) as client:
        response = await client.post(
            f"http://egress{path}", json={}, headers={"authorization": f"Bearer {IDENTITY}"}
        )

    assert response.status_code == status and upstream.seen == []


def test_decision_providers_are_not_terminated_unless_configured():
    assert decision_upstreams(settings(REGISTRY)) == {}


def test_a_decision_provider_without_its_key_refuses_to_start():
    environ = env({"jev": JEV})
    del environ["AAESTRO_EGRESS_DECISION_KEY_JEV"]
    with pytest.raises(AaestroError) as exc:
        settings(environ)
    assert exc.value.code == "CFG_001"
    assert "AAESTRO_EGRESS_DECISION_KEY_JEV" in exc.value.details["settings"]


def test_a_malformed_decision_providers_setting_does_not_echo_the_value():
    with pytest.raises(AaestroError) as exc:
        settings(env({"jev": {**JEV, "base_url": "https://user:secret@api.typesafe.ai"}}))
    assert exc.value.code == "CFG_001"
    assert "secret" not in str(exc.value) and exc.value.__cause__ is None


PLAIN = {"kind": "jev", "base_url": "http://posek.internal:8000", "locales": ["en"]}
LOOPBACK = {"kind": "jev", "base_url": "http://127.0.0.1:8000", "locales": ["en"]}


@pytest.mark.parametrize(
    ("declared", "private"),
    [
        ({**PLAIN, "allow_plaintext": True}, ""),  # opt-in 만
        (PLAIN, "posek.internal:8000"),  # 사설 명시만 — 설정 모델이 opt-in 없는 http 를 거절한다
        ({**LOOPBACK, "allow_plaintext": True}, ""),  # 루프백도 사설 명시가 필요하다
        (LOOPBACK, "127.0.0.1:8000"),  # 루프백도 opt-in 이 필요하다
    ],
)
def test_plaintext_decision_upstream_needs_opt_in_and_a_private_listing(declared, private):
    environ = env({"jev": declared}, AAESTRO_EGRESS_PRIVATE_DESTINATIONS=private)
    with pytest.raises(AaestroError) as exc:
        settings(environ)
    assert exc.value.code == "CFG_001"


@pytest.mark.parametrize(
    ("declared", "private"),
    [
        ({**PLAIN, "allow_plaintext": True}, "posek.internal:8000"),
        ({**LOOPBACK, "allow_plaintext": True}, "127.0.0.1:8000"),
    ],
)
def test_plaintext_decision_upstream_with_opt_in_and_private_listing_is_accepted(declared, private):
    environ = env({"jev": declared}, AAESTRO_EGRESS_PRIVATE_DESTINATIONS=private)
    [(name, upstream)] = decision_upstreams(settings(environ)).items()
    assert name == "jev" and upstream.base_url == declared["base_url"]
    assert upstream.logical_host == TARGET


def test_every_adapter_kind_has_proxy_endpoints():
    assert DECISION_ENDPOINTS.keys() == ADAPTERS.keys()


def test_decision_keys_cannot_be_listed_for_mcp_servers():
    with pytest.raises(AaestroError):
        settings({**REGISTRY, "AAESTRO_EGRESS_MCP_TOKENS": "AAESTRO_EGRESS_DECISION_KEY_JEV"})
