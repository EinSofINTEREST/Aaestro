"""Writing declarations — nothing reaches disk without passing validation (#242)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aaestro.authoring import Author
from aaestro.catalog import Catalog
from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.core.manifest import AgentManifest
from aaestro.core.topology import GraphTopology
from aaestro.materials import InMemoryMaterialStore
from tests.fixtures.topologies import mission_dict

REPO_ROOT = Path(__file__).resolve().parents[2]


def write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def promptset(name: str = "solo", version: str = "0.1.0") -> dict:
    return {
        "apiVersion": "aaestro/v1",
        "kind": "Promptset",
        "metadata": {"name": name, "version": version},
        "spec": {
            "engine": "jinja2",
            "templates": {"default": {"file": "t.j2"}, "step": {"file": "s.j2"}},
        },
    }


def agent(
    name: str, version: str = "0.1.0", *, promptset_ref: str = "promptsets/solo@0.1.0"
) -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "apiVersion": "aaestro/v1",
            "kind": "Agent",
            "metadata": {"name": name, "version": version},
            "spec": {
                "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                "promptset": {"ref": promptset_ref},
            },
        }
    )


def graph(
    name: str, version: str = "1.0.0", *, agent_name: str = "alpha", agent_version: str = "0.1.0"
) -> GraphTopology:
    return GraphTopology.model_validate(
        {
            "apiVersion": "aaestro/v1",
            "kind": "Graph",
            "metadata": {"name": name, "version": version},
            "spec": {
                "mode": "mission",
                "goal": "test",
                "state": {"schema": "aaestro.graphs.schemas:ResearchState"},
                "nodes": [{"id": "step", "agent": f"agents/{agent_name}@{agent_version}"}],
                "edges": [{"from": "START", "to": "step"}, {"from": "step", "to": "END"}],
            },
        }
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    write(tmp_path / "modules" / "promptsets" / "solo" / "0.1.0" / "promptset.yaml", promptset())
    write(
        tmp_path / "agents" / "alpha" / "manifest.yaml",
        agent("alpha").model_dump(mode="json", by_alias=True, exclude_none=True),
    )
    # 실제 저장소의 global.yaml 을 그대로 쓴다 — 손으로 만든 최소 문서는 스키마와 어긋난다
    write(
        tmp_path / "groups" / "global.yaml",
        yaml.safe_load((REPO_ROOT / "groups" / "global.yaml").read_text(encoding="utf-8")),
    )
    (tmp_path / "graphs").mkdir()
    return tmp_path


@pytest.fixture
def author(workspace: Path) -> Author:
    return Author(catalog=Catalog.under(workspace))


# --- 검증 없이 쓰지 않는다 ------------------------------------------------------------


def test_a_valid_graph_is_written_as_readable_yaml(author, workspace):
    path = author.save_graph("pipeline", graph("pipeline"))

    assert path == workspace / "graphs" / "pipeline.yaml"
    text = path.read_text(encoding="utf-8")
    assert list(yaml.safe_load(text)) == ["apiVersion", "kind", "metadata", "spec"]
    assert Catalog.under(workspace).graph("pipeline") == graph("pipeline")


def test_a_graph_that_fails_validation_never_touches_disk(author, workspace):
    """검증 실패한 초안은 파일이 생기지 않는다 — 이것이 이 API 의 요점이다."""
    with pytest.raises(AaestroError) as excinfo:
        author.save_graph("bad", graph("bad", agent_name="nobody"))

    assert excinfo.value.code == ErrorCode.VAL_001
    assert not (workspace / "graphs" / "bad.yaml").exists()
    assert any("nobody" in str(f) for f in excinfo.value.details["findings"])


def test_validate_reports_findings_without_saving(author, workspace):
    report = author.validate(graphs=[graph("draft", agent_name="nobody")])

    assert not report.ok
    assert not (workspace / "graphs" / "draft.yaml").exists()


def test_a_draft_graph_can_reference_a_saved_agent(author):
    """저장된 것 + 초안을 함께 검증한다 — 초안 그래프가 기존 에이전트를 참조하는 것이 보통이다."""
    assert author.validate(graphs=[graph("draft")]).ok


def test_a_draft_agent_is_checked_even_when_no_graph_references_it(author):
    report = author.validate(agents=[agent("orphan", promptset_ref="promptsets/absent@9.9.9")])

    assert not report.ok


def test_a_draft_agent_that_breaks_a_saved_graph_is_rejected(author):
    """에이전트 초안이 기존 그래프를 깨뜨릴 수 있다 — 함께 본다."""
    author.save_graph("pipeline", graph("pipeline"))

    report = author.validate(agents=[agent("alpha", "0.2.0")])

    # pipeline 은 agents/alpha@0.1.0 을 참조한다 — 초안이 0.2.0 으로 덮으면 그 ref 가 깨진다
    assert not report.ok


# --- 버전 규칙 ---------------------------------------------------------------------


def test_saving_the_same_content_again_is_idempotent(author):
    first = author.save_graph("pipeline", graph("pipeline"))
    before = first.read_text(encoding="utf-8")

    second = author.save_graph("pipeline", graph("pipeline"))

    assert second == first and second.read_text(encoding="utf-8") == before


def test_a_changed_graph_without_a_bump_is_rejected(author, workspace):
    author.save_graph("pipeline", graph("pipeline"))
    changed = graph("pipeline").model_copy(
        update={"spec": graph("pipeline").spec.model_copy(update={"goal": "other"})}
    )

    with pytest.raises(AaestroError) as excinfo:
        author.save_graph("pipeline", changed)

    assert excinfo.value.code == ErrorCode.MOD_002
    assert (
        yaml.safe_load((workspace / "graphs" / "pipeline.yaml").read_text())["spec"]["goal"]
        == "test"
    )


def test_a_bumped_graph_is_written(author, workspace):
    author.save_graph("pipeline", graph("pipeline"))

    author.save_graph("pipeline", graph("pipeline", "1.1.0"))

    assert Catalog.under(workspace).graph("pipeline").metadata.version == "1.1.0"


def test_a_downgrade_is_rejected(author):
    author.save_graph("pipeline", graph("pipeline", "1.1.0"))

    with pytest.raises(AaestroError) as excinfo:
        author.save_graph("pipeline", graph("pipeline", "1.0.0"))

    assert excinfo.value.code == ErrorCode.MOD_002


def test_an_agent_bump_follows_the_same_rule(author, workspace):
    with pytest.raises(AaestroError) as excinfo:
        author.save_agent(
            "alpha",
            agent("alpha", "0.1.0", promptset_ref="promptsets/solo@0.1.0").model_copy(
                update={
                    "metadata": agent("alpha").metadata.model_copy(
                        update={"description": "changed"}
                    )
                }
            ),
        )

    assert excinfo.value.code == ErrorCode.MOD_002


# --- 이름은 위치다 ---------------------------------------------------------------


def test_the_path_name_must_match_the_declaration(author, workspace):
    with pytest.raises(AaestroError) as excinfo:
        author.save_graph("one", graph("two"))

    assert excinfo.value.code == ErrorCode.VAL_002
    assert not (workspace / "graphs" / "one.yaml").exists()
    assert not (workspace / "graphs" / "two.yaml").exists()


# --- 삭제 안전 ---------------------------------------------------------------------


def test_deleting_a_referenced_agent_is_refused(author, workspace):
    author.save_graph("pipeline", graph("pipeline"))

    with pytest.raises(AaestroError) as excinfo:
        author.delete_agent("alpha")

    assert excinfo.value.code == ErrorCode.VAL_002
    assert excinfo.value.details["referenced_by"] == ["pipeline"]
    assert (workspace / "agents" / "alpha" / "manifest.yaml").exists()


def test_an_unreferenced_agent_can_be_deleted(author, workspace):
    author.delete_agent("alpha")

    assert not (workspace / "agents" / "alpha" / "manifest.yaml").exists()


def test_deleting_a_missing_graph_is_not_found(author):
    with pytest.raises(AaestroError) as excinfo:
        author.delete_graph("nope")

    assert excinfo.value.code == ErrorCode.NF_001


def test_a_deployed_declaration_cannot_be_deleted_or_overwritten(workspace):
    """배포 중인 것을 바꾸면 실행 중 run 의 계약이 발밑에서 바뀐다 (#243 이 채운다)."""
    author = Author(catalog=Catalog.under(workspace), in_use=lambda kind, name: name == "alpha")

    with pytest.raises(AaestroError) as refused_delete:
        author.delete_agent("alpha")
    with pytest.raises(AaestroError) as refused_save:
        author.save_agent("alpha", agent("alpha", "0.2.0"))

    assert "deployed" in refused_delete.value.message
    assert "deployed" in refused_save.value.message


# --- 모듈은 쓰지 않는다 ------------------------------------------------------------


def test_the_author_has_no_module_writer():
    """04 Registry 2 — 게시된 모듈은 불변. 쓰는 메서드가 생기면 그것이 위반이다.

    빌드 재료(#264)는 모듈이 아니라 에이전트의 빌드 입력이라 여기에 속하지 않는다 —
    다만 같은 불변성 규칙을 따른다 (같은 버전에 다른 내용 금지).
    """
    writers = sorted(n for n in dir(Author) if n.startswith(("save_", "delete_")))

    assert writers == [
        "delete_agent",
        "delete_graph",
        "delete_materials",
        "save_agent",
        "save_all",
        "save_graph",
        "save_materials",
    ]
    # 취지는 이름 목록이 아니라 **모듈을 쓰지 않는다** 는 것이다
    assert not [n for n in writers if any(m in n for m in ("skillset", "promptset", "memoryset"))]


# --- 깨진 파일 -----------------------------------------------------------------------


def test_a_broken_saved_file_can_be_repaired(author, workspace):
    """버전을 비교할 대상이 없는 깨진 파일은 덮어쓸 수 있어야 고치는 길이 있다."""
    (workspace / "graphs" / "pipeline.yaml").write_text("kind: Graph\n", encoding="utf-8")

    author.save_graph("pipeline", graph("pipeline"))

    assert Catalog.under(workspace).graph("pipeline") == graph("pipeline")


def test_unrelated_broken_declarations_block_saving(author, workspace):
    """읽지 못한 선언은 검증에서 빠진 것이지 통과한 것이 아니다."""
    (workspace / "agents" / "ghost").mkdir()
    (workspace / "agents" / "ghost" / "manifest.yaml").write_text("kind: Agent\n", encoding="utf-8")

    with pytest.raises(AaestroError) as excinfo:
        author.save_graph("pipeline", graph("pipeline"))

    assert any(f.get("check") == "catalog" for f in excinfo.value.details["findings"])


# --- 리뷰 반영 (#249) --------------------------------------------------------------


def test_writes_are_atomic_a_failure_leaves_the_old_file_intact(author, workspace, monkeypatch):
    """`write_text` 는 먼저 비운다 — 중간에 죽으면 마지막 정상 버전이 사라진다."""
    author.save_graph("pipeline", graph("pipeline"))
    before = (workspace / "graphs" / "pipeline.yaml").read_text(encoding="utf-8")
    import aaestro.authoring as authoring

    def exploding(model):
        raise OSError("disk full")

    monkeypatch.setattr(authoring, "_serialize", exploding)

    with pytest.raises(OSError):
        author.save_graph("pipeline", graph("pipeline", "1.1.0"))

    assert (workspace / "graphs" / "pipeline.yaml").read_text(encoding="utf-8") == before
    assert not list((workspace / "graphs").glob(".*.tmp")), "임시 파일이 남았다"


def test_an_agent_bump_and_its_graph_can_only_be_saved_together(author, workspace):
    """따로는 어느 순서로도 통과하지 못한다 — 함께 검증하면 통과한다."""
    author.save_graph("pipeline", graph("pipeline"))
    bumped_agent = agent("alpha", "0.2.0")
    bumped_graph = graph("pipeline", "1.1.0", agent_version="0.2.0")

    with pytest.raises(AaestroError):
        author.save_agent("alpha", bumped_agent)  # 저장된 그래프의 ref 가 깨진다
    with pytest.raises(AaestroError):
        author.save_graph("pipeline", bumped_graph)  # 아직 없는 버전을 가리킨다

    written = author.save_all(graphs={"pipeline": bumped_graph}, agents={"alpha": bumped_agent})

    assert len(written) == 2
    assert Catalog.under(workspace).agent("alpha").metadata.version == "0.2.0"
    assert Catalog.under(workspace).graph("pipeline").metadata.version == "1.1.0"


def test_save_all_rolls_back_every_file_when_one_write_fails(author, workspace, monkeypatch):
    author.save_graph("pipeline", graph("pipeline"))
    before_graph = (workspace / "graphs" / "pipeline.yaml").read_bytes()
    before_agent = (workspace / "agents" / "alpha" / "manifest.yaml").read_bytes()
    real = Author._write
    calls = {"n": 0}

    def second_fails(path, model):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real(path, model)

    monkeypatch.setattr(Author, "_write", staticmethod(second_fails))

    with pytest.raises(OSError):
        author.save_all(
            graphs={"pipeline": graph("pipeline", "1.1.0", agent_version="0.2.0")},
            agents={"alpha": agent("alpha", "0.2.0")},
        )

    assert (workspace / "graphs" / "pipeline.yaml").read_bytes() == before_graph
    assert (workspace / "agents" / "alpha" / "manifest.yaml").read_bytes() == before_agent


def test_save_all_with_nothing_changed_writes_nothing(author):
    author.save_graph("pipeline", graph("pipeline"))

    assert author.save_all(graphs={"pipeline": graph("pipeline")}) == []


def test_cli_scoped_validation_ignores_unrelated_broken_graphs(workspace):
    """지목한 그래프만 판정한다는 docstring 과 실제가 같아야 한다."""
    (workspace / "graphs" / "broken.yaml").write_text("kind: Graph\n", encoding="utf-8")
    author = Author(catalog=Catalog.under(workspace))

    assert author.validate(graphs=[graph("draft")], with_saved_graphs=False).ok
    assert not author.validate(graphs=[graph("draft")], with_saved_graphs=True).ok


# --- 노드 ↔ promptset 계약이 저작에서 걸린다 (#260) ------------------------------------


def test_a_node_missing_its_template_is_refused_at_save(author, workspace):
    """#260 — 화면에서 만든 그래프가 run 에서 죽던 자리. 저장 전에 잡아야 한다."""
    write(
        workspace / "modules" / "promptsets" / "solo" / "0.1.0" / "promptset.yaml",
        {**promptset(), "spec": {"engine": "jinja2", "templates": {"default": {"file": "t.j2"}}}},
    )

    with pytest.raises(AaestroError) as exc_info:
        author.save_graph("pipeline", graph("pipeline"))

    assert exc_info.value.code == ErrorCode.VAL_001
    assert not (workspace / "graphs" / "pipeline.yaml").exists()


def test_a_node_that_does_not_supply_a_required_variable_is_refused(author, workspace):
    write(
        workspace / "modules" / "promptsets" / "solo" / "0.1.0" / "promptset.yaml",
        {
            **promptset(),
            "spec": {
                "engine": "jinja2",
                "templates": {
                    "default": {"file": "t.j2"},
                    "step": {
                        "file": "s.j2",
                        "variables": {"query": {"type": "string", "required": True}},
                    },
                },
            },
        },
    )

    report = author.validate(graphs=[graph("pipeline")], with_saved_graphs=False)

    assert not report.ok
    finding = next(f for f in report.findings if f.check == "node_templates")
    assert finding.details["missing"] == ["query"]


def test_the_author_feeds_the_promptsets_to_the_validator(author, workspace):
    """배선 확인 — `promptsets=` 를 빼면 이 검사는 아무 것도 보지 못한 채 통과한다."""
    write(
        workspace / "modules" / "promptsets" / "solo" / "0.1.0" / "promptset.yaml",
        {**promptset(), "spec": {"engine": "jinja2", "templates": {"default": {"file": "t.j2"}}}},
    )

    report = author.validate(graphs=[graph("pipeline")], with_saved_graphs=False)

    assert [f.check for f in report.findings] == ["node_templates"]


def test_an_unloadable_promptset_is_a_finding_not_a_silent_skip(author, workspace):
    """읽지 못한 promptset 을 건너뛰면 그 에이전트의 노드 검사가 조용히 꺼진다."""
    write(
        workspace / "modules" / "promptsets" / "solo" / "0.1.0" / "promptset.yaml",
        {
            "apiVersion": "aaestro/v1",
            "kind": "Promptset",
            "metadata": {"name": "solo", "version": "0.1.0"},
            "spec": {"engine": "jinja2"},  # templates 누락 — 스키마 위반
        },
    )

    report = author.validate(graphs=[graph("pipeline")], with_saved_graphs=False)

    assert not report.ok
    assert any(
        f.check == "node_templates" and "could not be loaded" in f.message for f in report.findings
    ), [f.message for f in report.findings]


# --- 빌드 재료 (#264) ----------------------------------------------------------------


@pytest.fixture
def author_with_materials(workspace: Path) -> Author:
    return Author(catalog=Catalog.under(workspace), materials=InMemoryMaterialStore())


def test_materials_are_keyed_to_the_declared_version(author_with_materials):
    """재료는 선언과 한 몸이다 — 버전이 다르면 다른 재료다."""
    saved = author_with_materials.save_materials("alpha", {"src/agent.py": "MARK = 1"})

    assert saved.version == "0.1.0"
    assert author_with_materials.read_materials("alpha").files == {"src/agent.py": "MARK = 1"}


def test_an_agent_without_materials_reads_as_empty(author_with_materials):
    """아직 넣지 않은 것은 오류가 아니다."""
    found = author_with_materials.read_materials("alpha")

    assert found.files == {} and found.version == "0.1.0"


def test_the_same_version_cannot_get_different_materials(author_with_materials):
    """그 버전으로 구운 이미지와 어긋나면 무엇이 도는지 알 수 없다 (04 Registry 2)."""
    author_with_materials.save_materials("alpha", {"src/agent.py": "MARK = 1"})

    with pytest.raises(AaestroError) as exc_info:
        author_with_materials.save_materials("alpha", {"src/agent.py": "MARK = 2"})

    assert exc_info.value.code == ErrorCode.MOD_002


def test_saving_the_same_materials_again_is_idempotent(author_with_materials):
    author_with_materials.save_materials("alpha", {"src/agent.py": "MARK = 1"})

    again = author_with_materials.save_materials("alpha", {"src/agent.py": "MARK = 1"})

    assert again.files == {"src/agent.py": "MARK = 1"}


def test_a_version_bump_carries_its_own_materials(author_with_materials, workspace):
    author_with_materials.save_materials("alpha", {"src/agent.py": "old"})
    bumped = agent("alpha", version="0.2.0")
    write(
        workspace / "agents" / "alpha" / "manifest.yaml",
        bumped.model_dump(mode="json", by_alias=True, exclude_none=True),
    )

    saved = Author(
        catalog=Catalog.under(workspace), materials=author_with_materials.materials
    ).save_materials("alpha", {"src/agent.py": "new"})

    assert saved.version == "0.2.0"
    assert author_with_materials.materials.get("alpha", "0.1.0").files == {"src/agent.py": "old"}


def test_materials_of_a_deployed_agent_cannot_change(workspace):
    """배포 중인 에이전트의 재료가 바뀌면 도는 이미지와 어긋난다."""
    author = Author(
        catalog=Catalog.under(workspace),
        materials=InMemoryMaterialStore(),
        in_use=lambda kind, name: kind == "agent" and name == "alpha",
    )

    with pytest.raises(AaestroError) as exc_info:
        author.save_materials("alpha", {"src/agent.py": "MARK = 1"})

    assert exc_info.value.code == ErrorCode.VAL_002
    assert "deployed" in exc_info.value.message


def test_deleting_materials_leaves_the_declaration(author_with_materials, workspace):
    author_with_materials.save_materials("alpha", {"src/agent.py": "MARK = 1"})

    assert author_with_materials.delete_materials("alpha") is True
    assert author_with_materials.read_materials("alpha").files == {}
    assert (workspace / "agents" / "alpha" / "manifest.yaml").exists()


def test_deleting_then_re_adding_cannot_swap_the_contents(author_with_materials):
    """삭제가 행을 지우면 불변성 검사의 근거가 사라진다 — 같은 버전에 다른 내용이 들어간다.

    그러면 그 버전으로 구운 이미지가 무엇으로 만들어졌는지 알 수 없다.
    """
    author_with_materials.save_materials("alpha", {"src/agent.py": "ORIGINAL"})
    author_with_materials.delete_materials("alpha")

    with pytest.raises(AaestroError) as exc_info:
        author_with_materials.save_materials("alpha", {"src/agent.py": "SWAPPED"})

    assert exc_info.value.code == ErrorCode.MOD_002


def test_deleting_twice_reports_that_nothing_was_there(author_with_materials):
    author_with_materials.save_materials("alpha", {"src/agent.py": "MARK = 1"})

    assert author_with_materials.delete_materials("alpha") is True
    assert author_with_materials.delete_materials("alpha") is False


def test_an_unchanged_save_is_allowed_while_deployed(workspace):
    """바뀌는 것이 없는 저장까지 막으면 같은 PUT 의 재시도가 실패로 보인다."""
    store = InMemoryMaterialStore()
    Author(catalog=Catalog.under(workspace), materials=store).save_materials(
        "alpha", {"src/agent.py": "MARK = 1"}
    )
    deployed = Author(
        catalog=Catalog.under(workspace), materials=store, in_use=lambda kind, name: True
    )

    again = deployed.save_materials("alpha", {"src/agent.py": "MARK = 1"})

    assert again.files == {"src/agent.py": "MARK = 1"}


def test_the_saved_record_carries_its_store_timestamp(author_with_materials):
    """넣은 것을 그대로 돌려주면 PUT 응답과 이후 GET 이 다르게 보인다."""
    saved = author_with_materials.save_materials("alpha", {"src/agent.py": "MARK = 1"})

    assert saved.updated_at
    assert saved.updated_at == author_with_materials.read_materials("alpha").updated_at


def test_a_path_outside_the_context_never_reaches_the_store(author_with_materials):
    with pytest.raises(AaestroError) as exc_info:
        author_with_materials.save_materials("alpha", {"../escape.py": "x"})

    assert exc_info.value.code == ErrorCode.VAL_002
    assert author_with_materials.materials.get("alpha", "0.1.0") is None


def test_materials_for_an_unknown_agent_are_not_found(author_with_materials):
    with pytest.raises(AaestroError) as exc_info:
        author_with_materials.save_materials("nobody", {})

    assert exc_info.value.code == ErrorCode.NF_001


def test_without_a_store_the_surface_is_refused(author):
    """재료 표면을 열지 않은 조립에서 조용히 성공하면 빌드가 빈 재료로 돈다."""
    with pytest.raises(AaestroError) as exc_info:
        author.save_materials("alpha", {})

    assert exc_info.value.code == ErrorCode.CFG_001


# --- 리뷰 반영 (#275) --------------------------------------------------------------


def test_a_new_declaration_is_readable_by_other_users(author, workspace):
    """``mkstemp`` 의 0600 을 그대로 두면 다른 uid 로 도는 에이전트 컨테이너가 선언을 못 읽는다."""
    author.save_graph("pipeline", graph("pipeline"))

    assert (workspace / "graphs" / "pipeline.yaml").stat().st_mode & 0o777 == 0o644


def test_replacing_a_declaration_keeps_its_permissions(author, workspace):
    """운영자가 정한 권한을 저장이 조용히 바꾸지 않는다 — 넓히지도, 좁히지도."""
    author.save_graph("pipeline", graph("pipeline"))
    path = workspace / "graphs" / "pipeline.yaml"
    path.chmod(0o640)

    author.save_graph("pipeline", graph("pipeline", "1.1.0"))

    assert path.stat().st_mode & 0o777 == 0o640
    assert Catalog.under(workspace).graph("pipeline").metadata.version == "1.1.0"


# --- 삭제가 남기는 것 (#258) ---------------------------------------------------------


def test_deleting_an_agent_removes_its_empty_directory(author, workspace):
    """`204` 는 "삭제됐다" 로 읽힌다 — 프레임워크가 쓴 것은 매니페스트뿐이라 빈 자리를 안 남긴다."""
    directory = workspace / "agents" / "alpha"
    assert directory.is_dir()

    retained = author.delete_agent("alpha")

    assert retained == []
    assert not directory.exists(), "빈 디렉토리가 남았다"


def test_files_aaestro_did_not_write_are_kept_and_named(author, workspace):
    """사람이 둔 코드는 지우지 않는다 — 대신 무엇이 남았는지 알린다 (#258)."""
    directory = workspace / "agents" / "alpha"
    (directory / "src").mkdir()
    (directory / "src" / "agent.py").write_text("MARK = 1\n", encoding="utf-8")
    (directory / "Dockerfile").write_text("FROM aaestro/agent-base:0.1.0\n", encoding="utf-8")

    retained = author.delete_agent("alpha")

    assert retained == ["Dockerfile", "src/agent.py"]
    assert (directory / "src" / "agent.py").read_text(encoding="utf-8") == "MARK = 1\n"
    assert not (directory / "manifest.yaml").exists(), "선언은 지워야 한다"


def test_empty_subdirectories_go_even_when_a_file_is_kept(author, workspace):
    """빈 디렉토리는 지운다 — 남은 파일 옆이라고 예외를 두면 빈 자리가 쌓인다 (#301 리뷰)."""
    directory = workspace / "agents" / "alpha"
    (directory / "notes.md").write_text("손으로 둔 파일\n", encoding="utf-8")
    (directory / "cache" / "inner").mkdir(parents=True)

    retained = author.delete_agent("alpha")

    assert retained == ["notes.md"]
    assert not (directory / "cache").exists(), "빈 하위 디렉토리가 남았다"
    assert (directory / "notes.md").exists()


def test_symlinks_are_reported_not_followed_or_removed(author, workspace, tmp_path):
    """디렉토리를 가리키는 링크에 rmdir 을 걸면 매니페스트를 지운 뒤에 터진다 (#301 리뷰)."""
    directory = workspace / "agents" / "alpha"
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "keep").mkdir(parents=True)
    (elsewhere / "keep" / "file.txt").write_text("남의 트리\n", encoding="utf-8")
    (directory / "linked").symlink_to(elsewhere / "keep", target_is_directory=True)
    (directory / "dangling").symlink_to(tmp_path / "gone")

    retained = author.delete_agent("alpha")

    assert retained == ["dangling", "linked"]
    assert (directory / "linked").is_symlink() and (directory / "dangling").is_symlink()
    assert (elsewhere / "keep" / "file.txt").exists(), "링크를 따라가 남의 트리를 건드렸다"


# --- fallback provider 와 프록시 upstream (#358) ------------------------------------------


def _agent_with_openai_fallback(name: str) -> AgentManifest:
    document = agent(name).model_dump(mode="json", by_alias=True, exclude_none=True)
    document["spec"]["model"]["fallbacks"] = [
        {"provider": "openai-compat", "name": "gpt-5", "on": ["server_error"]}
    ]
    return AgentManifest.model_validate(document)


def test_validate_reports_a_fallback_provider_the_proxy_does_not_terminate(workspace):
    """providers 가 Author 에서 validate_deployment 까지 닿는지 — 끊기면 프록시에서야 거절된다."""
    draft = _agent_with_openai_fallback("alpha")
    proxied = Author(catalog=Catalog.under(workspace), providers=frozenset({"anthropic"}))

    report = proxied.validate(agents=[draft])

    [finding] = [f for f in report.findings if f.check == "model_providers"]
    assert "openai-compat" in str(finding)


def test_validate_accepts_the_same_fallback_without_a_proxy_or_when_it_is_terminated(workspace):
    draft = _agent_with_openai_fallback("alpha")
    catalog = Catalog.under(workspace)

    no_proxy = Author(catalog=catalog, providers=None).validate(agents=[draft])
    terminated = Author(
        catalog=catalog, providers=frozenset({"anthropic", "openai-compat"})
    ).validate(agents=[draft])

    assert no_proxy.ok
    assert terminated.ok


# --- 노드 model override 의 null 은 지우기 선언이다 --------------------------------------


def test_a_node_override_that_clears_with_null_survives_saving(author, workspace):
    """``exclude_none`` 덤프가 설정한 null 을 지우면 "아래 계층 지우기" 가 사라진다."""
    document = graph("pipeline").model_dump(mode="json", by_alias=True)
    document["spec"]["nodes"][0]["model"] = {"retry": None, "circuit": None, "max_tokens": None}
    declared = GraphTopology.model_validate(document)

    author.save_graph("pipeline", declared)

    saved = Catalog.under(workspace).graph("pipeline").node("step").model
    assert saved is not None
    assert saved.model_fields_set == {"retry", "circuit", "max_tokens"}


def test_the_serialized_override_keeps_exactly_the_keys_it_set():
    from aaestro.authoring import _serialize

    document = mission_dict()
    document["spec"]["nodes"][0]["model"] = {"retry": None, "circuit": None, "max_tokens": None}
    declared = GraphTopology.model_validate(document)

    again = GraphTopology.model_validate(yaml.safe_load(_serialize(declared)))

    assert again.node("planner").model.model_fields_set == {"retry", "circuit", "max_tokens"}
    assert again.node("researcher").model is None


def test_a_serialization_that_drops_override_keys_is_caught_before_writing(
    author, workspace, monkeypatch
):
    """값만 비교하면 null 이 빠진 override 도 같아 보인다 — 키 집합까지 대조해야 잡힌다."""
    import aaestro.authoring as authoring

    original = authoring._serialize

    def dropping_nulls(model):
        document = yaml.safe_load(original(model))
        for node in document["spec"]["nodes"]:
            if "model" in node:
                node["model"] = {k: v for k, v in node["model"].items() if v is not None}
        return yaml.safe_dump(document, sort_keys=False)

    monkeypatch.setattr(authoring, "_serialize", dropping_nulls)
    document = graph("pipeline").model_dump(mode="json", by_alias=True)
    document["spec"]["nodes"][0]["model"] = {"max_tokens": 1, "circuit": None}

    with pytest.raises(AaestroError) as exc_info:
        author.save_graph("pipeline", GraphTopology.model_validate(document))

    assert exc_info.value.code == ErrorCode.INTERNAL_001
    assert not (workspace / "graphs" / "pipeline.yaml").exists()
