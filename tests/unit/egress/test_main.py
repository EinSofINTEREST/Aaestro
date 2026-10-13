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


@pytest.mark.parametrize(
    "name", ["ANTHROPIC_API_KEY", "OPENAI_COMPAT_API_KEY", "AAESTRO_ACCESS_ENFORCER_TOKEN"]
)
def test_proxy_held_secrets_cannot_be_listed_for_mcp_servers(name):
    """목록이 유일한 문이다 — 모델 키·레지스트리 자격을 넣으면 선언 하나로 외부로 나간다."""
    with pytest.raises(AaestroError) as exc_info:
        settings({**REGISTRY, "AAESTRO_EGRESS_MCP_TOKENS": f"CORP_TOKEN,{name}"})

    assert exc_info.value.code == ErrorCode.CFG_001


def test_the_session_key_survives_a_restart_but_differs_per_deployment_secret():
    from aaestro.egress.__main__ import session_key

    assert session_key("enforcer") == session_key("enforcer")
    assert session_key("enforcer") != session_key("other")


# --- OpenAI 호환 upstream (#361) ---------------------------------------------

COMPAT = {
    **REGISTRY,
    "AAESTRO_EGRESS_OPENAI_COMPAT_UPSTREAM": "https://api.openai.com",
    "OPENAI_COMPAT_API_KEY": "sk-compat",
}


def test_openai_compat_is_not_terminated_unless_configured():
    """기본 주소가 없다 — 설정하지 않으면 그 provider 는 프록시에 없다."""
    from aaestro.egress.__main__ import provider_upstreams

    found = settings({**REGISTRY, "ANTHROPIC_API_KEY": "sk-ant"})

    assert (found["openai_compat_upstream"], found["openai_compat_key"]) == ("", "")
    assert sorted(provider_upstreams(found)) == ["anthropic"]


def test_openai_compat_upstream_carries_the_bearer_key_under_its_logical_host():
    from aaestro.egress.__main__ import provider_upstreams
    from aaestro.egress.providers import OPENAI_COMPAT_ENDPOINTS

    [(name, upstream)] = provider_upstreams(settings(COMPAT)).items()

    assert name == "openai-compat"
    assert (upstream.base_url, upstream.api_key) == ("https://api.openai.com", "sk-compat")
    assert (upstream.key_header, upstream.key_scheme) == ("authorization", "Bearer")
    # 판정 대상은 upstream 주소가 아니라 논리 호스트다
    assert upstream.logical_host == "openai-compat.invalid"
    assert upstream.endpoints == OPENAI_COMPAT_ENDPOINTS


@pytest.mark.parametrize(
    "partial",
    [
        {"AAESTRO_EGRESS_OPENAI_COMPAT_UPSTREAM": "https://api.openai.com"},
        {"OPENAI_COMPAT_API_KEY": "sk-compat"},
    ],
)
def test_openai_compat_upstream_and_key_must_come_together(partial):
    """하나만 있으면 provider 가 조용히 빠지고, 에이전트는 호출할 때에야 404 를 본다."""
    with pytest.raises(AaestroError) as exc_info:
        settings({**REGISTRY, **partial})

    assert exc_info.value.code == ErrorCode.CFG_001
    assert "sk-compat" not in str(exc_info.value) + str(exc_info.value.details)


@pytest.mark.parametrize(
    "upstream",
    [
        "https://api.openai.com/v1",
        "https://api.openai.com/v1/",
        "https://gateway.example.com/llm/v1",
    ],
)
def test_openai_compat_upstream_ending_in_v1_is_refused_with_the_fix(upstream):
    """프록시가 v1/chat/completions 를 붙인다 — /v1 을 넣으면 /v1/v1/... 404 가 된다."""
    with pytest.raises(AaestroError) as exc_info:
        settings({**COMPAT, "AAESTRO_EGRESS_OPENAI_COMPAT_UPSTREAM": upstream})

    assert exc_info.value.code == ErrorCode.CFG_001
    assert "remove /v1" in exc_info.value.message
    assert exc_info.value.details["settings"] == ["AAESTRO_EGRESS_OPENAI_COMPAT_UPSTREAM"]


@pytest.mark.parametrize(
    "upstream",
    [
        "https://api.openai.com",
        "https://api.openai.com/",
        "https://gateway.example.com/llm",
        "https://gateway.example.com/api",
    ],
)
def test_openai_compat_upstream_with_a_path_prefix_is_accepted(upstream):
    found = settings({**COMPAT, "AAESTRO_EGRESS_OPENAI_COMPAT_UPSTREAM": upstream})

    assert found["openai_compat_upstream"] == upstream


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "OPENAI_COMPAT_API_KEY"])
@pytest.mark.parametrize("blank", [" ", "\t", "  \n"])
def test_a_blank_provider_key_is_refused(name, blank):
    """공백뿐인 키는 "없음" 이 아니라 오설정이다 — 빈 자격을 실어 보내다 401 로 드러난다."""
    with pytest.raises(AaestroError) as exc_info:
        settings({**COMPAT, "ANTHROPIC_API_KEY": "sk-ant", name: blank})

    assert exc_info.value.code == ErrorCode.CFG_001
    assert exc_info.value.details["settings"] == [name]


def test_a_provider_key_loses_its_surrounding_whitespace():
    found = settings({**COMPAT, "ANTHROPIC_API_KEY": " sk-ant\n", "OPENAI_COMPAT_API_KEY": "sk-c "})

    assert (found["anthropic_key"], found["openai_compat_key"]) == ("sk-ant", "sk-c")


def test_the_anthropic_upstream_passes_its_protocol_headers():
    from aaestro.egress.__main__ import provider_upstreams
    from aaestro.egress.providers import ANTHROPIC_REQUEST_HEADERS, COMMON_REQUEST_HEADERS

    upstreams = provider_upstreams(settings({**COMPAT, "ANTHROPIC_API_KEY": "sk-ant"}))

    assert upstreams["anthropic"].request_headers == ANTHROPIC_REQUEST_HEADERS
    assert upstreams["openai-compat"].request_headers == COMMON_REQUEST_HEADERS


def test_openai_compat_upstream_follows_the_plaintext_rule():
    """anthropic upstream 과 같은 규칙이다 — 키를 실어 보내는 곳이라 http 는 명시한 사설 대역만."""
    plain = {**COMPAT, "AAESTRO_EGRESS_OPENAI_COMPAT_UPSTREAM": "http://fake-provider:8000"}

    with pytest.raises(AaestroError) as exc_info:
        settings(plain)
    allowed = settings(
        {
            **plain,
            "AAESTRO_EGRESS_PRIVATE_DESTINATIONS": "fake-provider:8000",
            "AAESTRO_EGRESS_ALLOW_PLAINTEXT_UPSTREAM": "true",
        }
    )

    assert exc_info.value.details["settings"][0] == "AAESTRO_EGRESS_OPENAI_COMPAT_UPSTREAM"
    assert allowed["openai_compat_upstream"] == "http://fake-provider:8000"
