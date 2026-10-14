"""Skillpack as a module type — registry, manifest, config, deploy validation, CLI, API, agentd.

타입 하나가 닿는 자리를 한 파일에서 본다. 로더 자체의 규칙은 `test_skillpack.py`.

cli · agentd · API · config 테스트가 소스 구조를 미러링한 자리 (`tests/unit/cli/` 등) 가 아니라
여기 있는 이유: 그 파일들을 아직 머지되지 않은 다른 PR 들이 고치고 있어 충돌을 피한다.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import structlog
import yaml
from pydantic import ValidationError
from structlog.testing import capture_logs

from aaestro.agentd.__main__ import ECHO_EXECUTOR, EXECUTOR_ENV, build_executor
from aaestro.catalog import Catalog
from aaestro.cli.main import EXIT_FAILED, EXIT_OK, main
from aaestro.config import AaestroConfig
from aaestro.core.errors import AaestroError
from aaestro.core.manifest import AgentManifest, ParsedModuleRef
from aaestro.modules.registry import ModuleRegistry, RegistryRoots
from aaestro.orchestrator.control import create_app
from aaestro.orchestrator.runstore import InMemoryRunStore
from tests.fixtures.builders import make_manifest

REPO_ROOT = Path(__file__).resolve().parents[3]
REF = "skillpacks/docs@1.0.0"
SKILL_MD = "---\nname: {name}\ndescription: Does a thing. Use when needed.\n---\n\n# Body\n"
WARNING = "skillpacks declared but not loaded by this executor"


def agent_doc(skillpacks: list[str]) -> dict:
    return {
        "apiVersion": "aaestro/v1",
        "kind": "Agent",
        "metadata": {"name": "solo", "version": "0.1.0"},
        "spec": {
            "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
            "promptset": {"ref": "promptsets/solo@0.1.0"},
            "skillpacks": [{"ref": ref} for ref in skillpacks],
        },
    }


def write_pack(base: Path, *, skill_name: str = "notes", extra_lines: int = 0) -> Path:
    root = base / "modules" / "skillpacks" / "docs" / "1.0.0"
    (root / "skills" / "notes").mkdir(parents=True)
    (root / "skillpack.yaml").write_text(
        "apiVersion: aaestro/v1\nkind: Skillpack\nmetadata:\n  name: docs\n  version: 1.0.0\n"
    )
    (root / "skills" / "notes" / "SKILL.md").write_text(
        SKILL_MD.format(name=skill_name) + "line\n" * extra_lines
    )
    return root


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """에이전트 하나 · 그래프 하나 — 에이전트가 skillpack 을 선언한다."""
    for directory in ("agents/solo", "graphs", "groups", "modules/promptsets/solo/0.1.0"):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / "modules" / "promptsets" / "solo" / "0.1.0" / "promptset.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "aaestro/v1",
                "kind": "Promptset",
                "metadata": {"name": "solo", "version": "0.1.0"},
                "spec": {
                    "engine": "jinja2",
                    "templates": {
                        "default": {"file": "t.j2"},
                        "solo": {"file": "t.j2"},
                        "system": {"file": "t.j2"},
                    },
                },
            }
        )
    )
    (tmp_path / "agents" / "solo" / "manifest.yaml").write_text(yaml.safe_dump(agent_doc([REF])))
    (tmp_path / "groups" / "global.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "aaestro/v1",
                "kind": "Group",
                "metadata": {"name": "global", "version": "0.1.0"},
                "spec": {"secrets": ["ANTHROPIC_API_KEY"]},
            }
        )
    )
    (tmp_path / "graphs" / "solo-graph.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "aaestro/v1",
                "kind": "Graph",
                "metadata": {"name": "solo-graph", "version": "1.0.0"},
                "spec": {
                    "mode": "mission",
                    "goal": "single node",
                    "state": {"schema": "aaestro.graphs.schemas:ResearchState"},
                    "nodes": [{"id": "solo", "agent": "agents/solo@0.1.0"}],
                    "edges": [{"from": "START", "to": "solo"}, {"from": "solo", "to": "END"}],
                },
            }
        )
    )
    return tmp_path


# --- ref · registry · config -------------------------------------------------------


def test_parse_skillpack_ref_valid():
    parsed = ParsedModuleRef.parse(REF)

    assert (parsed.type, parsed.name, parsed.version) == ("skillpacks", "docs", "1.0.0")


@pytest.mark.parametrize(
    "ref", ["skillpacks/docs@latest", "skillpacks/docs", "skillpack/docs@1.0.0"]
)
def test_parse_skillpack_ref_invalid_raises(ref):
    with pytest.raises(ValueError, match="invalid module ref"):
        ParsedModuleRef.parse(ref)


def test_resolve_skillpack_ref_points_at_its_declaration(tmp_path):
    root = write_pack(tmp_path)

    path = ModuleRegistry.under(tmp_path).resolve(REF)

    assert path.root == root
    assert path.manifest_file == root / "skillpack.yaml"


def test_load_document_skillpack_kind_mismatch_raises_mod_003(tmp_path):
    root = write_pack(tmp_path)
    (root / "skillpack.yaml").write_text(
        "apiVersion: aaestro/v1\nkind: Skillset\nmetadata:\n  name: docs\n  version: 1.0.0\n"
    )

    with pytest.raises(AaestroError) as exc_info:
        ModuleRegistry.under(tmp_path).load_document(REF)

    assert exc_info.value.code == "MOD_003"


def test_resolve_skillpack_without_a_configured_root_raises_mod_001(tmp_path):
    """루트를 직접 조립한 레지스트리 — 조용히 다른 루트를 쓰지 않는다."""
    roots = RegistryRoots(
        skillsets=tmp_path,
        promptsets=tmp_path,
        memorysets=tmp_path,
        agents=tmp_path,
        graphs=tmp_path,
        groups=tmp_path,
    )

    with pytest.raises(AaestroError) as exc_info:
        ModuleRegistry(roots).resolve(REF)

    assert exc_info.value.code == "MOD_001"
    assert Catalog(roots=roots).modules("skillpacks").items == {}


def test_config_declares_a_skillpacks_root(tmp_path):
    roots = AaestroConfig().registry.roots

    assert roots.skillpacks == "./modules/skillpacks"
    catalog = Catalog.from_config(roots, base=tmp_path)
    assert catalog.roots.skillpacks == (tmp_path / "modules" / "skillpacks").resolve()


@pytest.mark.parametrize("environment", ["dev", "staging", "prod"])
def test_environment_configs_declare_the_skillpacks_root(environment):
    document = yaml.safe_load((REPO_ROOT / "configs" / f"{environment}.yaml").read_text())

    assert document["registry"]["roots"]["skillpacks"] == "./modules/skillpacks"


# --- manifest ----------------------------------------------------------------------


def test_manifest_accepts_skillpack_refs():
    manifest = AgentManifest.model_validate(agent_doc([REF]))

    assert [item.ref for item in manifest.spec.skillpacks] == [REF]


def test_manifest_skillpacks_default_to_none_declared():
    assert make_manifest().spec.skillpacks == ()


@pytest.mark.parametrize(
    "ref", ["skillsets/web-search@0.2.0", "skillpacks/docs@latest", "skillpacks/Docs@1.0.0"]
)
def test_manifest_skillpacks_field_rejects_other_refs(ref):
    with pytest.raises(ValidationError):
        AgentManifest.model_validate(agent_doc([ref]))


@pytest.mark.parametrize(
    "refs",
    [
        [REF, REF],
        [REF, "skillpacks/docs@1.1.0"],
        ["skillpacks/other@1.0.0", REF, "skillpacks/docs@2.0.0"],
    ],
)
def test_manifest_rejects_the_same_skillpack_twice(refs):
    """같은 ref 의 중복도, 같은 팩의 다른 버전도 — 같은 이름의 skill 이 두 벌 생긴다."""
    with pytest.raises(ValidationError, match=r"duplicate skillpack: \['docs'\]"):
        AgentManifest.model_validate(agent_doc(refs))


def test_manifest_accepts_different_skillpacks():
    manifest = AgentManifest.model_validate(agent_doc([REF, "skillpacks/docs-extra@1.0.0"]))

    assert len(manifest.spec.skillpacks) == 2


def test_manifest_skillsets_field_rejects_a_skillpack_ref():
    document = agent_doc([])
    document["spec"]["skillsets"] = [{"ref": REF}]

    with pytest.raises(ValidationError, match="skillset ref"):
        AgentManifest.model_validate(document)


# --- 배포 검증 · CLI ----------------------------------------------------------------


def test_validate_passes_when_the_declared_skillpack_is_published(workspace, capsys):
    write_pack(workspace)

    assert main(["--root", str(workspace), "validate"]) == EXIT_OK
    assert "warnings" not in capsys.readouterr().out


def test_validate_fails_when_the_declared_skillpack_is_missing(workspace, capsys):
    assert main(["--root", str(workspace), "validate"]) == EXIT_FAILED

    output = capsys.readouterr().out
    assert "module_refs" in output
    assert REF in output


def test_validate_fails_when_a_skill_in_the_pack_is_invalid(workspace, capsys):
    """선언은 맞고 담긴 skill 이 표준을 어긴 팩 — 어느 skill 의 어느 필드인지 나온다."""
    write_pack(workspace, skill_name="Notes")

    assert main(["--root", str(workspace), "validate"]) == EXIT_FAILED

    output = capsys.readouterr().out
    assert "skill 'notes'" in output
    assert "name: " in output


def test_validate_reports_a_long_skill_md_as_a_warning_not_a_failure(workspace, capsys):
    write_pack(workspace, extra_lines=500)

    assert main(["--root", str(workspace), "--json", "validate"]) == EXIT_OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["failures"] == []
    assert len(payload["warnings"]) == 1
    assert payload["warnings"][0].startswith(f"{REF}: skill 'notes'")


def test_deploy_reports_warnings_like_validate(workspace, capsys):
    write_pack(workspace, extra_lines=500)
    graph = str(workspace / "graphs" / "solo-graph.yaml")

    assert main(["--root", str(workspace), "--json", "deploy", graph]) == EXIT_OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["warnings"][0].startswith(f"{REF}: skill 'notes'")


def test_deploy_omits_warnings_when_there_are_none(workspace, capsys):
    write_pack(workspace)
    graph = str(workspace / "graphs" / "solo-graph.yaml")

    assert main(["--root", str(workspace), "--json", "deploy", graph]) == EXIT_OK
    assert "warnings" not in json.loads(capsys.readouterr().out)


def test_status_lists_the_skillpack_ref(workspace, capsys):
    write_pack(workspace)

    assert main(["--root", str(workspace), "--json", "status"]) == EXIT_OK
    assert REF in json.loads(capsys.readouterr().out)["modules"]


# --- catalog API -------------------------------------------------------------------


async def get(base: Path, path: str) -> httpx.Response:
    app = create_app(InMemoryRunStore(), catalog=Catalog.under(base))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://control.test") as api:
        return await api.get(path)


async def test_api_lists_skillpacks_with_versions(workspace):
    write_pack(workspace)

    body = (await get(workspace, "/v1/modules/skillpacks")).json()

    assert body == {
        "items": [{"name": "docs", "versions": ["1.0.0"]}],
        "problems": [],
        "warnings": [],
    }


async def test_api_returns_the_skillpack_declaration(workspace):
    write_pack(workspace)

    body = (await get(workspace, "/v1/modules/skillpacks/docs/1.0.0")).json()

    assert body == {
        "apiVersion": "aaestro/v1",
        "kind": "Skillpack",
        "metadata": {"name": "docs", "version": "1.0.0"},
        "skills": [{"name": "notes", "files": 1}],
    }


async def test_api_single_skillpack_agrees_with_the_listing_about_a_broken_pack(workspace):
    """목록이 게시하지 않는 팩을 단건 조회가 200 으로 돌려주지 않는다."""
    write_pack(workspace, skill_name="other")

    listed = (await get(workspace, "/v1/modules/skillpacks")).json()
    single = await get(workspace, "/v1/modules/skillpacks/docs/1.0.0")

    assert listed["items"] == []
    assert single.status_code == 400
    assert single.json()["error"]["code"] == "MOD_003"
    assert single.json()["error"]["message"] == listed["problems"][0]["message"]


async def test_api_unknown_skillpack_is_404(workspace):
    assert (await get(workspace, "/v1/modules/skillpacks/ghost/1.0.0")).status_code == 404


async def test_api_reports_an_invalid_pack_under_problems(workspace):
    write_pack(workspace, skill_name="other")

    body = (await get(workspace, "/v1/modules/skillpacks")).json()

    assert body["items"] == []
    assert body["problems"][0]["code"] == "MOD_003"
    assert "skill 'notes'" in body["problems"][0]["message"]


async def test_api_reports_warnings_apart_from_problems(workspace):
    write_pack(workspace, extra_lines=500)

    body = (await get(workspace, "/v1/modules/skillpacks")).json()

    assert body["items"] == [{"name": "docs", "versions": ["1.0.0"]}]
    assert body["problems"] == []
    assert len(body["warnings"]) == 1


async def test_api_lists_the_repository_example():
    body = (await get(REPO_ROOT, "/v1/modules/skillpacks")).json()

    assert {"name": "docs", "versions": ["1.0.0"]} in body["items"]
    assert body["problems"] == []


# --- agentd — 선언은 받고, 아직 쓰지 않는다는 것을 말한다 ---------------------------


@pytest.fixture
def fresh_log(monkeypatch):
    """모듈 로거를 새 프록시로 — 앞선 테스트가 `cache_logger_on_first_use` 로 설정해 두면
    이미 쓰인 프록시는 처리기가 굳어 `capture_logs` 에 잡히지 않는다 (실행 순서 의존)."""
    monkeypatch.setattr("aaestro.agentd.__main__.log", structlog.get_logger())


async def test_agentd_warns_that_declared_skillpacks_are_not_loaded(monkeypatch, fresh_log):
    """조용히 무시되는 선언이 되지 않게 — 기동은 되고 WARN 이 남는다 (#374 전까지)."""
    monkeypatch.setenv(EXECUTOR_ENV, ECHO_EXECUTOR)
    manifest = AgentManifest.model_validate(agent_doc([REF]))

    with capture_logs() as logs:
        executor = await build_executor(manifest)

    assert executor is not None
    warned = [entry for entry in logs if entry["event"] == WARNING]
    assert len(warned) == 1
    assert warned[0]["log_level"] == "warning"
    assert warned[0]["agent"] == "solo"
    assert warned[0]["skillpacks"] == [REF]


async def test_agentd_does_not_warn_without_declared_skillpacks(monkeypatch, fresh_log):
    monkeypatch.setenv(EXECUTOR_ENV, ECHO_EXECUTOR)
    manifest = AgentManifest.model_validate(agent_doc([]))

    with capture_logs() as logs:
        await build_executor(manifest)

    assert [entry for entry in logs if entry["event"] == WARNING] == []
