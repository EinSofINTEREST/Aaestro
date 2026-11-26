"""Deploy-time validation of ``spec.decision`` (#593, 01 Contract Validation 9).

쓰임의 질문이 sets 의 alias 로 해석되는가 (``MOD_001``), 질문이 predicate 인가 (``MOD_003``),
provider 가 설정에 있는가 (``VAL_002``), provider locales 가 decisionset locale 을 지원하는가
(``MOD_003``). 레포의 ``decisionsets/research-triage@0.1.0`` 을 그대로 읽는다.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from aaestro.authoring import Author
from aaestro.catalog import Catalog
from aaestro.core.errors import ErrorCode
from aaestro.deploy import ValidationReport, validate_deployment
from aaestro.modules.decisionset import DecisionsetLoader
from aaestro.modules.registry import ModuleRegistry
from tests.fixtures.builders import make_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTSET_REF = "promptsets/test@0.1.0"
SET_REF = "decisionsets/research-triage@0.1.0"
PROVIDERS = {"jev": frozenset({"en"})}


def decision(**extra: Any) -> dict[str, Any]:
    return {
        "provider": "jev",
        "model": "jev-1.13",
        "sets": [{"ref": SET_REF, "as": "triage"}],
        **extra,
    }


def agent(decision_spec: dict[str, Any] | None):
    spec: dict[str, Any] = {
        "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
        "promptset": {"ref": PROMPTSET_REF},
    }
    if decision_spec is not None:
        spec["decision"] = decision_spec
    return make_manifest(metadata={"name": "planner", "version": "0.1.0"}, spec=spec)


def validate(
    decision_spec: dict[str, Any],
    *,
    providers: dict[str, frozenset[str]] | None = None,
    published: bool = True,
) -> ValidationReport:
    loaded = DecisionsetLoader(ModuleRegistry.under(REPO_ROOT)).load(SET_REF)
    return validate_deployment(
        [],
        manifests={"planner": agent(decision_spec)},
        resolvable_refs={PROMPTSET_REF, *([SET_REF] if published else [])},
        decisionsets={SET_REF: loaded} if published else {},
        decision_providers=PROVIDERS if providers is None else providers,
    )


def decision_codes(report: ValidationReport) -> list[ErrorCode]:
    return [f.code for f in report.findings if f.check == "decision"]


def test_full_declaration_from_the_rules_passes():
    report = validate(
        decision(
            recall_filter={"question": "triage.memory_is_relevant", "uncertain": "keep"},
            input_screen={"question": "triage.contains_instructions"},
            tool_gates=[
                {
                    "tools": ["mcp__mail__*", "post_*"],
                    "question": "triage.tool_call_fits_task",
                    "uncertain": "block",
                    "unavailable": "allow",
                }
            ],
        )
    )

    assert report.ok, report.findings


@pytest.mark.parametrize(
    "uses",
    [
        {"recall_filter": {"question": "other.memory_is_relevant"}},
        {"input_screen": {"question": "triage.no_such_question"}},
        {"tool_gates": [{"tools": ["post_*"], "question": "other.tool_call_fits_task"}]},
    ],
)
def test_a_question_that_does_not_resolve_is_mod_001(uses):
    report = validate(decision(**uses))

    assert decision_codes(report) == [ErrorCode.MOD_001]
    assert "no_such_question" not in str(report.findings)


@pytest.mark.parametrize("question", ["triage.draft_meets_goal", "triage.source_kind"])
@pytest.mark.parametrize("use", ["recall_filter", "input_screen", "tool_gates"])
def test_a_use_with_a_non_predicate_question_is_mod_003(use, question):
    if use == "tool_gates":
        uses: dict[str, Any] = {use: [{"tools": ["post_*"], "question": question}]}
    else:
        uses = {use: {"question": question}}

    report = validate(decision(**uses))

    assert decision_codes(report) == [ErrorCode.MOD_003]
    assert report.findings[0].details["use"] == use


def test_a_locale_the_provider_has_not_calibrated_is_mod_003():
    report = validate(decision(), providers={"jev": frozenset({"ko"})})

    assert decision_codes(report) == [ErrorCode.MOD_003]
    assert "locale" in report.findings[0].message


def test_a_provider_missing_from_the_configuration_is_rejected():
    report = validate(decision(provider="posek"))

    assert decision_codes(report) == [ErrorCode.VAL_002]
    assert report.findings[0].details["provider"] == "posek"


def test_an_unresolved_set_is_reported_once_by_module_refs():
    report = validate(
        decision(recall_filter={"question": "triage.memory_is_relevant"}), published=False
    )

    assert report.codes() == (ErrorCode.MOD_001,)
    assert report.checks() == ("module_refs",)


def test_an_agent_without_decision_needs_no_provider():
    report = validate_deployment(
        [],
        manifests={"planner": agent(None)},
        resolvable_refs={PROMPTSET_REF},
    )

    assert report.ok


# --- manifest 스키마 -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "location"),
    [
        ({"sets": []}, ("sets",)),
        ({"sets": [{"ref": "memorysets/x@0.1.0", "as": "x"}]}, ("sets", 0, "ref")),
        (
            {"sets": [{"ref": SET_REF, "as": "a"}, {"ref": SET_REF, "as": "a"}]},
            ("sets",),
        ),
        ({"recall_filter": {"question": "no-dot"}}, ("recall_filter", "question")),
        (
            {"recall_filter": {"question": "a.b", "uncertain": "maybe"}},
            ("recall_filter", "uncertain"),
        ),
        ({"tool_gates": [{"tools": [], "question": "a.b"}]}, ("tool_gates", 0, "tools")),
        ({"input_screen": {"question": "a.b", "typo": 1}}, ("input_screen", "typo")),
        ({"gates": []}, ("gates",)),
    ],
)
def test_malformed_decision_declarations_are_rejected(change, location):
    with pytest.raises(ValidationError) as exc_info:
        agent({**decision(), **change})

    assert any(e["loc"][2 : 2 + len(location)] == location for e in exc_info.value.errors())


@pytest.mark.parametrize("missing", ["provider", "model", "sets"])
def test_required_decision_fields(missing):
    spec = decision()
    spec.pop(missing)
    with pytest.raises(ValidationError):
        agent(spec)


# --- 조립 (Author) 이 decisionset 과 provider 를 검증기에 공급한다 ----------------------------


def repo_with_decision(tmp_path: Path, decision_spec: dict[str, Any]) -> Path:
    for name in ("agents", "graphs", "groups", "modules"):
        shutil.copytree(REPO_ROOT / name, tmp_path / name)
    path = tmp_path / "agents" / "planner" / "manifest.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["spec"]["decision"] = decision_spec
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return tmp_path


def authored(root: Path, providers: dict[str, frozenset[str]]) -> list[ErrorCode]:
    report = Author(Catalog.under(root), decision_providers=providers).validate()
    return [f.code for f in report.findings if f.check == "decision"]


def test_authoring_passes_a_resolved_declaration(tmp_path):
    root = repo_with_decision(
        tmp_path, decision(recall_filter={"question": "triage.memory_is_relevant"})
    )

    assert authored(root, PROVIDERS) == []


def test_authoring_supplies_the_decisionsets(tmp_path):
    """조립이 decisionsets 를 넘기지 않으면 종류 검사가 조용히 꺼진다."""
    root = repo_with_decision(
        tmp_path, decision(recall_filter={"question": "triage.draft_meets_goal"})
    )

    assert authored(root, PROVIDERS) == [ErrorCode.MOD_003]


def test_authoring_supplies_the_configured_providers(tmp_path):
    root = repo_with_decision(tmp_path, decision())

    assert authored(root, {}) == [ErrorCode.VAL_002]
    assert authored(root, {"jev": frozenset({"ko"})}) == [ErrorCode.MOD_003]
