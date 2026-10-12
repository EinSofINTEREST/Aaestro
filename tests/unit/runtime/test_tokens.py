"""Unit tests for per-agent Control API tokens.

핵심 계약은 **발급처와 사용처가 같은 값을 본다** 는 것이다 — 어긋나면
컨테이너는 떴는데 모든 호출이 401 이 된다.
"""

from __future__ import annotations

import json

import pytest

from aaestro.observability.logging import mask_secrets
from aaestro.runtime.tokens import (
    AGENT_TOKEN_ENV,
    TokenIssuer,
    authenticated_env,
    generate_token,
)

# --- 발급 ---------------------------------------------------------------------


def test_tokens_are_unpredictable():
    """추측 가능한 토큰은 인증이 아니다."""
    tokens = {generate_token() for _ in range(50)}

    assert len(tokens) == 50
    assert all(len(t) >= 32 for t in tokens)


def test_same_agent_gets_a_stable_token():
    """두 번 발급하면 이미 기동한 컨테이너의 토큰과 어긋난다."""
    issuer = TokenIssuer()

    assert issuer.issue("researcher") == issuer.issue("researcher")


def test_different_agents_get_different_tokens():
    """한 에이전트의 토큰으로 다른 에이전트를 호출할 수 없어야 한다."""
    issuer = TokenIssuer()

    assert issuer.issue("planner") != issuer.issue("writer")


def test_rotate_replaces_the_token():
    issuer = TokenIssuer()
    original = issuer.issue("researcher")

    rotated = issuer.rotate("researcher")

    assert rotated != original
    assert issuer.issue("researcher") == rotated


def test_known_does_not_mint():
    """조회가 발급 부수효과를 내면 의도치 않은 토큰이 생긴다."""
    issuer = TokenIssuer()

    assert issuer.known("absent") is None
    assert issuer.known("absent") is None


def test_forget_drops_the_token():
    """정리된 에이전트의 죽은 토큰을 들고 있지 않는다."""
    issuer = TokenIssuer()
    first = issuer.issue("researcher")

    issuer.forget("researcher")

    assert issuer.known("researcher") is None
    assert issuer.issue("researcher") != first


# --- 환경 주입 ----------------------------------------------------------------


def test_env_carries_the_issued_token():
    issuer = TokenIssuer()

    env = issuer.env_for("researcher")

    assert env == {AGENT_TOKEN_ENV: issuer.issue("researcher")}


def test_declared_secrets_are_preserved():
    issuer = TokenIssuer()

    env = authenticated_env(issuer, "researcher", {"ANTHROPIC_API_KEY": "sk-x"})

    assert env["ANTHROPIC_API_KEY"] == "sk-x"
    assert env[AGENT_TOKEN_ENV] == issuer.issue("researcher")


def test_runtime_token_wins_over_a_declared_collision():
    """운영자가 같은 키를 선언해도 runtime 값이 이겨야 한다 —
    컨테이너가 runtime 이 모르는 토큰으로 뜨면 모든 호출이 401 이다."""
    issuer = TokenIssuer()

    env = authenticated_env(issuer, "researcher", {AGENT_TOKEN_ENV: "operator-value"})

    assert env[AGENT_TOKEN_ENV] == issuer.issue("researcher")


def test_empty_declared_env_is_fine():
    issuer = TokenIssuer()

    assert set(authenticated_env(issuer, "a")) == {AGENT_TOKEN_ENV}


# --- 유출 방지 ----------------------------------------------------------------


@pytest.mark.parametrize("payload_key", [AGENT_TOKEN_ENV, "token", "agent_token"])
def test_token_is_masked_in_logs(payload_key):
    """토큰이 로그에 남으면 로그 접근권이 곧 에이전트 접근권이 된다."""
    issuer = TokenIssuer()
    token = issuer.issue("researcher")

    masked = mask_secrets(None, "info", {payload_key: token, "agent": "researcher"})

    assert token not in json.dumps(masked)
    assert masked["agent"] == "researcher"


def test_token_is_masked_inside_nested_environment():
    issuer = TokenIssuer()
    token = issuer.issue("researcher")

    masked = mask_secrets(None, "info", {"environment": {AGENT_TOKEN_ENV: token}})

    assert token not in json.dumps(masked)


# --- 계약 결합 ----------------------------------------------------------------


def test_container_env_and_caller_token_match():
    """발급처와 사용처가 어긋나면 컨테이너는 떴는데 호출이 전부 막힌다."""
    import yaml

    from aaestro.core.manifest import AgentManifest
    from aaestro.runtime.spec import build_container_spec
    from tests.unit.cli.test_main import REPO_ROOT

    issuer = TokenIssuer()
    manifest = AgentManifest.model_validate(
        yaml.safe_load((REPO_ROOT / "agents" / "echo" / "manifest.yaml").read_text("utf-8"))
    )

    spec = build_container_spec(manifest, env=authenticated_env(issuer, manifest.name))
    injected = spec.to_docker_kwargs()["environment"][AGENT_TOKEN_ENV]

    assert injected == issuer.issue(manifest.name)
