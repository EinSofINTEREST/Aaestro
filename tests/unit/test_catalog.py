"""The one place that reads what the repository declares.

조회가 CLI 에만 묶여 있으면 UI 는 "무엇으로 조립할 수 있는가" 를 알 길이 없고,
CLI 만 고치고 API 를 빠뜨리는 일이 생긴다 (#240). 목록은 깨진 선언을 **이름을 대며**
따로 보고한다 — 하나 때문에 전체가 500 이 되면 어느 파일인지 모른다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aaestro.catalog import MODULE_TYPES, Catalog
from aaestro.core.errors import AaestroError, ErrorCode

REPO_ROOT = Path(__file__).resolve().parents[2]


def write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")


def agent(name: str, version: str = "0.1.0") -> dict:
    return {
        "apiVersion": "aaestro/v1",
        "kind": "Agent",
        "metadata": {"name": name, "version": version},
        "spec": {
            "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
            "promptset": {"ref": "promptsets/solo@0.1.0"},
        },
    }


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    write(tmp_path / "agents" / "alpha" / "manifest.yaml", agent("alpha"))
    write(tmp_path / "agents" / "beta" / "manifest.yaml", agent("beta", "0.2.0"))
    write(
        tmp_path / "modules" / "promptsets" / "solo" / "0.1.0" / "promptset.yaml",
        {
            "apiVersion": "aaestro/v1",
            "kind": "Promptset",
            "metadata": {"name": "solo", "version": "0.1.0"},
            "spec": {"engine": "jinja2", "templates": {"default": {"file": "t.j2"}}},
        },
    )
    (tmp_path / "graphs").mkdir()
    (tmp_path / "groups").mkdir()
    return tmp_path


# --- agents -------------------------------------------------------------------


def test_agents_are_keyed_by_name(workspace):
    found = Catalog.under(workspace).agents()

    assert sorted(found.items) == ["alpha", "beta"]
    assert found.items["beta"].metadata.version == "0.2.0"
    assert found.problems == ()


def test_a_single_agent_is_returned(workspace):
    assert Catalog.under(workspace).agent("alpha").name == "alpha"


def test_an_unknown_agent_is_not_found(workspace):
    with pytest.raises(AaestroError) as excinfo:
        Catalog.under(workspace).agent("nope")

    assert excinfo.value.code == ErrorCode.NF_001


# --- 깨진 선언 -----------------------------------------------------------------


def test_a_broken_declaration_is_reported_not_dropped(workspace):
    """조용히 빼면 운영자가 빠진 줄 모르고, 예외로 올리면 전체가 사라진다."""
    write(
        workspace / "agents" / "gamma" / "manifest.yaml",
        {"apiVersion": "aaestro/v1", "kind": "Agent"},
    )

    found = Catalog.under(workspace).agents()

    assert sorted(found.items) == ["alpha", "beta"], "멀쩡한 것은 그대로 나와야 한다"
    assert len(found.problems) == 1
    assert found.problems[0].path.endswith("agents/gamma/manifest.yaml")
    assert found.problems[0].code == ErrorCode.VAL_002


def test_a_broken_single_lookup_names_the_field(workspace):
    """어느 파일의 어느 필드가 왜 인지 — 500 하나로 뭉개지 않는다."""
    write(
        workspace / "agents" / "gamma" / "manifest.yaml",
        {"apiVersion": "aaestro/v1", "kind": "Agent"},
    )

    with pytest.raises(AaestroError) as excinfo:
        Catalog.under(workspace).agent("gamma")

    details = excinfo.value.details
    assert details["path"].endswith("agents/gamma/manifest.yaml")
    assert any(e["field"] == "metadata" for e in details["errors"])


def test_unreadable_yaml_is_a_problem_with_its_path(workspace):
    (workspace / "agents" / "delta").mkdir()
    (workspace / "agents" / "delta" / "manifest.yaml").write_text(
        "- not: [a mapping", encoding="utf-8"
    )

    found = Catalog.under(workspace).agents()

    assert found.problems[0].code == ErrorCode.CFG_001
    assert "delta" in found.problems[0].path


# --- modules ------------------------------------------------------------------


def test_module_refs_are_recovered_from_the_directory_layout(workspace):
    assert Catalog.under(workspace).module_refs() == frozenset({"promptsets/solo@0.1.0"})


def test_a_missing_module_root_yields_nothing(tmp_path):
    (tmp_path / "agents").mkdir()
    assert Catalog.under(tmp_path).module_refs() == frozenset()
    assert Catalog.under(tmp_path).modules("skillsets").items == {}


def test_modules_group_versions_by_name(workspace):
    write(
        workspace / "modules" / "promptsets" / "solo" / "0.2.0" / "promptset.yaml",
        {
            "apiVersion": "aaestro/v1",
            "kind": "Promptset",
            "metadata": {"name": "solo", "version": "0.2.0"},
            "spec": {"engine": "jinja2", "templates": {"default": {"file": "t.j2"}}},
        },
    )

    assert Catalog.under(workspace).modules("promptsets").items == {"solo": ("0.1.0", "0.2.0")}


def test_a_module_document_is_integrity_checked(workspace):
    document = Catalog.under(workspace).module("promptsets", "solo", "0.1.0")

    assert document["metadata"] == {"name": "solo", "version": "0.1.0"}


def test_an_unknown_module_version_is_not_found(workspace):
    with pytest.raises(AaestroError) as excinfo:
        Catalog.under(workspace).module("promptsets", "solo", "9.9.9")

    assert excinfo.value.code == ErrorCode.NF_001


@pytest.mark.parametrize("module_type", ["agents", "graphs", "nonsense"])
def test_only_module_types_are_browsable_as_modules(workspace, module_type):
    """에이전트/그래프는 모듈 경로가 아니다 — 자기 엔드포인트가 있다."""
    with pytest.raises(AaestroError) as excinfo:
        Catalog.under(workspace).modules(module_type)

    assert excinfo.value.code == ErrorCode.NF_001


def test_module_types_are_the_read_only_ones():
    """04 Registry 2 — v0.1 은 세 종류만, 전부 읽기 전용."""
    assert MODULE_TYPES == ("skillsets", "promptsets", "memorysets")


# --- 설정 루트 -----------------------------------------------------------------


def test_config_roots_resolve_against_the_repo_root(tmp_path):
    """설정의 상대 경로는 작업 디렉토리가 아니라 레포 루트 기준이다."""
    from aaestro.config import RegistryRoots

    catalog = Catalog.from_config(RegistryRoots(), base=tmp_path)

    assert catalog.roots.agents == (tmp_path / "agents").resolve()
    assert catalog.roots.skillsets == (tmp_path / "modules" / "skillsets").resolve()


# --- 실제 저장소 -----------------------------------------------------------------


def test_the_real_repository_has_no_broken_declarations():
    """저장소의 선언이 전부 읽혀야 한다 — 깨진 것이 있으면 여기서 이름이 나온다."""
    catalog = Catalog.under(REPO_ROOT)

    for found in (catalog.agents(), catalog.graphs(), catalog.groups()):
        assert found.problems == (), [p.path for p in found.problems]
    assert catalog.agents().items and catalog.graphs().items and catalog.groups().items


# --- 리뷰 반영 (#247) ------------------------------------------------------------


def test_the_listing_key_is_the_location_not_the_declared_name(workspace):
    """`agents/foo/manifest.yaml` 이 `name: bar` 를 선언하면 목록 키 `bar` 로는
    단건 조회가 안 된다 — 위치가 정체성이므로 그런 선언은 깨진 것이다."""
    write(workspace / "agents" / "foo" / "manifest.yaml", agent("bar"))

    found = Catalog.under(workspace).agents()

    assert "bar" not in found.items and "foo" not in found.items
    problem = next(p for p in found.problems if p.path.endswith("agents/foo/manifest.yaml"))
    assert problem.code == ErrorCode.VAL_002
    assert "location" in problem.message


def test_a_single_lookup_rejects_a_name_mismatch_too(workspace):
    write(workspace / "agents" / "foo" / "manifest.yaml", agent("bar"))

    with pytest.raises(AaestroError) as excinfo:
        Catalog.under(workspace).agent("foo")

    assert excinfo.value.details == {
        "path": str(workspace / "agents" / "foo" / "manifest.yaml"),
        "declared": "bar",
        "located": "foo",
    }


def test_two_files_cannot_silently_overwrite_each_other(workspace):
    """같은 이름을 선언한 두 파일 — 위치 키를 쓰면 둘 다 살아남거나 둘 다 깨진다."""
    write(workspace / "agents" / "alpha2" / "manifest.yaml", agent("alpha"))

    found = Catalog.under(workspace).agents()

    assert found.items["alpha"].metadata.version == "0.1.0", "원래 alpha 가 덮어써졌다"
    assert any(p.path.endswith("alpha2/manifest.yaml") for p in found.problems)


def test_graphs_and_groups_use_the_filename_stem_as_identity(workspace):
    write(
        workspace / "groups" / "research.yaml",
        {
            "apiVersion": "aaestro/v1",
            "kind": "Group",
            "metadata": {"name": "science"},
            "spec": {},
        },
    )

    found = Catalog.under(workspace).groups()

    assert found.items == {}
    assert found.problems[0].path.endswith("groups/research.yaml")


def test_non_utf8_is_a_problem_not_a_crash(workspace):
    (workspace / "agents" / "latin").mkdir()
    (workspace / "agents" / "latin" / "manifest.yaml").write_bytes(b"name: caf\xe9\n")

    found = Catalog.under(workspace).agents()

    assert sorted(found.items) == ["alpha", "beta"]
    assert any(p.code == ErrorCode.CFG_001 and "latin" in p.path for p in found.problems)


def test_an_empty_module_directory_is_not_published(workspace):
    """디렉토리만 있고 문서가 없는 ref 를 해석 가능으로 넘기면 배포 검증은 통과하고
    로드에서야 실패한다 — 그 ref 는 게시된 것이 아니다."""
    (workspace / "modules" / "promptsets" / "ghost" / "0.1.0").mkdir(parents=True)

    catalog = Catalog.under(workspace)

    assert "promptsets/ghost@0.1.0" not in catalog.module_refs()
    listed = catalog.modules("promptsets")
    assert "ghost" not in listed.items
    assert any("ghost" in p.path and p.code == ErrorCode.MOD_001 for p in listed.problems)


def test_a_module_whose_document_contradicts_its_path_is_not_published(workspace):
    write(
        workspace / "modules" / "promptsets" / "solo" / "0.9.0" / "promptset.yaml",
        {
            "apiVersion": "aaestro/v1",
            "kind": "Promptset",
            "metadata": {"name": "solo", "version": "0.1.0"},
            "spec": {"engine": "jinja2", "templates": {"default": {"file": "t.j2"}}},
        },
    )

    listed = Catalog.under(workspace).modules("promptsets")

    assert listed.items == {"solo": ("0.1.0",)}
    assert any("0.9.0" in p.path for p in listed.problems)


def test_groups_have_their_own_root(tmp_path):
    """agents 루트를 옮겨도 groups 는 따라가지 않는다 — 명시 루트다."""
    from aaestro.modules.registry import RegistryRoots

    roots = RegistryRoots.under(tmp_path)
    moved = RegistryRoots(**{**roots.__dict__, "agents": tmp_path / "elsewhere"})

    assert Catalog(roots=moved).roots.groups == tmp_path / "groups"
