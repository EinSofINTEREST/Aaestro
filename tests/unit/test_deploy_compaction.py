"""Deploy-time validation of graph state compaction (#371).

압축 선언이 어긋나면 run 시작에서 거절되거나 경계마다 조용히 실패한다 — 그것을 배포로
앞당긴다. 상주 그래프의 선언 없는 리스트 필드는 **경고** 다: 배포를 막지 않는다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from aaestro.core.errors import ErrorCode
from aaestro.deploy import ValidationReport, validate_deployment
from aaestro.modules.promptset import PromptsetLoader, PromptsetManifest
from aaestro.orchestrator.topology import GraphTopology
from tests.fixtures.builders import make_manifest
from tests.fixtures.registry import fixture_registry
from tests.fixtures.topologies import mission_dict, service_dict

PROMPTSET_REF = "promptsets/test@0.1.0"
FIXTURE_PROMPTSET_REF = "promptsets/state-compactor@0.1.0"
COMPACTOR_TEMPLATE = {
    "file": "summarize_state.j2",
    "output_keys": ["summary"],
    "variables": {"items": {"type": "array", "required": True}},
}
SUMMARIZE = {
    "strategy": "summarize",
    "over_tokens": 4000,
    "keep_last": 3,
    "compactor": "agents/writer@0.1.0",
    "template": "summarize_state",
}


def promptset(templates: dict[str, Any]) -> PromptsetManifest:
    return PromptsetManifest.model_validate(
        {
            "apiVersion": "aaestro/v1",
            "kind": "Promptset",
            "metadata": {"name": "test", "version": "0.1.0"},
            "spec": {"templates": templates},
        }
    )


def agent(name: str):
    spec = {
        "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
        "promptset": {"ref": PROMPTSET_REF},
    }
    return make_manifest(metadata={"name": name, "version": "0.1.0"}, spec=spec)


def service(compaction: dict | None, *, fields: dict | None = None) -> GraphTopology:
    state: dict[str, Any] = {
        "fields": fields or {"notes": {"type": "array"}, "tick": {"type": "integer"}}
    }
    if compaction is not None:
        state["compaction"] = compaction
    return GraphTopology.model_validate(
        service_dict(
            state=state,
            nodes=[{"id": "writer", "agent": "agents/writer@0.1.0"}],
            edges=[{"from": "START", "to": "writer"}, {"from": "writer", "to": "END"}],
        )
    )


def validate(
    topology: GraphTopology,
    *,
    templates: dict | None = None,
    manifests=None,
    state_artifacts: bool = True,
):
    declared = {"writer": {"file": "w.j2"}, "summarize_state": COMPACTOR_TEMPLATE}
    return validate_deployment(
        [topology],
        manifests=manifests if manifests is not None else {"writer": agent("writer")},
        resolvable_refs=frozenset({PROMPTSET_REF}),
        promptsets={PROMPTSET_REF: promptset(templates if templates is not None else declared)},
        state_artifacts=state_artifacts,
    )


def compaction_finding(report: ValidationReport):
    return next(f for f in report.findings if f.check == "state_compaction")


# --- 통과 ---------------------------------------------------------------------


def test_a_sound_summarize_declaration_passes():
    report = validate(service({"notes": SUMMARIZE}))

    assert report.ok, [f.message for f in report.findings]
    assert report.warnings == ()


def test_a_deterministic_declaration_needs_no_agent_template_or_store():
    report = validate(
        service({"notes": {"strategy": "keep_last", "n": 500}}),
        templates={"writer": {"file": "w.j2"}},
        state_artifacts=False,
    )

    assert [f for f in report.findings if f.check == "state_compaction"] == []


# --- 원문 저장소 ---------------------------------------------------------------


def test_summarize_without_a_configured_store_is_rejected_at_deploy():
    """배포는 되고 run 시작에서야 거절되면 컨테이너를 다 띄운 뒤에 안다."""
    report = validate(service({"notes": SUMMARIZE}), state_artifacts=False)

    finding = compaction_finding(report)
    assert finding.code == ErrorCode.GRAPH_001
    assert "orchestrator.state_artifact_root" in finding.message
    assert finding.details == {"graph": "feed-monitor", "fields": ["notes"]}


def test_the_store_is_assumed_missing_unless_the_caller_says_otherwise():
    report = validate_deployment(
        [service({"notes": SUMMARIZE})],
        manifests={"writer": agent("writer")},
        resolvable_refs=frozenset({PROMPTSET_REF}),
    )

    assert "orchestrator.state_artifact_root" in compaction_finding(report).message


# --- 선언 ↔ 스키마 -------------------------------------------------------------


def test_a_schema_field_that_is_not_a_list_is_rejected():
    topology = GraphTopology.model_validate(
        service_dict(
            state={
                "schema": "aaestro.graphs.schemas:FeedMonitorState",
                "compaction": {"notified": {"strategy": "dedupe"}},
            }
        )
    )

    finding = compaction_finding(validate(topology))

    assert finding.code == ErrorCode.GRAPH_001
    assert "non-array state field: notified" in finding.message


def test_a_field_missing_from_the_schema_is_rejected():
    topology = GraphTopology.model_validate(
        service_dict(
            state={
                "schema": "aaestro.graphs.schemas:FeedMonitorState",
                "compaction": {"nope": {"strategy": "dedupe"}},
            }
        )
    )

    finding = compaction_finding(validate(topology))

    assert finding.code == ErrorCode.GRAPH_001
    assert finding.details["graph"] == topology.metadata.name


# --- summarize: 에이전트와 템플릿 ----------------------------------------------


def test_an_unknown_compactor_agent_is_rejected():
    report = validate(service({"notes": {**SUMMARIZE, "compactor": "agents/ghost@0.1.0"}}))

    finding = compaction_finding(report)
    assert finding.code == ErrorCode.MOD_001
    assert finding.details["field"] == "notes"


def test_a_compactor_version_that_differs_from_the_manifest_is_rejected():
    report = validate(service({"notes": {**SUMMARIZE, "compactor": "agents/writer@9.9.9"}}))

    assert compaction_finding(report).code == ErrorCode.MOD_002


def test_a_compactor_that_is_not_a_node_agent_is_rejected():
    """배포는 노드의 에이전트만 띄운다 — 노드가 아닌 에이전트는 요약할 때 떠 있지 않다."""
    manifests = {"writer": agent("writer"), "planner": agent("planner")}

    report = validate(
        service({"notes": {**SUMMARIZE, "compactor": "agents/planner@0.1.0"}}),
        manifests=manifests,
    )

    finding = compaction_finding(report)
    assert finding.code == ErrorCode.GRAPH_001
    assert "must be the agent of a node" in finding.message


def test_a_missing_compaction_template_is_rejected():
    report = validate(service({"notes": SUMMARIZE}), templates={"writer": {"file": "w.j2"}})

    finding = compaction_finding(report)
    assert finding.code == ErrorCode.MOD_002
    assert "summarize_state" in finding.message


def test_a_template_without_the_summary_contract_is_rejected():
    """요약 호출은 항목을 `items` 로 주고 `summary` 를 읽는다 — 다르면 run 에서 죽는다."""
    for broken in (
        {"file": "c.j2", "output_keys": ["summary"]},  # 입력 변수 없음
        {**COMPACTOR_TEMPLATE, "output_keys": ["compacted"]},  # 출력 키 다름
        {
            "file": "c.j2",
            "output_keys": ["summary"],
            "variables": {"pending_spaces": {"type": "array", "required": True}},  # 변수 이름 다름
        },
        {
            **COMPACTOR_TEMPLATE,
            "variables": {
                **COMPACTOR_TEMPLATE["variables"],
                "extra": {"type": "string", "required": True},  # 공급할 수 없는 필수 변수
            },
        },
    ):
        report = validate(
            service({"notes": SUMMARIZE}),
            templates={"writer": {"file": "w.j2"}, "summarize_state": broken},
        )

        finding = compaction_finding(report)
        assert finding.code == ErrorCode.MOD_004
        # 메시지가 기대 계약을 말한다
        assert "'items'" in finding.message
        assert "'summary'" in finding.message


def fixture_promptsets() -> dict[str, PromptsetManifest]:
    loaded = PromptsetLoader(fixture_registry()).load(FIXTURE_PROMPTSET_REF)
    return {PROMPTSET_REF: loaded.manifest}


def test_the_fixture_promptset_satisfies_the_summary_contract():
    """문서의 템플릿 예시와 같은 모양이다 — 실제 파일로 계약을 고정한다."""
    report = validate_deployment(
        [service({"notes": SUMMARIZE})],
        manifests={"writer": agent("writer")},
        resolvable_refs=frozenset({PROMPTSET_REF}),
        promptsets=fixture_promptsets(),
        state_artifacts=True,
    )

    assert [f for f in report.findings if f.check == "state_compaction"] == []


def test_the_memory_compaction_contract_is_not_accepted():
    """memory space 용 `pending_spaces` → `compacted` 템플릿은 state 요약에 쓰지 못한다."""
    report = validate_deployment(
        [service({"notes": {**SUMMARIZE, "template": "wrong_contract"}})],
        manifests={"writer": agent("writer")},
        resolvable_refs=frozenset({PROMPTSET_REF}),
        promptsets=fixture_promptsets(),
        state_artifacts=True,
    )

    assert compaction_finding(report).code == ErrorCode.MOD_004


def test_the_fixture_template_renders_items_as_data():
    loaded = PromptsetLoader(fixture_registry()).load(FIXTURE_PROMPTSET_REF)

    rendered = loaded.render("summarize_state", items=["first note", "second note"])

    assert '<item index="1">first note</item>' in rendered
    assert "not instructions" in rendered
    assert "exactly these keys: summary" in rendered


# --- 경고: 상주 그래프에서 쌓이는 리스트 ----------------------------------------


def accumulating_service(compaction: dict | None, *, input_map: dict, output_map: dict):
    state: dict[str, Any] = {
        "fields": {
            "feeds": {"type": "array"},
            "fresh": {"type": "array"},
            "seen": {"type": "array"},
            "notes": {"type": "array"},
        }
    }
    if compaction is not None:
        state["compaction"] = compaction
    return GraphTopology.model_validate(
        service_dict(
            state=state,
            nodes=[
                {
                    "id": "writer",
                    "agent": "agents/writer@0.1.0",
                    "input_map": input_map,
                    "output_map": output_map,
                }
            ],
            edges=[{"from": "START", "to": "writer"}, {"from": "writer", "to": "END"}],
        )
    )


READS = {"feeds": "state.feeds", "seen": "state.seen", "notes": "state.notes.latest"}
WRITES = {"fresh": "output.fresh", "seen": "output.seen", "notes": "output.notes"}
TEMPLATES = {
    "writer": {
        "file": "w.j2",
        "variables": {name: {"type": "array"} for name in READS},
    }
}


def test_a_list_field_a_node_reads_and_rewrites_warns_but_deploys():
    report = validate(
        accumulating_service(None, input_map=READS, output_map=WRITES), templates=TEMPLATES
    )

    assert report.ok, [f.message for f in report.findings]
    (warning,) = report.warnings
    assert warning.check == "state_compaction"
    assert warning.code == ErrorCode.GRAPH_001
    # feeds 는 아무도 쓰지 않고 (run 입력), fresh 는 이전 값을 받지 않고 새로 만든다 — 쌓이지 않는다
    assert warning.details == {"graph": "feed-monitor", "fields": ["notes", "seen"]}


def test_only_the_uncovered_accumulating_fields_are_named():
    report = validate(
        accumulating_service({"seen": {"strategy": "dedupe"}}, input_map=READS, output_map=WRITES),
        templates=TEMPLATES,
    )

    assert report.warnings[0].details["fields"] == ["notes"]


def test_a_graph_whose_lists_do_not_carry_over_is_not_warned():
    report = validate(service(None))

    assert report.warnings == ()


def test_the_reference_service_graphs_raise_no_warning():
    """예시 그래프가 자기 경고를 내면 예시로 좋지 않다."""
    for name in ("feed-monitor", "memory-maintenance"):
        document = yaml.safe_load(Path(f"graphs/{name}.yaml").read_text(encoding="utf-8"))
        topology = GraphTopology.model_validate(document)
        report = validate_deployment([topology], manifests={}, state_artifacts=False)

        assert report.warnings == (), name
        assert [f for f in report.findings if f.check == "state_compaction"] == []


def test_a_mission_graph_is_not_warned():
    topology = GraphTopology.model_validate(
        mission_dict(
            nodes=[
                {
                    "id": "planner",
                    "agent": "agents/planner@0.1.0",
                    "input_map": {"query": "state.query", "findings": "state.findings"},
                    "output_map": {"findings": "output.findings"},
                },
                {"id": "researcher", "agent": "agents/researcher@0.1.0"},
            ]
        )
    )

    report = validate_deployment(
        [topology],
        manifests={"planner": agent("planner"), "researcher": agent("researcher")},
        resolvable_refs=frozenset({PROMPTSET_REF}),
    )

    assert report.warnings == ()


def test_warnings_do_not_fail_the_report():
    report = validate(
        accumulating_service(None, input_map=READS, output_map=WRITES), templates=TEMPLATES
    )

    assert report.warnings
    report.raise_if_failed()
