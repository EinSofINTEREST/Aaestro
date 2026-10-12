"""Unit tests for scoped secret resolution."""

from __future__ import annotations

import pytest

from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.core.manifest import GroupManifest
from aaestro.runtime.scope import ScopedSecrets, SecretScope
from tests.fixtures.builders import make_manifest


def make_group(name: str = "research", secrets: tuple[str, ...] = ("SEARCH_API_KEY",)):
    """그룹 정의 — secrets 목록이 멤버에게 제공할 키를 결정한다."""
    return GroupManifest.model_validate(
        {
            "apiVersion": "aaestro/v1",
            "kind": "Group",
            "metadata": {"name": name, "version": "0.1.0"},
            "spec": {"secrets": list(secrets)},
        }
    )


def member_manifest(group: str | None = "research", allowlist: tuple[str, ...] = ()):
    """소속과 allowlist 만 바꿔 쓰는 에이전트 manifest."""
    metadata = {"name": "researcher", "version": "0.1.0"}
    if group is not None:
        metadata["group"] = group
    return make_manifest(
        metadata=metadata,
        spec={
            "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
            "promptset": {"ref": "promptsets/test@0.1.0"},
            "runtime": {"env_allowlist": list(allowlist)},
        },
    )


# --- 우선순위 --------------------------------------------------------------


def test_local_wins_over_group_and_global():
    """가까운 스코프가 우선한다 (shadowing 허용)."""
    secrets = ScopedSecrets(
        local={"KEY": "local"},
        group={"KEY": "group"},
        global_={"KEY": "global"},
        group_name="research",
        group_declared=frozenset({"KEY"}),
    )

    resolved = secrets.resolve("KEY")

    assert resolved.value == "local"
    assert resolved.scope is SecretScope.LOCAL


def test_group_wins_over_global():
    secrets = ScopedSecrets(
        group={"KEY": "group"},
        global_={"KEY": "global"},
        group_name="research",
        group_declared=frozenset({"KEY"}),
    )

    resolved = secrets.resolve("KEY")

    assert resolved.value == "group"
    assert resolved.scope is SecretScope.GROUP
    assert resolved.group == "research"


def test_global_is_the_last_resort():
    secrets = ScopedSecrets(global_={"KEY": "global"})

    resolved = secrets.resolve("KEY")

    assert resolved.value == "global"
    assert resolved.scope is SecretScope.GLOBAL


# --- 그룹 경계 -------------------------------------------------------------


def test_non_member_does_not_see_group_values():
    """비멤버는 같은 키를 allowlist 에 넣어도 group 값으로 해석되지 않는다."""
    secrets = ScopedSecrets(
        group={"SEARCH_API_KEY": "group-secret"},
        group_name=None,  # 소속 없음
        group_declared=frozenset({"SEARCH_API_KEY"}),
    )

    with pytest.raises(AaestroError) as exc_info:
        secrets.resolve("SEARCH_API_KEY")

    assert exc_info.value.code == "CFG_002"


def test_member_does_not_see_undeclared_group_keys():
    """그룹이 제공하기로 선언하지 않은 키는 멤버에게도 보이지 않는다."""
    secrets = ScopedSecrets(
        group={"HIDDEN": "group-secret"},
        group_name="research",
        group_declared=frozenset({"SEARCH_API_KEY"}),  # HIDDEN 미선언
    )

    with pytest.raises(AaestroError) as exc_info:
        secrets.resolve("HIDDEN")

    assert exc_info.value.code == "CFG_002"


def test_member_sees_declared_group_keys():
    secrets = ScopedSecrets.for_agent(
        member_manifest(allowlist=("SEARCH_API_KEY",)),
        groups={"research": make_group()},
        group_values={"SEARCH_API_KEY": "group-secret"},
    )

    assert secrets.resolve("SEARCH_API_KEY").scope is SecretScope.GROUP


def test_agent_without_group_falls_back_to_global():
    secrets = ScopedSecrets.for_agent(
        member_manifest(group=None, allowlist=("ANTHROPIC_API_KEY",)),
        groups={"research": make_group()},
        global_values={"ANTHROPIC_API_KEY": "global-secret"},
    )

    assert secrets.resolve("ANTHROPIC_API_KEY").scope is SecretScope.GLOBAL


def test_unknown_group_definition_yields_no_group_keys():
    secrets = ScopedSecrets.for_agent(
        member_manifest(group="research"),
        groups={},  # 정의를 못 찾음
        group_values={"SEARCH_API_KEY": "group-secret"},
    )

    with pytest.raises(AaestroError):
        secrets.resolve("SEARCH_API_KEY")


# --- 실패 처리 -------------------------------------------------------------


def test_unresolvable_key_raises_cfg_002():
    with pytest.raises(AaestroError) as exc_info:
        ScopedSecrets().resolve("MISSING")

    assert exc_info.value.category is ErrorCategory.CONFIG
    assert exc_info.value.code == "CFG_002"
    assert exc_info.value.details["key"] == "MISSING"


def test_error_never_carries_the_secret_value():
    """에러 메시지·details 에 값이 실리면 로그로 유출된다."""
    secrets = ScopedSecrets(local={"OTHER": "super-secret-value"})

    with pytest.raises(AaestroError) as exc_info:
        secrets.resolve("MISSING")

    rendered = f"{exc_info.value.message} {exc_info.value.details}"
    assert "super-secret-value" not in rendered


def test_describe_omits_the_value():
    resolved = ScopedSecrets(local={"KEY": "super-secret-value"}).resolve("KEY")

    described = resolved.describe()

    assert described == {"key": "KEY", "scope": "local"}
    assert "super-secret-value" not in str(described)


def test_describe_includes_group_for_group_scope():
    secrets = ScopedSecrets(
        group={"KEY": "v"}, group_name="research", group_declared=frozenset({"KEY"})
    )

    assert secrets.resolve("KEY").describe() == {
        "key": "KEY",
        "scope": "group",
        "group": "research",
    }


# --- 일괄 해석 -------------------------------------------------------------


def test_resolve_all_covers_every_declared_key():
    secrets = ScopedSecrets(
        local={"A": "1"},
        group={"B": "2"},
        global_={"C": "3"},
        group_name="research",
        group_declared=frozenset({"B"}),
    )

    resolved = secrets.resolve_all(("A", "B", "C"))

    assert {k: v.scope for k, v in resolved.items()} == {
        "A": SecretScope.LOCAL,
        "B": SecretScope.GROUP,
        "C": SecretScope.GLOBAL,
    }


def test_resolve_all_fails_on_the_first_unresolvable_key():
    secrets = ScopedSecrets(local={"A": "1"})

    with pytest.raises(AaestroError) as exc_info:
        secrets.resolve_all(("A", "MISSING"))

    assert exc_info.value.details["key"] == "MISSING"


def test_env_for_includes_only_declared_keys():
    """allowlist 밖의 키는 컨테이너에 주입되지 않는다."""
    secrets = ScopedSecrets(local={"DECLARED": "1", "UNDECLARED": "2"})

    assert secrets.env_for(("DECLARED",)) == {"DECLARED": "1"}


def test_env_for_empty_allowlist_is_empty():
    assert ScopedSecrets(local={"A": "1"}).env_for(()) == {}


# --- SecretsProvider 계약 ---------------------------------------------------


def test_get_returns_value_or_none():
    secrets = ScopedSecrets(local={"KEY": "value"})

    assert secrets.get("KEY") == "value"
    assert secrets.get("MISSING") is None


def test_repr_never_exposes_the_value():
    """객체를 로깅하는 것만으로 secret 이 새면 안 된다."""
    resolved = ScopedSecrets(local={"KEY": "super-secret-value"}).resolve("KEY")

    assert "super-secret-value" not in repr(resolved)
    assert "KEY" in repr(resolved)
    assert resolved.value == "super-secret-value"  # 주입 경로는 그대로 동작


def test_undeclared_key_is_rejected_even_when_present():
    """allowlist 밖의 키는 값이 있어도 해석하지 않는다."""
    secrets = ScopedSecrets(
        local={"DECLARED": "1", "UNDECLARED": "2"}, allowlist=frozenset({"DECLARED"})
    )

    assert secrets.resolve("DECLARED").value == "1"
    with pytest.raises(AaestroError) as exc_info:
        secrets.resolve("UNDECLARED")

    assert exc_info.value.code == "CFG_002"
    assert "env_allowlist" in exc_info.value.message


def test_undeclared_global_key_is_rejected():
    secrets = ScopedSecrets(global_={"LEAK": "v"}, allowlist=frozenset({"DECLARED"}))

    assert secrets.get("LEAK") is None


def test_for_agent_enforces_the_manifest_allowlist():
    """for_agent 는 manifest 의 env_allowlist 를 강제해야 한다."""
    secrets = ScopedSecrets.for_agent(
        member_manifest(group=None, allowlist=("ANTHROPIC_API_KEY",)),
        local={"ANTHROPIC_API_KEY": "ok", "OTHER_SECRET": "leak"},
    )

    assert secrets.get("ANTHROPIC_API_KEY") == "ok"
    assert secrets.get("OTHER_SECRET") is None


def test_resolver_without_allowlist_is_unrestricted():
    """저수준 사용(테스트 등)에서는 allowlist 미지정이 제한 없음을 뜻한다."""
    assert ScopedSecrets(local={"ANY": "v"}).get("ANY") == "v"
