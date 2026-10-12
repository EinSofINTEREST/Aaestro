"""Egress proxy process settings (#293)."""

from __future__ import annotations

import asyncio

import pytest

from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.egress.__main__ import settings, supervise
from aaestro.egress.connect import EgressMode

REGISTRY = {"AAESTRO_ACCESS_URL": "http://control-plane:8700", "AAESTRO_ACCESS_ENFORCER_TOKEN": "e"}


def test_the_proxy_does_not_start_without_the_registry():
    """판정 없이 뜨는 이그레스 프록시는 열린 문이다."""
    with pytest.raises(AaestroError) as exc_info:
        settings({"AAESTRO_ACCESS_URL": "http://control-plane:8700"})

    assert exc_info.value.code == ErrorCode.CFG_001


def test_enforce_is_the_default_and_an_unknown_mode_is_refused():
    assert settings(REGISTRY)["mode"] is EgressMode.ENFORCE
    assert settings({**REGISTRY, "AAESTRO_EGRESS_MODE": "record"})["mode"] is EgressMode.RECORD
    with pytest.raises(AaestroError):
        settings({**REGISTRY, "AAESTRO_EGRESS_MODE": "off"})


def test_private_destinations_are_an_explicit_list():
    found = settings(
        {**REGISTRY, "AAESTRO_EGRESS_PRIVATE_DESTINATIONS": " fake-provider:8000, ,lab:9"}
    )

    assert found["private_destinations"] == frozenset({"fake-provider:8000", "lab:9"})
    assert settings(REGISTRY)["private_destinations"] == frozenset()


@pytest.mark.parametrize("key", ["AAESTRO_EGRESS_PORT", "AAESTRO_EGRESS_PROVIDER_PORT"])
@pytest.mark.parametrize("raw", ["http", "0", "65536", "-1"])
def test_a_listener_port_outside_1_to_65535_is_a_config_error(key, raw):
    with pytest.raises(AaestroError) as exc_info:
        settings({**REGISTRY, key: raw})

    assert (exc_info.value.code, exc_info.value.details["settings"]) == (ErrorCode.CFG_001, [key])


def test_listener_ports_default_and_accept_the_bounds():
    assert (settings(REGISTRY)["connect_port"], settings(REGISTRY)["provider_port"]) == (8080, 8081)
    assert settings({**REGISTRY, "AAESTRO_EGRESS_PORT": "65535"})["connect_port"] == 65535
    assert settings({**REGISTRY, "AAESTRO_EGRESS_PROVIDER_PORT": "1"})["provider_port"] == 1


def test_a_plaintext_provider_upstream_needs_the_switch_and_a_private_listing():
    """키를 실어 보내는 곳이다 — http 는 사설로 명시한 대역에, 스위치를 켰을 때만 (#302)."""
    plain = {**REGISTRY, "AAESTRO_EGRESS_ANTHROPIC_UPSTREAM": "http://fake-provider:8000"}
    listed = {**plain, "AAESTRO_EGRESS_PRIVATE_DESTINATIONS": "fake-provider:8000"}
    switch = {"AAESTRO_EGRESS_ALLOW_PLAINTEXT_UPSTREAM": "true"}

    for refused in (
        plain,
        listed,
        {**plain, **switch},
        {**listed, "AAESTRO_EGRESS_ALLOW_PLAINTEXT_UPSTREAM": "yes"},
    ):
        with pytest.raises(AaestroError) as exc_info:
            settings(refused)
        assert exc_info.value.code == ErrorCode.CFG_001

    allowed = settings({**listed, **switch})
    assert allowed["anthropic_upstream"] == "http://fake-provider:8000"
    assert settings(REGISTRY)["anthropic_upstream"] == "https://api.anthropic.com"


def test_a_public_provider_host_is_never_plaintext_whatever_the_settings():
    """운영 설정에 스위치가 섞여 들어가도 공인 호스트로 평문 키가 나가지 않는다 (#302)."""
    with pytest.raises(AaestroError):
        settings(
            {
                **REGISTRY,
                "AAESTRO_EGRESS_ANTHROPIC_UPSTREAM": "http://api.anthropic.com",
                "AAESTRO_EGRESS_ALLOW_PLAINTEXT_UPSTREAM": "true",
                "AAESTRO_EGRESS_PRIVATE_DESTINATIONS": "fake-provider:8000",
            }
        )


@pytest.mark.parametrize(
    "upstream",
    [
        "ftp://api.anthropic.com",
        "https://",
        "https://user:pw@api.anthropic.com",
        "https://api.anthropic.com?x=1",
        "https://api.anthropic.com#frag",
        "https://api.anthropic.com:0",
        "https://api.anthropic.com:99999",
    ],
)
def test_a_malformed_provider_upstream_is_a_config_error(upstream):
    with pytest.raises(AaestroError) as exc_info:
        settings({**REGISTRY, "AAESTRO_EGRESS_ANTHROPIC_UPSTREAM": upstream})

    assert exc_info.value.code == ErrorCode.CFG_001


async def test_when_one_part_ends_the_others_are_cancelled():
    cancelled = []

    async def forever(name):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(name)
            raise

    async def ends():
        return None

    await asyncio.wait_for(supervise(forever("connect"), forever("feed"), ends()), timeout=2)

    assert sorted(cancelled) == ["connect", "feed"]


async def test_a_failing_part_stops_the_rest_and_its_error_propagates():
    cancelled = []

    async def forever():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append("listener")
            raise

    async def feed_breaks():
        raise RuntimeError("feed broke")

    with pytest.raises(RuntimeError, match="feed broke"):
        await asyncio.wait_for(supervise(forever(), feed_breaks()), timeout=2)
    assert cancelled == ["listener"]


def test_remote_mcp_needs_the_declarations_and_an_explicit_credential_list():
    from aaestro.egress.__main__ import _mcp_routers

    bare = settings(REGISTRY)
    assert (bare["repo_root"], bare["mcp_tokens"]) == ("", frozenset())
    assert _mcp_routers(bare, access=None) == [], "선언 없이 원격 MCP 를 종단하지 않는다"

    found = settings(
        {**REGISTRY, "AAESTRO_REPO_ROOT": "/repo", "AAESTRO_EGRESS_MCP_TOKENS": " CORP_TOKEN, ,X"}
    )
    assert found["mcp_tokens"] == frozenset({"CORP_TOKEN", "X"})
    assert len(_mcp_routers(found, access=None)) == 1


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "AAESTRO_ACCESS_ENFORCER_TOKEN"])
def test_proxy_held_secrets_cannot_be_listed_for_mcp_servers(name):
    """목록이 유일한 문이다 — 모델 키·레지스트리 자격을 넣으면 선언 하나로 외부로 나간다."""
    with pytest.raises(AaestroError) as exc_info:
        settings({**REGISTRY, "AAESTRO_EGRESS_MCP_TOKENS": f"CORP_TOKEN,{name}"})

    assert exc_info.value.code == ErrorCode.CFG_001


def test_the_session_key_survives_a_restart_but_differs_per_deployment_secret():
    from aaestro.egress.__main__ import session_key

    assert session_key("enforcer") == session_key("enforcer")
    assert session_key("enforcer") != session_key("other")
