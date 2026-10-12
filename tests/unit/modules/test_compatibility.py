"""Unit tests for cross-module compatibility rules."""

from __future__ import annotations

import pytest

from aaestro.core.errors import AaestroError
from aaestro.modules.compatibility import (
    check_promptset_templates,
    check_skillset_env,
    check_tool_namespaces,
)
from aaestro.modules.promptset import PromptsetLoader
from aaestro.modules.skillset import SkillsetLoader
from tests.fixtures.builders import make_manifest
from tests.fixtures.registry import fixture_registry


@pytest.fixture
def skillset():
    return SkillsetLoader(fixture_registry()).load("skillsets/web-search@0.2.0")


@pytest.fixture
def promptset():
    return PromptsetLoader(fixture_registry()).load("promptsets/researcher@0.1.0")


def _manifest(**runtime):
    return make_manifest(
        spec={
            "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
            "promptset": {"ref": "promptsets/researcher@0.1.0"},
            "runtime": runtime,
        }
    )


def test_env_subset_passes(skillset):
    check_skillset_env(_manifest(env_allowlist=["SEARCH_API_KEY"]), skillset)


def test_missing_env_raises_mod_002(skillset):
    """skillset requires.env ⊆ agent env_allowlist — 04 Compatibility Rules 2."""
    with pytest.raises(AaestroError) as exc_info:
        check_skillset_env(_manifest(env_allowlist=[]), skillset)

    assert exc_info.value.code == "MOD_002"
    assert exc_info.value.details["missing_env"] == ["SEARCH_API_KEY"]


def test_templates_cover_wired_nodes(promptset):
    check_promptset_templates(_manifest(), promptset, ["research", "summarize"])


def test_missing_node_template_raises_mod_002(promptset):
    """agentd 가 node_id 로 템플릿을 고르므로 누락은 런타임 실패가 된다."""
    with pytest.raises(AaestroError) as exc_info:
        check_promptset_templates(_manifest(), promptset, ["research", "planning"])

    assert exc_info.value.code == "MOD_002"
    assert exc_info.value.details["missing_templates"] == ["planning"]


def test_missing_default_template_raises_mod_002(tmp_path):
    from aaestro.modules.registry import ModuleRegistry

    module = tmp_path / "modules" / "promptsets" / "nodefault" / "0.1.0"
    (module / "templates").mkdir(parents=True)
    (module / "promptset.yaml").write_text(
        "apiVersion: aaestro/v1\nkind: Promptset\nmetadata:\n"
        "  name: nodefault\n  version: 0.1.0\nspec:\n  templates:\n"
        "    research:\n      file: templates/a.j2\n",
        encoding="utf-8",
    )
    (module / "templates" / "a.j2").write_text("x\n", encoding="utf-8")
    promptset = PromptsetLoader(ModuleRegistry.under(tmp_path)).load("promptsets/nodefault@0.1.0")

    with pytest.raises(AaestroError) as exc_info:
        check_promptset_templates(_manifest(), promptset, ["research"])

    assert exc_info.value.code == "MOD_002"
    assert "default" in exc_info.value.message


def test_agent_not_serving_direct_requests_needs_no_default(tmp_path):
    from aaestro.modules.registry import ModuleRegistry

    module = tmp_path / "modules" / "promptsets" / "nodefault" / "0.1.0"
    (module / "templates").mkdir(parents=True)
    (module / "promptset.yaml").write_text(
        "apiVersion: aaestro/v1\nkind: Promptset\nmetadata:\n"
        "  name: nodefault\n  version: 0.1.0\nspec:\n  templates:\n"
        "    research:\n      file: templates/a.j2\n",
        encoding="utf-8",
    )
    (module / "templates" / "a.j2").write_text("x\n", encoding="utf-8")
    promptset = PromptsetLoader(ModuleRegistry.under(tmp_path)).load("promptsets/nodefault@0.1.0")

    check_promptset_templates(_manifest(), promptset, ["research"], accepts_direct=False)


def test_distinct_tool_names_pass(skillset):
    check_tool_namespaces([skillset], ["mcp__fs__read_file"])


def test_duplicate_tool_across_skillsets_raises_mod_002(skillset):
    with pytest.raises(AaestroError) as exc_info:
        check_tool_namespaces([skillset, skillset])

    assert exc_info.value.code == "MOD_002"
    assert exc_info.value.details["tool"] == "search"


def test_mcp_tool_colliding_with_skill_raises_mod_002(skillset):
    with pytest.raises(AaestroError) as exc_info:
        check_tool_namespaces([skillset], ["search"])

    assert exc_info.value.code == "MOD_002"
