"""Unit tests for module reference resolution."""

from __future__ import annotations

import shutil

import pytest

from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.modules.promptset import PromptsetLoader
from aaestro.modules.registry import ModuleRegistry, RegistryRoots
from aaestro.modules.skillset import SkillsetLoader
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


# --- containment: 모듈 디렉토리 안의 링크 (#408) ----------------------------------

_CONTROL = ("\x1b", "\n", "\r")


@pytest.fixture
def repo(tmp_path):
    """픽스처 모듈을 tmp 로 복사한다 — 링크를 심어도 커밋된 픽스처를 건드리지 않는다."""
    shutil.copytree(FIXTURE_ROOT / "modules", tmp_path / "repo" / "modules")
    (tmp_path / "outside.txt").write_text("outside the registry root\n", encoding="utf-8")
    return tmp_path / "repo"


def _module(repo, ref_path: str):
    return repo / "modules" / ref_path


@pytest.mark.parametrize(
    ("ref", "module_dir", "link", "target"),
    [
        # 루트 밖을 가리키는 상대 링크
        (
            "skillsets/web-search@0.2.0",
            "skillsets/web-search/0.2.0",
            "skills/leak.py",
            "../../../../../../outside.txt",
        ),
        # 나갔다 돌아오는 링크 — 최종 경로는 루트 안이다
        (
            "promptsets/researcher@0.1.0",
            "promptsets/researcher/0.1.0",
            "templates/round-trip.j2",
            "../../0.1.0/templates/default.j2",
        ),
        # 루트 밖의 이름에 기대는 형제 버전 디렉토리 참조
        (
            "memorysets/agent-longterm@0.1.0",
            "memorysets/agent-longterm/0.1.0",
            "sibling",
            "../../../promptsets/researcher/0.1.0",
        ),
        # 대상 없는 링크
        (
            "skillsets/web-search@0.2.0",
            "skillsets/web-search/0.2.0",
            "skills/dangling.py",
            "missing.py",
        ),
        # 자기 조상을 가리키는 디렉토리 링크
        (
            "promptsets/researcher@0.1.0",
            "promptsets/researcher/0.1.0",
            "templates/self",
            ".",
        ),
    ],
)
def test_resolve_link_leaving_module_root_raises_mod_003(repo, ref, module_dir, link, target):
    path = _module(repo, module_dir) / link
    path.symlink_to(target)

    with pytest.raises(AaestroError) as exc_info:
        ModuleRegistry.under(repo).resolve(ref)

    assert exc_info.value.code == "MOD_003"
    assert exc_info.value.details["module_ref"] == ref


def test_resolve_absolute_link_raises_mod_003_even_inside_root(repo):
    module = _module(repo, "promptsets/researcher/0.1.0")
    (module / "templates" / "abs.j2").symlink_to(module / "templates" / "default.j2")

    with pytest.raises(AaestroError) as exc_info:
        ModuleRegistry.under(repo).resolve("promptsets/researcher@0.1.0")

    assert exc_info.value.code == "MOD_003"
    assert "absolute" in exc_info.value.message


def test_resolve_relative_link_inside_module_root_is_allowed(repo):
    module = _module(repo, "promptsets/researcher/0.1.0")
    (module / "templates" / "alias.j2").symlink_to("default.j2")

    path = ModuleRegistry.under(repo).resolve("promptsets/researcher@0.1.0")

    assert path.root == module


def test_resolve_link_error_sanitizes_the_link_name(repo):
    module = _module(repo, "skillsets/web-search/0.2.0")
    (module / "skills" / "x\x1b[31m\nINJECTED").symlink_to("/etc/hostname")

    with pytest.raises(AaestroError) as exc_info:
        ModuleRegistry.under(repo).resolve("skillsets/web-search@0.2.0")

    assert not any(ch in str(exc_info.value) for ch in _CONTROL)


def test_skillset_loader_rejects_outside_link_before_import(repo, tmp_path):
    marker = tmp_path / "imported"
    outside = tmp_path / "evil.py"
    outside.write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
    module = _module(repo, "skillsets/web-search/0.2.0")
    (module / "skills" / "search.py").unlink()
    (module / "skills" / "search.py").symlink_to(outside)

    with pytest.raises(AaestroError) as exc_info:
        SkillsetLoader(ModuleRegistry.under(repo)).load("skillsets/web-search@0.2.0")

    assert exc_info.value.code == "MOD_003"
    assert not marker.exists(), "루트 밖 코드가 import 됐다"


def test_promptset_loader_rejects_outside_template_link(repo, tmp_path):
    module = _module(repo, "promptsets/researcher/0.1.0")
    (module / "templates" / "default.j2").unlink()
    (module / "templates" / "default.j2").symlink_to(tmp_path / "outside.txt")

    with pytest.raises(AaestroError) as exc_info:
        PromptsetLoader(ModuleRegistry.under(repo)).load("promptsets/researcher@0.1.0")

    assert exc_info.value.code == "MOD_003"


# --- sanitization: 이름이 에러에 실릴 때 (#408) ----------------------------------


def test_parse_invalid_ref_sanitizes_control_characters(registry):
    ref = "skillpacks/p@1.0.0\x1b[31m\nINJECTED LINE"

    with pytest.raises(AaestroError) as exc_info:
        registry.parse(ref)

    error = exc_info.value
    assert error.code == "MOD_001"
    for text in (error.message, error.details["module_ref"]):
        assert not any(ch in text for ch in _CONTROL)


def test_parse_ref_with_trailing_newline_is_invalid(registry):
    """``$`` 는 끝의 개행 앞에서도 맞는다 — 개행이 붙은 ref 가 다른 버전으로 해석되면 안 된다."""
    with pytest.raises(AaestroError) as exc_info:
        registry.parse("skillsets/web-search@0.2.0\n")

    assert exc_info.value.code == "MOD_001"
