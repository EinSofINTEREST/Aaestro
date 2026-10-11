"""Smoke tests for the malkuth CLI.

실제 컨테이너 없이 명령 표면을 검증한다 — 이 계층의 계약은 인자 해석과
종료 코드지 오케스트레이션이 아니다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from malkuth.cli.main import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    build_parser,
    main,
    validate_root,
)
from malkuth.orchestrator.topology import GraphTopology

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """유효한 최소 저장소 — 배포 검증을 통과하는 구성."""
    (tmp_path / "agents" / "solo").mkdir(parents=True)
    (tmp_path / "groups").mkdir()
    (tmp_path / "graphs").mkdir()
    # 빈 디렉토리는 게시된 모듈이 아니다 — 문서까지 있어야 ref 가 해석된다 (#240 리뷰)
    solo = tmp_path / "modules" / "promptsets" / "solo" / "0.1.0"
    solo.mkdir(parents=True)
    (solo / "promptset.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "malkuth/v1",
                "kind": "Promptset",
                "metadata": {"name": "solo", "version": "0.1.0"},
                # 노드 id 와 같은 이름의 템플릿이 있어야 agentd 가 고를 수 있다
                # (04 호환성 규칙 3) — `default` 만 두면 run 에서 MOD_004 로 죽는다
                "spec": {
                    "engine": "jinja2",
                    "templates": {"default": {"file": "t.j2"}, "solo": {"file": "t.j2"}},
                },
            }
        ),
        encoding="utf-8",
    )

    (tmp_path / "agents" / "solo" / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "malkuth/v1",
                "kind": "Agent",
                "metadata": {"name": "solo", "version": "0.1.0"},
                "spec": {
                    "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                    "promptset": {"ref": "promptsets/solo@0.1.0"},
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "groups" / "global.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "malkuth/v1",
                "kind": "Group",
                "metadata": {"name": "global", "version": "0.1.0"},
                "spec": {"secrets": ["ANTHROPIC_API_KEY"]},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "graphs" / "solo-graph.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "malkuth/v1",
                "kind": "Graph",
                "metadata": {"name": "solo-graph", "version": "1.0.0"},
                "spec": {
                    "mode": "mission",
                    "goal": "단일 노드 실행",
                    "state": {"schema": "malkuth.graphs.schemas:ResearchState"},
                    "nodes": [{"id": "solo", "agent": "agents/solo@0.1.0"}],
                    "edges": [
                        {"from": "START", "to": "solo"},
                        {"from": "solo", "to": "END"},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def run_cli(argv: list[str]) -> int:
    return main(argv)


# --- 파서 ---------------------------------------------------------------------


def test_parser_requires_a_subcommand(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


@pytest.mark.parametrize("command", ["deploy", "validate", "status", "config", "check"])
def test_every_command_is_registered(command):
    assert command in build_parser().format_help()


def test_port_range_is_parsed():
    args = build_parser().parse_args(["validate", "--a2a-port-range", "9100-9199"])

    assert args.a2a_port_range == (9100, 9199)


# --- status -------------------------------------------------------------------


def test_status_lists_declared_artifacts(workspace, capsys):
    assert run_cli(["--root", str(workspace), "status"]) == EXIT_OK

    output = capsys.readouterr().out
    assert "solo" in output
    assert "solo-graph" in output


def test_status_emits_json(workspace, capsys):
    import json

    run_cli(["--root", str(workspace), "--json", "status"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["agents"] == ["solo"]


# --- validate / deploy --------------------------------------------------------


def test_validate_passes_on_a_valid_workspace(workspace, capsys):
    assert run_cli(["--root", str(workspace), "validate"]) == EXIT_OK
    assert "status: ok" in capsys.readouterr().out


def test_validate_fails_on_a_dangling_ref(workspace, capsys):
    """게시되지 않은 모듈을 참조하면 배포 전에 막혀야 한다."""
    manifest = workspace / "agents" / "solo" / "manifest.yaml"
    document = yaml.safe_load(manifest.read_text())
    document["spec"]["promptset"]["ref"] = "promptsets/absent@9.9.9"
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")

    assert run_cli(["--root", str(workspace), "validate"]) == EXIT_FAILED
    assert "module_refs" in capsys.readouterr().out


def test_deploy_validates_a_single_graph(workspace, capsys):
    graph = str(workspace / "graphs" / "solo-graph.yaml")

    assert run_cli(["--root", str(workspace), "deploy", graph]) == EXIT_OK


def test_deploy_reports_failures_without_starting_anything(workspace, capsys):
    """검증 실패 시 아무것도 기동하지 않는다 — 이 명령의 요점이다."""
    manifest = workspace / "agents" / "solo" / "manifest.yaml"
    document = yaml.safe_load(manifest.read_text())
    document["metadata"]["group"] = "absent"
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")
    graph = str(workspace / "graphs" / "solo-graph.yaml")

    assert run_cli(["--root", str(workspace), "deploy", graph]) == EXIT_FAILED
    assert "groups" in capsys.readouterr().out


def with_model_layers(workspace: Path) -> str:
    """manifest 에 fallback, 그래프 노드에 override — 해석 결과에 두 계층이 다 보이게."""
    manifest = workspace / "agents" / "solo" / "manifest.yaml"
    document = yaml.safe_load(manifest.read_text())
    document["spec"]["model"]["fallbacks"] = [
        {"provider": "anthropic", "name": "claude-haiku-4-5", "on": ["rate_limit"]}
    ]
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")
    graph = workspace / "graphs" / "solo-graph.yaml"
    document = yaml.safe_load(graph.read_text())
    document["spec"]["nodes"][0]["model"] = {"name": "claude-opus-5"}
    graph.write_text(yaml.safe_dump(document), encoding="utf-8")
    return str(graph)


def test_validate_shows_each_node_model_from_the_manifest(workspace, capsys):
    assert run_cli(["--root", str(workspace), "validate"]) == EXIT_OK

    out = capsys.readouterr().out
    assert "models:\n  - solo-graph/solo: anthropic/claude-sonnet-5 (manifest)" in out


def test_deploy_shows_the_layered_model_and_its_fallbacks(workspace, capsys):
    graph = with_model_layers(workspace)

    assert run_cli(["--root", str(workspace), "deploy", graph]) == EXIT_OK

    out = capsys.readouterr().out
    assert (
        "  - solo-graph/solo: anthropic/claude-opus-5 → anthropic/claude-haiku-4-5 (node override)"
        in out
    )


def test_validate_json_lists_models_as_structured_entries(workspace, capsys):
    import json

    with_model_layers(workspace)

    assert run_cli(["--root", str(workspace), "--json", "validate"]) == EXIT_OK

    assert json.loads(capsys.readouterr().out)["models"] == [
        {
            "graph": "solo-graph",
            "node_id": "solo",
            "agent": "agents/solo@0.1.0",
            "provider": "anthropic",
            "name": "claude-opus-5",
            "fallbacks": [
                {"provider": "anthropic", "name": "claude-haiku-4-5", "on": ["rate_limit"]}
            ],
            "source": "node",
        }
    ]


def test_a_node_whose_manifest_is_missing_is_left_to_the_findings(workspace, capsys):
    (workspace / "agents" / "solo" / "manifest.yaml").unlink()

    assert run_cli(["--root", str(workspace), "validate"]) == EXIT_FAILED

    out = capsys.readouterr().out
    assert "solo-graph/solo:" not in out


def test_run_hands_each_node_task_its_resolved_model(workspace, monkeypatch):
    from malkuth.core.agent import TaskResult
    from malkuth.runtime.nodes import ControlNodeRuntime

    graph = with_model_layers(workspace)
    seen = []

    async def invoke(self, node, task):
        seen.append(task.config.model)
        return TaskResult.completed(task, output={})

    monkeypatch.setattr(ControlNodeRuntime, "invoke", invoke)

    run_cli(["--root", str(workspace), "run", graph, "--input", '{"query": "q"}'])

    assert [m.name for m in seen] == ["claude-opus-5"]
    assert [f.name for f in seen[0].fallbacks] == ["claude-haiku-4-5"]


# --- config -------------------------------------------------------------------


def test_config_prints_the_resolved_settings(capsys):
    assert run_cli(["config", "dev", "--config-dir", str(REPO_ROOT / "configs")]) == EXIT_OK

    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["orchestrator"]["checkpointer"] == "memory"


def test_unknown_environment_reports_a_structured_error(tmp_path, capsys):
    """스택 트레이스를 그대로 뱉으면 운영자가 원인을 찾기 어렵다."""
    assert run_cli(["config", "absent", "--config-dir", str(tmp_path)]) == EXIT_FAILED
    assert "CFG_001" in capsys.readouterr().err


# --- check --------------------------------------------------------------------


def test_check_reports_no_discrepancies(tmp_path, capsys):
    state = tmp_path / "state.yaml"
    state.write_text(yaml.safe_dump({"runs": ["r1"], "checkpoints": ["r1"]}), encoding="utf-8")

    assert run_cli(["check", str(state)]) == EXIT_OK


def test_check_reports_orphans(tmp_path, capsys):
    state = tmp_path / "state.yaml"
    state.write_text(
        yaml.safe_dump({"runs": ["r1"], "checkpoints": [], "running": ["ghost"]}),
        encoding="utf-8",
    )

    assert run_cli(["check", str(state)]) == EXIT_FAILED

    output = capsys.readouterr().out
    assert "run_without_checkpoint" in output
    assert "ghost_container" in output


def test_check_rejects_a_non_mapping_state(tmp_path, capsys):
    state = tmp_path / "state.yaml"
    state.write_text("- a\n- b\n", encoding="utf-8")

    assert run_cli(["check", str(state)]) == EXIT_FAILED
    assert "CFG_001" in capsys.readouterr().err


# --- 실제 저장소 ---------------------------------------------------------------


def test_repository_validates_end_to_end(capsys):
    """저장소 전체가 10항목을 통과해야 한다 — CLI 가 만든 예제만 통과하면
    아무것도 증명하지 못한다."""
    assert run_cli(["--root", str(REPO_ROOT), "validate"]) == EXIT_OK


# --- --providers ----------------------------------------------------------------


@pytest.fixture
def with_openai_fallback(workspace: Path, monkeypatch) -> Path:
    """프록시 upstream 이 아닐 수 있는 provider 로 넘어가는 fallback 을 가진 저장소."""
    from malkuth.access import baselines

    monkeypatch.setitem(baselines.PROVIDER_HOSTS, "openai-compat", "api.openai.com")
    manifest = workspace / "agents" / "solo" / "manifest.yaml"
    document = yaml.safe_load(manifest.read_text())
    document["spec"]["model"]["fallbacks"] = [
        {"provider": "openai-compat", "name": "gpt-5", "on": ["server_error"]}
    ]
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")
    return workspace


@pytest.mark.parametrize("command", ["validate", "deploy"])
def test_providers_rejects_a_fallback_the_proxy_does_not_terminate(
    with_openai_fallback, capsys, command
):
    extra = (
        [] if command == "validate" else [str(with_openai_fallback / "graphs" / "solo-graph.yaml")]
    )
    argv = ["--root", str(with_openai_fallback), command, *extra]

    assert run_cli([*argv, "--providers", "anthropic"]) == EXIT_FAILED
    assert "not an egress proxy upstream" in capsys.readouterr().out
    assert run_cli([*argv, "--providers", "anthropic,openai-compat"]) == EXIT_OK
    # 플래그가 없으면 프록시도 없다 — upstream 판정을 하지 않는다
    assert run_cli(argv) == EXIT_OK


def test_run_applies_the_same_providers_check(with_openai_fallback, capsys):
    graph = str(with_openai_fallback / "graphs" / "solo-graph.yaml")

    code = run_cli(["--root", str(with_openai_fallback), "run", graph, "--providers", "anthropic"])

    assert code == EXIT_FAILED
    assert "not an egress proxy upstream" in capsys.readouterr().out


def test_an_empty_providers_list_is_a_usage_error():
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["validate", "--providers", " , "])

    assert exc_info.value.code == EXIT_USAGE


# --- run ----------------------------------------------------------------------


def test_run_is_registered():
    assert "run" in build_parser().format_help()


def test_run_rejects_an_invalid_deployment(workspace, capsys):
    """검증에 실패한 그래프를 굴리면 노드 실행 중에야 실패한다."""
    manifest = workspace / "agents" / "solo" / "manifest.yaml"
    document = yaml.safe_load(manifest.read_text())
    document["spec"]["promptset"]["ref"] = "promptsets/absent@9.9.9"
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")
    graph = str(workspace / "graphs" / "solo-graph.yaml")

    assert run_cli(["--root", str(workspace), "run", graph]) == EXIT_FAILED

    output = capsys.readouterr().out
    assert "rejected" in output
    assert "module_refs" in output


def test_run_without_initial_state_is_rejected_before_the_node(workspace, capsys):
    """필수 state 가 없으면 노드 실행 전에 막힌다 — 어느 필드인지도 알려준다."""
    graph = str(workspace / "graphs" / "solo-graph.yaml")

    assert run_cli(["--root", str(workspace), "run", graph]) == EXIT_FAILED

    assert "query" in capsys.readouterr().err


def test_run_without_agent_addresses_fails_at_the_node(workspace, capsys):
    """state 가 유효하면 제출까지 가고, 에이전트가 없어 노드에서 실패한다."""
    graph = str(workspace / "graphs" / "solo-graph.yaml")

    exit_code = run_cli(["--root", str(workspace), "run", graph, "--input", '{"query": "q"}'])

    assert exit_code == EXIT_FAILED
    assert "failed" in capsys.readouterr().out


def test_run_closes_the_checkpointer_it_opened(workspace, monkeypatch):
    """여는 쪽이 닫는다 — 프로세스 종료에 기대면 소유자가 코드에 드러나지 않는다.

    상주 실행(service)에서는 그 기대 자체가 성립하지 않는다.
    """
    import malkuth.orchestrator.checkpoint as checkpoint_module

    closed: list[object] = []
    real = checkpoint_module.close_checkpointer

    async def recording(checkpointer):
        closed.append(checkpointer)
        await real(checkpointer)

    monkeypatch.setattr(checkpoint_module, "close_checkpointer", recording)
    graph = str(workspace / "graphs" / "solo-graph.yaml")

    # 노드는 에이전트 주소가 없어 실패하지만, 정리는 그와 무관하게 일어나야 한다
    run_cli(["--root", str(workspace), "run", graph, "--input", '{"query": "q"}'])

    assert closed, "checkpointer 를 만든 곳이 닫지 않으면 커넥션이 샌다"


def test_run_parses_agent_addresses():
    args = build_parser().parse_args(
        ["run", "g.yaml", "--agent", "planner=http://a:8080", "--agent", "writer=http://b:8080"]
    )

    assert args.agent == ["planner=http://a:8080", "writer=http://b:8080"]


def test_run_and_validate_agree_on_the_same_repository(workspace):
    """세 명령이 같은 입력에 다른 판정을 내리면 한 명령만 통과하는 상태가 생긴다."""
    topology = GraphTopology.model_validate(
        yaml.safe_load((workspace / "graphs" / "solo-graph.yaml").read_text())
    )

    assert validate_root(workspace, [topology]).ok


def test_global_secrets_resolve_for_every_command():
    """run 이 global_secrets 를 빠뜨리면 deploy 는 통과하는 배포를 거부한다."""
    topologies = [
        GraphTopology.model_validate(
            yaml.safe_load((REPO_ROOT / "graphs" / f"{name}.yaml").read_text())
        )
        for name in ("research-pipeline",)
    ]

    assert validate_root(REPO_ROOT, topologies).ok


# --- stdout / stderr 분리 -------------------------------------------------------


def test_json_output_is_parseable(workspace, capsys):
    """로그가 stdout 에 섞이면 스크립트가 --json 을 파싱할 수 없다."""
    import json

    run_cli(["--root", str(workspace), "--json", "validate"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)  # 섞여 있으면 여기서 터진다
    assert payload["status"] == "ok"


def test_logs_do_not_pollute_stdout(workspace, capsys):
    """검증 실패 로그가 나가는 경로에서도 stdout 은 순수해야 한다."""
    import json

    manifest = workspace / "agents" / "solo" / "manifest.yaml"
    document = yaml.safe_load(manifest.read_text())
    document["spec"]["promptset"]["ref"] = "promptsets/absent@9.9.9"
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")

    run_cli(["--root", str(workspace), "--json", "validate"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "failed"


def test_package_entry_point_runs_without_warnings():
    """`python -m malkuth.cli` 가 runpy 이중 import 경고를 내지 않아야 한다.

    CLI 가 설치되지 않은 환경(smoke.sh fallback)에서 그 경고가 그대로 노출된다.
    """
    import subprocess
    import sys

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-W", "error::RuntimeWarning", "-m", "malkuth.cli", "status"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        # timeout 이 없으면 status 의 회귀나 I/O 대기가 테스트 실행 전체를 멈춘다
        timeout=60,
    )

    # -W error::RuntimeWarning 이 경고를 예외로 승격하므로 종료 코드가 곧 판정이다.
    # 문자열 매칭을 더하면 새로운 경고 종류를 놓치는 검사로 되돌아간다
    assert result.returncode == 0, result.stderr


# --- CLI 가 Control API 토큰을 싣는가 -------------------------------------------


def clients_for(argv: list[str], monkeypatch, env: str | None = None):
    """`malkuth run` 인자로 만들어지는 Control API 클라이언트."""
    from malkuth.cli.main import _control_clients
    from malkuth.runtime.tokens import AGENT_TOKEN_ENV

    if env is None:
        monkeypatch.delenv(AGENT_TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(AGENT_TOKEN_ENV, env)
    return _control_clients(build_parser().parse_args(argv))


def test_run_carries_the_agent_token(monkeypatch):
    """토큰을 빠뜨리면 인증이 켜진 스택에서 모든 노드 호출이 401 이 된다."""
    clients = clients_for(
        ["run", "g.yaml", "--agent", "planner=http://a:8080", "--agent-token", "TOK"],
        monkeypatch,
    )

    assert clients["planner"]._token == "TOK"


def test_agent_token_falls_back_to_the_environment(monkeypatch):
    """compose 가 주입하는 것과 같은 키를 본다."""
    clients = clients_for(
        ["run", "g.yaml", "--agent", "planner=http://a:8080"], monkeypatch, env="FROM-ENV"
    )

    assert clients["planner"]._token == "FROM-ENV"


def test_explicit_token_wins_over_the_environment(monkeypatch):
    clients = clients_for(
        ["run", "g.yaml", "--agent", "planner=http://a:8080", "--agent-token", "EXPLICIT"],
        monkeypatch,
        env="FROM-ENV",
    )

    assert clients["planner"]._token == "EXPLICIT"


def test_empty_environment_token_is_treated_as_unset(monkeypatch):
    """compose 의 ${VAR:-default} 와 같은 규칙 — 빈 값은 미설정이다."""
    clients = clients_for(
        ["run", "g.yaml", "--agent", "planner=http://a:8080"], monkeypatch, env=""
    )

    assert clients["planner"]._token is None


def test_every_agent_shares_the_run_token(monkeypatch):
    clients = clients_for(
        [
            "run",
            "g.yaml",
            "--agent",
            "planner=http://a:8080",
            "--agent",
            "writer=http://b:8080",
            "--agent-token",
            "SHARED",
        ],
        monkeypatch,
    )

    assert {c._token for c in clients.values()} == {"SHARED"}


# --- 깨진 선언은 검증에서 빠진 것이지 통과한 것이 아니다 (#247 리뷰) ------------------


def test_validate_fails_when_an_unreferenced_agent_is_broken(workspace):
    """참조되지 않는 깨진 에이전트가 있어도 validate 가 통과하면 저장소는 불완전한 채 배포된다."""
    (workspace / "agents" / "broken").mkdir()
    (workspace / "agents" / "broken" / "manifest.yaml").write_text(
        "kind: Agent\n", encoding="utf-8"
    )

    report = validate_root(workspace, [])

    assert not report.ok
    finding = next(f for f in report.findings if f.check == "catalog")
    assert finding.details["path"].endswith("agents/broken/manifest.yaml")


def test_validate_fails_when_a_module_directory_has_no_document(workspace):
    (workspace / "modules" / "promptsets" / "ghost" / "0.1.0").mkdir(parents=True)

    report = validate_root(workspace, [])

    assert any(f.check == "catalog" and "ghost" in f.details["path"] for f in report.findings)
