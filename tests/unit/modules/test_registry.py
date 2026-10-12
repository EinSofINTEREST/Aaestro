"""Unit tests for module reference resolution."""

from __future__ import annotations

import pytest

from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.modules.registry import ModuleRegistry, RegistryRoots
from tests.fixtures.registry import FIXTURE_ROOT, fixture_registry


@pytest.fixture
def registry() -> ModuleRegistry:
    return fixture_registry()


@pytest.mark.parametrize(
    ("ref", "expected_type", "expected_name", "expected_version"),
    [
        ("skillsets/web-search@0.2.0", "skillsets", "web-search", "0.2.0"),
        ("promptsets/researcher@0.1.0", "promptsets", "researcher", "0.1.0"),
        ("memorysets/agent-longterm@0.1.0", "memorysets", "agent-longterm", "0.1.0"),
    ],
)
def test_parse_ref_valid(registry, ref, expected_type, expected_name, expected_version):
    parsed = registry.parse(ref)

    assert (parsed.type, parsed.name, parsed.version) == (
        expected_type,
        expected_name,
        expected_version,
    )


@pytest.mark.parametrize(
    "ref",
    ["web-search", "skillsets/x@latest", "skillsets/@1.0.0", "skillsets/x@main"],
)
def test_parse_ref_invalid_raises_mod_001(registry, ref):
    with pytest.raises(AaestroError) as exc_info:
        registry.parse(ref)

    assert exc_info.value.code == "MOD_001"
    assert exc_info.value.category is ErrorCategory.MODULE


def test_resolve_locates_versioned_directory(registry):
    path = registry.resolve("skillsets/web-search@0.2.0")

    assert path.manifest_file.name == "skillset.yaml"
    assert path.root == FIXTURE_ROOT / "modules" / "skillsets" / "web-search" / "0.2.0"


def test_resolve_missing_module_raises_mod_001(registry):
    with pytest.raises(AaestroError) as exc_info:
        registry.resolve("skillsets/nonexistent@1.0.0")

    assert exc_info.value.code == "MOD_001"
    assert "expected_path" in exc_info.value.details


def test_resolve_missing_version_raises_mod_001(registry):
    """게시되지 않은 버전은 해석되지 않는다 — 버전 고정의 실효성."""
    with pytest.raises(AaestroError) as exc_info:
        registry.resolve("skillsets/web-search@9.9.9")

    assert exc_info.value.code == "MOD_001"


def test_unknown_module_type_root_raises_mod_001():
    roots = RegistryRoots.under(FIXTURE_ROOT)

    with pytest.raises(AaestroError) as exc_info:
        roots.for_type("widgets")

    assert exc_info.value.code == "MOD_001"


def test_load_document_returns_mapping(registry):
    path, document = registry.load_document("skillsets/web-search@0.2.0")

    assert document["kind"] == "Skillset"
    assert path.version == "0.2.0"


def test_kind_mismatch_raises_mod_003(registry, tmp_path):
    module = tmp_path / "modules" / "skillsets" / "broken" / "0.1.0"
    module.mkdir(parents=True)
    (module / "skillset.yaml").write_text(
        "apiVersion: aaestro/v1\nkind: Promptset\nmetadata:\n  name: broken\n  version: 0.1.0\n",
        encoding="utf-8",
    )

    with pytest.raises(AaestroError) as exc_info:
        ModuleRegistry.under(tmp_path).load_document("skillsets/broken@0.1.0")

    assert exc_info.value.code == "MOD_003"
    assert "kind mismatch" in exc_info.value.message


def test_name_mismatch_raises_mod_003(tmp_path):
    module = tmp_path / "modules" / "skillsets" / "declared" / "0.1.0"
    module.mkdir(parents=True)
    (module / "skillset.yaml").write_text(
        "apiVersion: aaestro/v1\nkind: Skillset\nmetadata:\n  name: other\n  version: 0.1.0\n",
        encoding="utf-8",
    )

    with pytest.raises(AaestroError) as exc_info:
        ModuleRegistry.under(tmp_path).load_document("skillsets/declared@0.1.0")

    assert exc_info.value.code == "MOD_003"
    assert "name mismatch" in exc_info.value.message


def test_version_mismatch_raises_mod_003(tmp_path):
    module = tmp_path / "modules" / "skillsets" / "drifted" / "0.1.0"
    module.mkdir(parents=True)
    (module / "skillset.yaml").write_text(
        "apiVersion: aaestro/v1\nkind: Skillset\nmetadata:\n  name: drifted\n  version: 0.2.0\n",
        encoding="utf-8",
    )

    with pytest.raises(AaestroError) as exc_info:
        ModuleRegistry.under(tmp_path).load_document("skillsets/drifted@0.1.0")

    assert exc_info.value.code == "MOD_003"
    assert "version mismatch" in exc_info.value.message


def test_missing_metadata_raises_mod_003(tmp_path):
    module = tmp_path / "modules" / "skillsets" / "bare" / "0.1.0"
    module.mkdir(parents=True)
    (module / "skillset.yaml").write_text(
        "apiVersion: aaestro/v1\nkind: Skillset\n", encoding="utf-8"
    )

    with pytest.raises(AaestroError) as exc_info:
        ModuleRegistry.under(tmp_path).load_document("skillsets/bare@0.1.0")

    assert exc_info.value.code == "MOD_003"


def test_non_mapping_document_raises_mod_003(tmp_path):
    module = tmp_path / "modules" / "skillsets" / "listy" / "0.1.0"
    module.mkdir(parents=True)
    (module / "skillset.yaml").write_text("- just\n- a list\n", encoding="utf-8")

    with pytest.raises(AaestroError) as exc_info:
        ModuleRegistry.under(tmp_path).load_document("skillsets/listy@0.1.0")

    assert exc_info.value.code == "MOD_003"


def test_malformed_yaml_raises_mod_003(tmp_path):
    module = tmp_path / "modules" / "skillsets" / "invalid" / "0.1.0"
    module.mkdir(parents=True)
    (module / "skillset.yaml").write_text("kind: [unclosed\n", encoding="utf-8")

    with pytest.raises(AaestroError) as exc_info:
        ModuleRegistry.under(tmp_path).load_document("skillsets/invalid@0.1.0")

    assert exc_info.value.code == "MOD_003"


def test_agent_and_graph_refs_use_their_own_layout(tmp_path):
    (tmp_path / "agents" / "planner").mkdir(parents=True)
    (tmp_path / "agents" / "planner" / "manifest.yaml").write_text(
        "apiVersion: aaestro/v1\nkind: Agent\nmetadata:\n  name: planner\n  version: 0.1.0\n",
        encoding="utf-8",
    )
    (tmp_path / "graphs").mkdir(parents=True)
    (tmp_path / "graphs" / "pipeline.yaml").write_text(
        "apiVersion: aaestro/v1\nkind: Graph\nmetadata:\n  name: pipeline\n  version: 1.0.0\n",
        encoding="utf-8",
    )
    registry = ModuleRegistry.under(tmp_path)

    agent_path, _ = registry.load_document("agents/planner@0.1.0")
    graph_path, _ = registry.load_document("graphs/pipeline@1.0.0")

    assert agent_path.manifest_file.name == "manifest.yaml"
    assert graph_path.manifest_file.name == "pipeline.yaml"
