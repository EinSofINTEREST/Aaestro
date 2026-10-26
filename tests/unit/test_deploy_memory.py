"""Deploy-time validation of memory attachments (#467).

09 Attachment 4 (부착 위치 == memoryset `spec.scope`) 와 Compaction 4 (service 그래프의 run scope
는 compaction 필수) 를 배포 검증이 강제한다. 선언만 맞고 이 검사가 꺼져 있으면 런타임에야
드러나거나 아예 드러나지 않는다.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from aaestro.authoring import Author
from aaestro.catalog import Catalog
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode, field_problems
from aaestro.core.manifest import GroupManifest, MemorySpaceRef
from aaestro.core.topology import GraphTopology
from aaestro.deploy import ValidationReport, validate_deployment
from aaestro.modules.memoryset import LoadedMemoryset, MemorysetLoader, MemorysetManifest
from tests.fixtures.builders import make_manifest
from tests.fixtures.topologies import mission_dict, service_dict

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTSET_REF = "promptsets/test@0.1.0"
COMPACTION = {"trigger_entries": 100, "strategy": "summarize", "keep_kinds": ["fact"]}


def ref_of(name: str) -> str:
    return f"memorysets/{name}@0.1.0"


def memoryset(name: str, scope: str, *, compaction: bool = False) -> LoadedMemoryset:
    retention: dict[str, Any] = {"ttl_days": 30}
    if compaction:
        retention["compaction"] = COMPACTION
    manifest = MemorysetManifest.model_validate(
        {
            "apiVersion": "aaestro/v1",
            "kind": "Memoryset",
            "metadata": {"name": name, "version": "0.1.0"},
            "spec": {
                "scope": scope,
                "index": {
                    "embedding": {
                        "provider": "openai-compatible",
                        "model": "text-embedding-3-small",
                        "dimensions": 1536,
                    }
                },
                "retention": retention,
            },
        }
    )
    return LoadedMemoryset(ref=ref_of(name), manifest=manifest)


def space(name: str) -> dict[str, str]:
    return {"ref": ref_of(name), "as": name}


def agent(*names: str):
    spec: dict[str, Any] = {
        "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
        "promptset": {"ref": PROMPTSET_REF},
        "memory": {"spaces": [space(n) for n in names]},
    }
    return make_manifest(metadata={"name": "planner", "version": "0.1.0"}, spec=spec)


def group(name: str, *names: str) -> GroupManifest:
    return GroupManifest.model_validate(
        {
            "apiVersion": "aaestro/v1",
            "kind": "Group",
            "metadata": {"name": name, "version": "0.1.0"},
            "spec": {"memory": {"spaces": [space(n) for n in names]}},
        }
    )


def graph(*names: str, mode: str = "mission") -> GraphTopology:
    build = service_dict if mode == "service" else mission_dict
    raw = build(
        nodes=[{"id": "planner", "agent": "agents/planner@0.1.0"}],
        edges=[{"from": "START", "to": "planner"}, {"from": "planner", "to": "END"}],
        memory={"spaces": [space(n) for n in names]},
    )
    return GraphTopology.model_validate(raw)


def validate(
    *,
    sets: list[LoadedMemoryset],
    topologies: list[GraphTopology] | None = None,
    manifest=None,
    groups: dict[str, GroupManifest] | None = None,
    published: bool = True,
) -> ValidationReport:
    refs = {PROMPTSET_REF, *(m.ref for m in sets if published)}
    return validate_deployment(
        topologies or [],
        manifests={"planner": manifest or agent()},
        groups=groups,
        resolvable_refs=refs,
        memorysets={m.ref: m for m in sets},
    )


def memory_findings(report: ValidationReport):
    return [f for f in report.findings if f.check == "memory_attachments"]


# --- 통과 (허용 경계) ----------------------------------------------------------


def test_every_scope_attached_at_its_own_location_passes():
    report = validate(
        sets=[
            memoryset("mine", "local"),
            memoryset("shared", "group"),
            memoryset("org", "global"),
            memoryset("scratch", "run"),
        ],
        manifest=agent("mine"),
        groups={"research": group("research", "shared"), "global": group("global", "org")},
        topologies=[graph("scratch")],
    )

    assert report.ok, [f.message for f in report.findings]


def test_a_service_graph_with_a_compacting_run_memoryset_passes():
    report = validate(
        sets=[memoryset("scratch", "run", compaction=True)],
        topologies=[graph("scratch", mode="service")],
    )

    assert report.ok, [f.message for f in report.findings]


def test_a_mission_graph_needs_no_compaction():
    report = validate(sets=[memoryset("scratch", "run")], topologies=[graph("scratch")])

    assert report.ok, [f.message for f in report.findings]


# --- 1. 그래프 ref 해석 -------------------------------------------------------------


def test_a_graph_space_that_does_not_resolve_is_reported_once():
    report = validate(
        sets=[memoryset("scratch", "run")], topologies=[graph("scratch")], published=False
    )

    found = memory_findings(report)
    assert [f.code for f in found] == [ErrorCode.MOD_001]
    assert found[0].details == {"graph": "research-pipeline", "module_ref": ref_of("scratch")}


def test_an_unresolved_agent_space_is_left_to_module_refs():
    """같은 결함을 finding 두 개로 내지 않는다."""
    report = validate(sets=[memoryset("mine", "local")], manifest=agent("mine"), published=False)

    assert memory_findings(report) == []
    assert "module_refs" in report.checks()


def test_a_group_space_that_does_not_resolve_is_reported():
    report = validate(
        sets=[memoryset("shared", "group")],
        groups={"research": group("research", "shared")},
        published=False,
    )

    found = memory_findings(report)
    assert [f.code for f in found] == [ErrorCode.MOD_001]
    assert found[0].details["group"] == "research"


# --- 2. 부착 위치 ↔ scope ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "attached_at"),
    [
        ("group", "local"),
        ("global", "local"),
        ("run", "local"),
        ("local", "group"),
        ("global", "group"),
        ("local", "global"),
        ("group", "global"),
        ("local", "run"),
        ("group", "run"),
    ],
)
def test_a_memoryset_attached_at_another_scope_is_mod_003(declared, attached_at):
    sets = [memoryset("space", declared)]
    kwargs: dict[str, Any] = {}
    if attached_at == "local":
        kwargs["manifest"] = agent("space")
    elif attached_at == "group":
        kwargs["groups"] = {"research": group("research", "space")}
    elif attached_at == "global":
        kwargs["groups"] = {"global": group("global", "space")}
    else:
        kwargs["topologies"] = [graph("space")]

    found = memory_findings(validate(sets=sets, **kwargs))

    assert [f.code for f in found] == [ErrorCode.MOD_003]
    assert declared in found[0].message
    assert attached_at in found[0].message
    assert found[0].details["module_ref"] == ref_of("space")


def test_the_global_group_is_checked_as_global_not_group():
    """groups/global.yaml 은 예약 그룹 — group scope 로 보면 정상 선언이 거절된다."""
    ok = validate(sets=[memoryset("org", "global")], groups={"global": group("global", "org")})
    wrong = validate(sets=[memoryset("org", "group")], groups={"global": group("global", "org")})

    assert memory_findings(ok) == []
    assert [f.code for f in memory_findings(wrong)] == [ErrorCode.MOD_003]


# --- 3. service 그래프의 compaction -----------------------------------------------------


def test_a_service_graph_with_a_run_memoryset_without_compaction_is_reported():
    topology = graph("scratch", mode="service")

    report = validate(sets=[memoryset("scratch", "run")], topologies=[topology])

    found = memory_findings(report)
    assert [f.code for f in found] == [ErrorCode.MOD_003]
    assert "without compaction" in found[0].message
    assert found[0].details == {"graph": topology.metadata.name, "module_ref": ref_of("scratch")}


def test_a_scope_mismatch_in_a_service_graph_is_not_also_a_compaction_finding():
    """local memoryset 을 run 에 붙인 결함은 부착 검사가 보고한다 — 한 결함에 finding 하나."""
    report = validate(sets=[memoryset("mine", "local")], topologies=[graph("mine", mode="service")])

    found = memory_findings(report)
    assert len(found) == 1
    assert "scope mismatch" in found[0].message


# --- 저장소의 실제 선언 -----------------------------------------------------------------


def test_the_repository_declarations_pass_the_memory_checks():
    """검증기가 만든 예제만 통과하면 아무것도 증명하지 못한다 — 실제 선언을 Author 로 돌린다."""
    report = Author(Catalog.under(REPO_ROOT)).validate()

    assert report.ok, [f"{f.check}: {f.message}" for f in report.findings]


# --- 조립 (Author) 이 memoryset 을 검증기에 공급한다 ----------------------------------------


def test_authoring_rejects_a_graph_that_attaches_a_memoryset_of_another_scope(tmp_path):
    """검증기만 맞고 조립이 memorysets 를 안 넘기면 검사가 조용히 꺼진다."""
    root = repo_copy(tmp_path)
    edit(
        root / "graphs" / "feed-monitor.yaml",
        lambda doc: doc["spec"]["memory"]["spaces"][0].update(
            ref="memorysets/agent-longterm@0.1.0"
        ),
    )

    report = Author(Catalog.under(root)).validate()

    found = memory_findings(report)
    assert [f.code for f in found] == [ErrorCode.MOD_003]
    assert found[0].details["graph"] == "feed-monitor"


def test_authoring_rejects_a_service_run_memoryset_that_lost_its_compaction(tmp_path):
    root = repo_copy(tmp_path)
    path = root / "modules" / "memorysets" / "run-scratch" / "0.1.0" / "memoryset.yaml"
    edit(path, lambda doc: doc["spec"]["retention"].pop("compaction"))

    report = Author(Catalog.under(root)).validate()

    found = [f for f in memory_findings(report) if "without compaction" in f.message]
    assert [f.details["graph"] for f in found] == ["feed-monitor"]


# --- 조립: 그 경로에만 부착된 ref 로 공급 줄을 하나씩 고정한다 ---------------------------------


def repo_copy(tmp_path: Path) -> Path:
    for name in ("agents", "graphs", "groups", "modules"):
        shutil.copytree(REPO_ROOT / name, tmp_path / name)
    return tmp_path


def edit(path: Path, mutate: Callable[[dict[str, Any]], object]) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(doc)
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")


def publish_memoryset(root: Path, name: str, scope: str) -> str:
    """agent-longterm 을 `name` · `scope` 로 바꿔 게시한다 — 다른 곳에 부착되지 않은 새 ref."""
    source = REPO_ROOT / "modules" / "memorysets" / "agent-longterm" / "0.1.0" / "memoryset.yaml"
    target = root / "modules" / "memorysets" / name / "0.1.0" / "memoryset.yaml"
    target.parent.mkdir(parents=True)
    doc = yaml.safe_load(source.read_text(encoding="utf-8"))
    doc["metadata"]["name"] = name
    doc["spec"]["scope"] = scope
    target.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return ref_of(name)


def attach(ref: str, name: str) -> Callable[[dict[str, Any]], object]:
    def mutate(doc: dict[str, Any]) -> object:
        spaces = doc["spec"].setdefault("memory", {}).setdefault("spaces", [])
        return spaces.append({"ref": ref, "as": name})

    return mutate


def authored(root: Path) -> list:
    return memory_findings(Author(Catalog.under(root)).validate())


def test_authoring_checks_a_space_attached_only_by_a_group(tmp_path):
    root = repo_copy(tmp_path)
    ref = publish_memoryset(root, "only-group", "local")
    edit(root / "groups" / "research.yaml", attach(ref, "only"))

    found = authored(root)

    assert [(f.code, f.details) for f in found] == [
        (ErrorCode.MOD_003, {"group": "research", "module_ref": ref})
    ]


def test_authoring_checks_a_space_attached_only_by_the_global_group(tmp_path):
    root = repo_copy(tmp_path)
    ref = publish_memoryset(root, "only-global", "group")
    edit(root / "groups" / "global.yaml", attach(ref, "only"))

    found = authored(root)

    assert [(f.code, f.details) for f in found] == [
        (ErrorCode.MOD_003, {"group": "global", "module_ref": ref})
    ]


def test_authoring_checks_a_space_attached_only_by_a_manifest(tmp_path):
    root = repo_copy(tmp_path)
    ref = publish_memoryset(root, "only-agent", "group")
    edit(root / "agents" / "planner" / "manifest.yaml", attach(ref, "only"))

    found = authored(root)

    assert [(f.code, f.details) for f in found] == [
        (ErrorCode.MOD_003, {"agent": "planner", "module_ref": ref})
    ]


def test_authoring_reports_a_published_memoryset_it_cannot_load(tmp_path, monkeypatch):
    """읽지 못한 선언은 검사에서 빠진 것이지 통과한 것이 아니다 — 주체와 함께 finding 으로."""
    root = repo_copy(tmp_path)
    ref = publish_memoryset(root, "unreadable", "local")
    edit(root / "agents" / "planner" / "manifest.yaml", attach(ref, "only"))
    real_load = MemorysetLoader.load

    def load(self: MemorysetLoader, requested: str) -> LoadedMemoryset:
        if requested == ref:
            raise AaestroError(
                category=ErrorCategory.MODULE, code=ErrorCode.MOD_003, message="unreadable"
            )
        return real_load(self, requested)

    monkeypatch.setattr(MemorysetLoader, "load", load)

    found = authored(root)

    assert [(f.code, f.details) for f in found] == [
        (ErrorCode.MOD_003, {"agent": "planner", "module_ref": ref})
    ]
    assert "could not be loaded" in found[0].message


def test_a_broken_memoryset_document_is_reported_once_by_the_catalog(tmp_path):
    """깨진 문서는 catalog 가 보고한다 — 부착 검사가 같은 결함을 다시 내지 않는다."""
    root = repo_copy(tmp_path)
    path = root / "modules" / "memorysets" / "run-scratch" / "0.1.0" / "memoryset.yaml"
    edit(path, lambda doc: doc["spec"].pop("index"))

    report = Author(Catalog.under(root)).validate()

    assert any(f.check == "catalog" for f in report.findings)
    assert not any("could not be loaded" in f.message for f in report.findings)
    assert [f.code for f in memory_findings(report)] == [ErrorCode.MOD_001, ErrorCode.MOD_001]


# --- space ref 의 타입 (스키마 경계) ---------------------------------------------------------


@pytest.mark.parametrize("ref", ["promptsets/x@0.1.0", "skillsets/x@0.1.0", "agents/x@0.1.0"])
def test_a_memory_space_ref_of_another_type_is_rejected_without_echoing_it(ref):
    with pytest.raises(ValidationError) as exc_info:
        MemorySpaceRef.model_validate({"ref": ref, "as": "x"})

    # 날 pydantic 에러는 입력을 싣는다 — 경계가 쓰는 변환 (#448) 을 거친 모양을 본다
    problems = field_problems(exc_info.value)
    assert "memory space ref must be" in str(problems)
    assert ref not in str(problems)


def test_a_memory_space_ref_of_another_type_is_rejected_in_a_manifest():
    with pytest.raises(ValueError, match="memory space ref must be"):
        agent_with_ref("promptsets/x@0.1.0")


def agent_with_ref(ref: str):
    spec: dict[str, Any] = {
        "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
        "promptset": {"ref": PROMPTSET_REF},
        "memory": {"spaces": [{"ref": ref, "as": "x"}]},
    }
    return make_manifest(metadata={"name": "planner", "version": "0.1.0"}, spec=spec)
