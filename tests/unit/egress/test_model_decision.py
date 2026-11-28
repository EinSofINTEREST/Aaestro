"""Model API termination is decided as ``model``, not ``egress`` (#399).

실제 레지스트리 (오케스트레이터가 꽂는 baseline 그대로) + control plane 권한 라우트 + 판정
클라이언트 + 프록시 앱을 잇는다 — ``runtime.egress`` 에 provider 호스트를 적어도 운영자 키가 붙는
종단은 열리지 않고, provider 선언이 같은 호스트의 CONNECT 터널을 열지도 않는다.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from aaestro.access.client import AccessClient
from aaestro.access.model import ResourceKind
from aaestro.catalog import Catalog
from aaestro.egress.__main__ import provider_upstreams
from aaestro.egress.connect import ConnectProxy
from aaestro.egress.providers import create_provider_app
from aaestro.orchestrator.__main__ import _access_registry
from aaestro.orchestrator.control import create_app as control_app
from aaestro.orchestrator.runstore import InMemoryRunStore
from tests.fixtures.access import STEWARD, agent, group, write
from tests.unit.egress.test_connect import basic, connect, running

ENFORCER = "enforcer-token"
KEY = "sk-operator-key"
MESSAGE = {"model": "m", "max_tokens": 1, "messages": []}


class Stack:
    def __init__(self, root: Path) -> None:
        # anthropic 을 선언하지 않고 egress 에 그 호스트만 적은 에이전트
        sneaky = agent("sneaky")
        sneaky["spec"]["model"] = {"provider": "openai-compat", "name": "gpt-5-mini"}
        sneaky["spec"]["runtime"] = {"egress": ["api.anthropic.com"]}
        write(root / "agents" / "sneaky" / "manifest.yaml", sneaky)
        write(root / "agents" / "worker" / "manifest.yaml", agent("worker"))
        # anthropic 을 선언하지 않은 그룹 멤버 — #399 전 관행대로 상한 egress 에 provider 호스트
        plain = agent("plain", "research")
        plain["spec"]["model"] = {"provider": "openai-compat", "name": "gpt-5-mini"}
        write(root / "agents" / "plain" / "manifest.yaml", plain)
        write(root / "agents" / STEWARD / "manifest.yaml", agent(STEWARD))
        ceiling = {"max_ttl_s": 600, "egress": ["api.anthropic.com"]}
        write(
            root / "groups" / "research.yaml", group("research", {"access": {"ceiling": ceiling}})
        )
        catalog = Catalog.under(root)
        settings = SimpleNamespace(access_store=root / "access.db", access_stewards=[STEWARD])
        self.registry = _access_registry(settings, catalog)
        control = control_app(
            InMemoryRunStore(),
            catalog=catalog,
            token="control-token",
            access=self.registry,
            enforcer_token=ENFORCER,
        )
        self.access = AccessClient(
            base_url="http://cp",
            enforcer_token=ENFORCER,
            component="egress",
            http=httpx.AsyncClient(
                transport=httpx.ASGITransport(app=control), base_url="http://cp"
            ),
        )
        self.upstream_seen: list[httpx.Request] = []

        def provider(request: httpx.Request) -> httpx.Response:
            self.upstream_seen.append(request)
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"))

        upstreams = provider_upstreams(
            {
                "anthropic_key": KEY,
                "anthropic_upstream": "https://api.anthropic.com",
                "openai_compat_key": "",
                "openai_compat_upstream": "",
            }
        )
        app = create_provider_app(
            self.access,
            upstreams,
            http=httpx.AsyncClient(transport=httpx.MockTransport(provider)),
        )
        self.proxy = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://egress"
        )

    async def call(self, agent_name: str) -> httpx.Response:
        credential = self.registry.issue_identity(agent_name, f"dep-{agent_name}")
        return await self.proxy.post(
            "/anthropic/v1/messages", headers={"x-api-key": credential}, json=MESSAGE
        )


@pytest.fixture
async def stack(tmp_path: Path):
    found = Stack(tmp_path)
    try:
        yield found
    finally:
        await found.access.aclose()
        await found.proxy.aclose()


async def test_an_egress_host_alone_does_not_open_the_model_termination(stack):
    answer = await stack.call("sneaky")

    assert answer.status_code == 403
    assert answer.json()["error"]["code"] == "ACC_001"
    assert stack.upstream_seen == [], "운영자 키를 붙인 호출이 나가면 안 된다"


async def test_a_declared_provider_passes_the_model_termination(stack):
    answer = await stack.call("worker")

    assert answer.status_code == 200
    [sent] = stack.upstream_seen
    assert sent.headers["x-api-key"] == KEY


async def test_a_provider_declaration_does_not_open_a_tunnel_to_its_host(stack):
    credential = stack.registry.issue_identity("worker", "dep-worker")
    opened: list[str] = []

    async def never(host, port):
        opened.append(host)
        raise AssertionError("the tunnel must be refused before resolving")

    proxy = ConnectProxy(access=stack.access, resolver=never, opener=never)
    async with running(proxy) as port:
        status, _, _, writer = await connect(
            port, "api.anthropic.com:443", basic("worker", credential)
        )
        writer.close()

    assert status == 403 and opened == []


def test_the_operator_can_revoke_a_declared_provider(stack):
    """좁히기는 운영자가 — 회수 대상은 provider 이름이다."""
    assert stack.registry.decide("worker", ResourceKind.MODEL, "anthropic").allowed
    stack.registry.revoke("worker", ResourceKind.MODEL, "anthropic", reason="test")

    assert not stack.registry.decide("worker", ResourceKind.MODEL, "anthropic").allowed


async def test_a_revocation_left_on_the_old_egress_host_still_cuts_the_model(stack):
    """업그레이드가 #399 전의 회수를 조용히 풀지 않는다 — 회수는 잇고, 부여는 잇지 않는다."""
    rule = stack.registry.revoke(
        "worker", ResourceKind.EGRESS, "api.anthropic.com", reason="before #399"
    )

    assert (await stack.call("worker")).status_code == 403

    stack.registry.lift(rule.rule_id)
    assert (await stack.call("worker")).status_code == 200


async def test_an_old_style_egress_grant_does_not_open_the_model(stack):
    """상한 egress 의 provider 호스트로 받은 부여는 터널만 연다 — 운영자 키는 아니다 (#399)."""
    steward = stack.registry.issue_identity(STEWARD, "dep-steward")
    stack.registry.grant(
        steward, "plain", ResourceKind.EGRESS, "api.anthropic.com",
        ttl_s=60, reason="r", requested_by="plain",
    )  # fmt: skip

    assert stack.registry.decide("plain", ResourceKind.EGRESS, "api.anthropic.com").allowed
    assert not stack.registry.decide("plain", ResourceKind.MODEL, "anthropic").allowed
    answer = await stack.call("plain")
    assert answer.status_code == 403
    assert answer.json()["error"]["code"] == "ACC_001"
    assert stack.upstream_seen == []
