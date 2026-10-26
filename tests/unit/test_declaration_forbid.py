"""Declaration schemas reject unknown keys instead of silently dropping them (#386).

선언 스키마가 모르는 키를 조용히 버리면 좁히는 선언 (``allowed_tools``, ``egress``, memory
``mode``) 의 오타가 **더 넓은 기본값** 으로 바뀐다. 이 파일은 (1) 선언 모델의 누락을 구조적으로
잡고, (2) 오타가 거절되는지, (3) 저장소의 모든 선언이 새 검증을 통과하는지, (4) 조회 응답을
되돌려 보내는 왕복이 깨지지 않는지, (5) 배포 검증이 모듈 선언도 읽는지를 고정한다.
"""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
import shutil
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel, ValidationError

import aaestro
from aaestro.authoring import Author
from aaestro.catalog import Catalog
from aaestro.cli.main import main as cli_main
from aaestro.core.errors import AaestroError
from aaestro.core.manifest import AgentManifest, GroupManifest
from aaestro.core.topology import GraphTopology
from aaestro.modules.memoryset import MemorysetLoader, MemorysetManifest
from aaestro.modules.promptset import PromptsetLoader, PromptsetManifest
from aaestro.modules.registry import ModuleRegistry
from aaestro.modules.skillset import SkillsetLoader, SkillsetManifest
from tests.fixtures.builders import manifest_dict
from tests.fixtures.declarations import (
    KIND_MODELS,
    MARKER,
    REPO_ROOT,
    _declaration_documents,
    _declaration_files,
    _edit,
    _first_doc,
    _repo_copy,
    _with,
)
from tests.fixtures.topologies import mission_dict

SKIPPED_MODULES = {
    # 임포트하면 서버를 띄우는 진입점 — 모델을 정의하지 않는다
    "aaestro.agentd.__main__",
    "aaestro.cli.__main__",
    "aaestro.egress.__main__",
    "aaestro.memory.__main__",
    "aaestro.orchestrator.__main__",
}

NOT_FORBIDDEN = {
    # 프로세스 간 전송 · 응답 모델 — 양쪽 버전이 다를 수 있어 새 필드에 내성이 필요하다
    "Acknowledgement": "agentd 응답",
    "ComponentHealth": "health 응답",
    "HealthStatus": "health 응답",
    "ModelUsage": "TaskResult 에 실려 오간다",
    "TaskConfig": "TaskRequest 에 실려 오간다",
    "TaskRequest": "orchestrator → agentd 전송",
    "TaskResult": "agentd → orchestrator 전송",
    "TraceContext": "A2A 호출을 따라 전파된다",
    "AaestroErrorPayload": "TaskResult / API 응답에 실린다",
    "DoneEvent": "스트리밍 이벤트",
    "ErrorEvent": "스트리밍 이벤트",
    "SteerEvent": "스트리밍 이벤트",
    "TokenEvent": "스트리밍 이벤트",
    "ToolCallEvent": "스트리밍 이벤트",
    "ToolResultEvent": "스트리밍 이벤트",
    "_BaseEvent": "스트리밍 이벤트 베이스",
    "SkillSpec": "agentd 가 광고하는 도구 스키마 (내부 값)",
    "MemoryEntry": "Memory Service 와 에이전트 사이 전송",
    "MemorySource": "MemoryEntry 의 일부",
    "AgentCard": "A2A 상대가 보내는 카드 — 상대 버전의 새 필드에 내성이 필요하다",
    "SkillCard": "AgentCard 의 일부",
    "RunView": "control plane 이 보고하는 응답 모델",
    # 로더가 만든 내부 값 — 선언 문서가 아니다
    "LoadedMemoryset": "로더 산출물",
    "LoadedPromptset": "로더 산출물",
    "LoadedSkill": "로더 산출물 (임의 타입 포함)",
    "LoadedSkillset": "로더 산출물",
    "ParsedModuleRef": "ref 문자열을 파싱한 내부 값",
    "ModulePath": "레지스트리가 만든 내부 값",
    # 미결 — 이슈 #404 로 분리됨. 결정이 나면 forbid 를 켜고 여기서 뺀다
    "LatestRequest": "미결 (#404): Memory Service 요청 본문",
    "ReadRequest": "미결 (#404): Memory Service 요청 본문",
    "SearchRequest": "미결 (#404): Memory Service 요청 본문",
    "DraftReviewState": "미결 (#404): 그래프 state 스키마",
    "FeedMonitorState": "미결 (#404): 그래프 state 스키마",
    "MaintenanceState": "미결 (#404): 그래프 state 스키마",
    "PermissionsState": "미결 (#404): 그래프 state 스키마",
    "ResearchState": "미결 (#404): 그래프 state 스키마",
}
"""forbid 가 아니어도 되는 모델 — 이유가 없으면 여기 넣지 않는다.

새 모델이 생기면 forbid 를 켜거나 이 목록에 이유와 함께 넣는 결정을 강제한다."""


@cache
def _all_models() -> dict[str, type[BaseModel]]:
    models: dict[str, type[BaseModel]] = {}
    for info in pkgutil.walk_packages(aaestro.__path__, "aaestro."):
        if info.name in SKIPPED_MODULES:
            continue
        module = importlib.import_module(info.name)
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, BaseModel) and cls is not BaseModel and cls.__module__ == info.name:
                models[f"{info.name}.{cls.__name__}"] = cls
    return models


def test_models_are_discovered_across_the_package():
    names = set(_all_models())
    assert {
        "aaestro.core.manifest.AgentManifest",
        "aaestro.config.AaestroConfig",
        "aaestro.orchestrator.control.DeployRequest",
        "aaestro.modules.memoryset.MemorysetSpec",
    } <= names
    assert len(names) > 100


@pytest.mark.parametrize("qualified", sorted(_all_models()))
def test_every_model_forbids_extra_keys_unless_listed(qualified: str):
    model = _all_models()[qualified]
    if model.__name__ in NOT_FORBIDDEN:
        assert model.model_config.get("extra") != "forbid", (
            f"{model.__name__} is forbid now — remove it from NOT_FORBIDDEN"
        )
        return
    assert model.model_config.get("extra") == "forbid", (
        f"{qualified}: set extra='forbid' or list it in NOT_FORBIDDEN with a reason"
    )


def test_not_forbidden_entries_name_existing_models():
    existing = {cls.__name__ for cls in _all_models().values()}
    assert set(NOT_FORBIDDEN) <= existing


def _mcp_manifest() -> dict[str, Any]:
    return manifest_dict(
        spec={
            "mcp": {
                "servers": [
                    {
                        "name": "fs",
                        "transport": "stdio",
                        "command": ["mcp-server-filesystem", "/workspace"],
                        "allowed_tools": ["read_file"],
                    }
                ]
            },
            "memory": {"spaces": [{"ref": "memorysets/agent-longterm@0.1.0", "as": "longterm"}]},
            "runtime": {"egress": ["api.example.com"]},
        }
    )


NARROWING_TYPOS = [
    pytest.param(
        ["spec", "mcp", "servers", 0], "allowed_tools", "allowed_tool", id="allowed_tools"
    ),
    pytest.param(["spec", "runtime"], "egress", "egres", id="egress"),
    pytest.param(["spec", "memory", "spaces", 0], None, "mod", id="memory_mode"),
]


@pytest.mark.parametrize(("path", "good", "typo"), NARROWING_TYPOS)
def test_manifest_narrowing_declaration_typo_is_rejected(
    path: list[str | int], good: str | None, typo: str
):
    doc = _mcp_manifest()
    node: Any = doc
    for step in path:
        node = node[step]
    value: Any = "ro" if good is None else node.pop(good)
    node[typo] = value

    with pytest.raises(ValidationError) as exc_info:
        AgentManifest.model_validate(doc)

    errors = exc_info.value.errors()
    assert any(e["type"] == "extra_forbidden" and e["loc"][-1] == typo for e in errors)
    # 에러가 키의 경로를 말해야 오타를 찾을 수 있다
    assert any(tuple(e["loc"][: len(path)]) == tuple(path) for e in errors)


def test_manifest_with_correct_narrowing_declarations_is_valid():
    AgentManifest.model_validate(_mcp_manifest())


def test_manifest_unknown_top_level_key_is_rejected():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AgentManifest.model_validate(_with(manifest_dict(), [], "decision", {}))


def test_manifest_spec_decision_is_explicitly_rejected():
    """규약에는 있으나 v0.1 스키마가 아직 받지 않는 선언 — 조용히 무시되지 않는다."""
    doc = _with(manifest_dict(), ["spec"], "decision", {"provider": "jev"})
    with pytest.raises(ValidationError) as exc_info:
        AgentManifest.model_validate(doc)
    assert exc_info.value.errors()[0]["loc"] == ("spec", "decision")


def test_group_unknown_key_is_rejected():
    doc = {
        "apiVersion": "aaestro/v1",
        "kind": "Group",
        "metadata": {"name": "research", "version": "0.1.0"},
        "spec": {"quotas": {"cpu": "2.0"}, "secret": ["KEY"]},
    }
    with pytest.raises(ValidationError) as exc_info:
        GroupManifest.model_validate(doc)
    assert exc_info.value.errors()[0]["loc"] == ("spec", "secret")


def test_graph_unknown_key_is_rejected():
    with pytest.raises(ValidationError) as exc_info:
        GraphTopology.model_validate(
            _with(mission_dict(), ["spec", "nodes", 0], "decision", {"question": "x.y"})
        )
    assert exc_info.value.errors()[0]["loc"] == ("spec", "nodes", 0, "decision")


def test_graph_unknown_connection_key_is_rejected():
    doc = mission_dict(connections=[{"caller": "planner", "callee": "researcher", "calle": "x"}])
    with pytest.raises(ValidationError) as exc_info:
        GraphTopology.model_validate(doc)
    assert exc_info.value.errors()[0]["loc"][-1] == "calle"


def test_module_manifest_models_reject_an_unknown_spec_key_with_its_path():
    """모듈 선언 종류마다 spec 의 모르는 키가 그 경로와 함께 거절된다."""
    for model, doc in (
        (SkillsetManifest, _first_doc("Skillset")),
        (PromptsetManifest, _first_doc("Promptset")),
        (MemorysetManifest, _first_doc("Memoryset")),
    ):
        with pytest.raises(ValidationError) as exc_info:
            model.model_validate(_with(doc, ["spec"], "bogus_key", 1))
        assert exc_info.value.errors()[0]["loc"] == ("spec", "bogus_key")


def test_repository_declarations_are_discovered():
    kinds = {doc["kind"] for doc in _declaration_documents().values()}
    assert kinds == set(KIND_MODELS)


@pytest.mark.parametrize("path", _declaration_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_repository_declaration_passes_strict_validation(path: Path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    KIND_MODELS[doc["kind"]].model_validate(doc)


# --- 조회 응답 왕복 -------------------------------------------------------------------------


@pytest.mark.parametrize("path", _declaration_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_declaration_dump_round_trips_through_validation(path: Path):
    """GET 응답 (필드명 키) 의 UI 가 일부를 그대로 PUT 해도 거절되지 않는다.

    별칭이 실제로 갈리는 곳은 그래프 (``from``/``to``, ``schema``/``fields``) 와 memory space
    (``as``) 다 — populate_by_name 이 빠지면 필드명 입력이 모르는 키로 거절된다.
    """
    doc = _declaration_documents()[path]
    model = KIND_MODELS[doc["kind"]]
    loaded = model.model_validate(doc)
    dumped = loaded.model_dump(mode="json")
    dumped["apiVersion"] = dumped.pop("api_version")

    assert model.model_validate(dumped) == loaded


# --- 로더 경계: 모듈 선언은 MOD_003 으로 거절된다 -------------------------------------------


@pytest.mark.parametrize(
    ("ref", "rel", "loader_cls"),
    [
        ("skillsets/web-search@0.2.0", "skillsets/web-search/0.2.0/skillset.yaml", SkillsetLoader),
        (
            "promptsets/researcher@0.1.0",
            "promptsets/researcher/0.1.0/promptset.yaml",
            PromptsetLoader,
        ),
        (
            "memorysets/agent-longterm@0.1.0",
            "memorysets/agent-longterm/0.1.0/memoryset.yaml",
            MemorysetLoader,
        ),
    ],
)
def test_module_loader_unknown_key_raises_mod_003(
    tmp_path: Path, ref: str, rel: str, loader_cls: Any
):
    src = REPO_ROOT / "tests" / "fixtures" / "modules" / "modules"
    dst = tmp_path / "modules"
    shutil.copytree(src, dst)
    target = dst / rel
    doc = yaml.safe_load(target.read_text(encoding="utf-8"))
    doc["spec"]["bogus_key"] = True
    target.write_text(yaml.safe_dump(doc), encoding="utf-8")

    loader = loader_cls(ModuleRegistry.under(tmp_path))
    with pytest.raises(AaestroError) as exc_info:
        loader.load(ref)

    assert exc_info.value.code == "MOD_003"
    assert any(e["field"].endswith("bogus_key") for e in exc_info.value.details["errors"])


def test_catalog_problem_carries_the_field_list_without_values(tmp_path: Path):
    root = _repo_copy(tmp_path)
    _edit(
        root / "agents/echo/manifest.yaml",
        lambda d: d["spec"]["runtime"].update({"env": {"API_KEY": MARKER}}),
    )

    problem = next(p for p in Catalog.under(root).agents().problems if "echo" in p.path)

    assert [e["field"] for e in problem.errors] == ["spec.runtime.env"]
    assert MARKER not in json.dumps([problem.message, *problem.errors])


# --- 배포 검증은 모듈 선언도 스키마로 읽는다 --------------------------------------------------


def _findings(root: Path) -> list[Any]:
    return list(Author(catalog=Catalog.under(root)).validate().findings)


def test_deploy_validation_of_the_unmodified_repository_copy_has_no_catalog_findings(
    tmp_path: Path,
):
    assert [f for f in _findings(_repo_copy(tmp_path)) if f.check == "catalog"] == []


@pytest.mark.parametrize(
    ("rel", "mutate", "field"),
    [
        pytest.param(
            "modules/memorysets/agent-longterm/0.1.0/memoryset.yaml",
            lambda d: (
                d["spec"]["retention"].pop("ttl_days"),
                d["spec"]["retention"].update({"ttl_day": MARKER}),
            ),
            "spec.retention.ttl_day",
            id="memoryset",
        ),
        pytest.param(
            "modules/skillsets/web-search/0.2.0/skillset.yaml",
            lambda d: d["spec"].update({"bogus": MARKER}),
            "spec.bogus",
            id="skillset",
        ),
        pytest.param(
            "modules/promptsets/researcher/0.1.0/promptset.yaml",
            lambda d: d["spec"].update({"bogus": MARKER}),
            "spec.bogus",
            id="promptset",
        ),
    ],
)
def test_deploy_validation_rejects_a_misspelled_module_key_with_its_path(
    tmp_path: Path, rel: str, mutate: Any, field: str
):
    root = _repo_copy(tmp_path)
    assert (root / rel).exists(), f"{rel} is not part of the repository"
    _edit(root / rel, mutate)

    findings = [
        f
        for f in _findings(root)
        if f.check == "catalog" and rel.split("/")[2] in f.details["path"]
    ]

    assert findings
    assert findings[0].code == "MOD_003"
    assert field in [e["field"] for e in findings[0].details["errors"]]
    assert MARKER not in json.dumps([f.details for f in findings], default=str)


# --- CLI: validate 는 어느 파일의 어느 키인지까지 말한다 -------------------------------------


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_cli_validate_reports_the_file_and_the_offending_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], as_json: bool
):
    root = _repo_copy(tmp_path)
    skillset = root / "modules/skillsets/web-search/0.2.0/skillset.yaml"
    _edit(skillset, lambda d: d["spec"].update({"bogus": MARKER}))
    manifest = root / "agents/echo/manifest.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "metadata:\n  name: echo\n", encoding="utf-8"
    )

    code = cli_main(["--root", str(root), *(["--json"] if as_json else []), "validate"])

    out = capsys.readouterr().out
    assert code != 0
    assert str(skillset.parent) in out
    assert "spec.bogus: Extra inputs are not permitted" in out
    assert "duplicate key 'metadata'" in out  # 읽기 실패의 사유 (키 이름 · 줄)
    assert str(manifest) in out
    assert MARKER not in out


def test_catalog_problem_carries_the_read_failure_reason(tmp_path: Path):
    root = _repo_copy(tmp_path)
    path = root / "agents/echo/manifest.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "kind: Agent\n", encoding="utf-8")

    problem = next(p for p in Catalog.under(root).agents().problems if "echo" in p.path)

    line = len(path.read_text(encoding="utf-8").splitlines())
    assert problem.reason == f"duplicate key 'kind' at line {line}"
