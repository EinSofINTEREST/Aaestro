"""Entry points report a broken declaration the same way — one structured line, no traceback (#386).

진입점 (agentd · control plane · Memory Service · CLI) 이 깨진 선언 · 설정을 만났을 때의 출력.
프로세스 경계를 보는 테스트라 **진짜 별도 프로세스** 를 띄운다 — 같은 프로세스에서 ``main()`` 을
부르면 로깅 설정 순서 같은 것은 보이지 않는다. 모든 입력은 설정 로드에서 실패한다 (서버는 뜨지
않는다).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from aaestro.agentd.__main__ import MANIFEST_ENV, load_manifest
from aaestro.authoring import Author
from aaestro.catalog import Catalog, load_yaml
from aaestro.config import load_config
from aaestro.core.errors import AaestroError
from aaestro.core.yamlio import NOT_UTF8
from aaestro.modules.registry import ModuleRegistry
from aaestro.modules.skillset import SkillsetLoader
from tests.fixtures.declarations import MARKER, REPO_ROOT, _edit, _repo_copy, _secret_manifest

NOT_TEXT = b"token: \xff" + MARKER.encode() + b"\n"
"""UTF-8 이 아닌 바이트 뒤에 표식 — 디코딩 에러가 바이트 조각을 싣고 나오는지 본다."""
SKILLSET = "modules/skillsets/web-search/0.2.0/skillset.yaml"
ENTRYPOINTS = {
    "agentd": "aaestro.agentd.__main__",
    "orchestrator": "aaestro.orchestrator.__main__",
    "memory": "aaestro.memory.__main__",
}
PROCESS_TIMEOUT_S = 60


def _run(args: list[str], **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, *args],
        # 메트릭 서버는 고정 포트 대신 임시 포트 (0) — 다른 테스트 · 프로세스와 부딪히지 않게
        env={**os.environ, "AAESTRO_METRICS_PORT": "0", "AAESTRO_LOG_FORMAT": "json", **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=PROCESS_TIMEOUT_S,
        cwd=REPO_ROOT,
    )


def _entrypoint(name: str, **env: str) -> subprocess.CompletedProcess[str]:
    return _run(["-c", f"from {ENTRYPOINTS[name]} import main; main()"], **env)


def _cli(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(["-m", "aaestro.cli", "--root", str(root), *args])


def _config_dir(tmp_path: Path, content: bytes) -> Path:
    (tmp_path / "dev.yaml").write_bytes(content)
    return tmp_path


def _startup_failure(name: str, tmp_path: Path, content: bytes) -> subprocess.CompletedProcess[str]:
    """깨진 입력으로 진입점을 띄운다 — agentd 는 manifest, 나머지는 설정 파일."""
    if name == "agentd":
        manifest = tmp_path / "manifest.yaml"
        manifest.write_bytes(content)
        return _entrypoint(name, **{MANIFEST_ENV: str(manifest)})
    return _entrypoint(
        name, AAESTRO_ENV="dev", AAESTRO_CONFIG_DIR=str(_config_dir(tmp_path, content))
    )


def _critical_line(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    (critical,) = [line for line in lines if line.get("level") == "critical"]
    return critical


# --- UTF-8 이 아닌 파일: 네 입구가 같은 처리 --------------------------------------------------


def _load_yaml(tmp_path: Path) -> None:
    path = tmp_path / "a.yaml"
    path.write_bytes(NOT_TEXT)
    load_yaml(path)


def _load_manifest(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_bytes(NOT_TEXT)
    load_manifest(path)


def _load_config(tmp_path: Path) -> None:
    load_config("dev", config_dir=_config_dir(tmp_path, NOT_TEXT), environ={})


def _load_module(tmp_path: Path) -> None:
    root = _repo_copy(tmp_path)
    (root / SKILLSET).write_bytes(NOT_TEXT)
    SkillsetLoader(ModuleRegistry.under(root)).load("skillsets/web-search@0.2.0")


NON_UTF8_ENTRIES = {
    "catalog.load_yaml": (_load_yaml, "CFG_001"),
    "agentd.load_manifest": (_load_manifest, "CFG_001"),
    "load_config": (_load_config, "CFG_001"),
    "registry": (_load_module, "MOD_003"),
}


@pytest.mark.parametrize("entry", sorted(NON_UTF8_ENTRIES))
def test_non_utf8_file_is_an_unreadable_file_at_every_entry(entry: str, tmp_path: Path):
    read, code = NON_UTF8_ENTRIES[entry]

    with pytest.raises(AaestroError) as exc_info:
        read(tmp_path)

    err = exc_info.value
    assert err.code == code
    assert err.details["reason"] == NOT_UTF8
    assert MARKER not in json.dumps({"m": err.message, "d": err.details}, default=str)
    # UnicodeDecodeError.object 는 파일 바이트 전체다 — 체인에도 문맥에도 남기지 않는다
    assert err.__cause__ is None
    assert err.__context__ is None


def test_deployment_validation_reports_a_non_utf8_module_instead_of_raising(tmp_path: Path):
    root = _repo_copy(tmp_path)
    (root / SKILLSET).write_bytes(NOT_TEXT)

    report = Author(catalog=Catalog.under(root)).validate()

    reasons = [finding.details.get("reason") for finding in report.findings]
    assert NOT_UTF8 in reasons


@pytest.mark.parametrize("name", sorted(ENTRYPOINTS))
def test_entrypoint_reports_a_non_utf8_file_without_a_traceback(name: str, tmp_path: Path):
    result = _startup_failure(name, tmp_path, NOT_TEXT)

    assert result.returncode == 1
    assert "Traceback" not in result.stdout + result.stderr
    assert MARKER not in result.stdout + result.stderr
    assert _critical_line(result)["details"]["reason"] == NOT_UTF8


# --- 기동 실패의 형식: 세 진입점이 같은 한 줄 --------------------------------------------------


def test_startup_failure_is_one_json_line_of_the_same_shape_at_every_entrypoint(tmp_path: Path):
    """agentd 도 로깅 설정이 manifest 로드보다 먼저다 — JSON 이고 마스킹 processor 를 거친다."""
    lines = {}
    for name in sorted(ENTRYPOINTS):
        directory = tmp_path / name
        directory.mkdir()
        result = _startup_failure(name, directory, b"a: 1\na: 2\n")
        assert result.returncode == 1
        # 한 줄뿐이다 — JSON 이 아닌 줄 (기본 설정의 콘솔 렌더) 이 섞여 나오지 않는다
        assert [line for line in result.stdout.splitlines() if not line.startswith("{")] == []
        lines[name] = _critical_line(result)

    for name, line in lines.items():
        assert set(line) == {
            "event",
            "level",
            "timestamp",
            "error_code",
            "error_message",
            "details",
        }, name
        assert line["error_code"] == "CFG_001"
        assert line["details"]["reason"] == "duplicate key 'a' at line 2"


def test_agentd_startup_failure_goes_through_the_masking_processor(tmp_path: Path):
    """마스킹은 로깅 설정이 끝난 뒤에만 돈다 — 설정 전에 찍힌 줄은 비밀 필드를 그대로 싣는다."""
    result = _entrypoint("agentd", **{MANIFEST_ENV: str(_secret_manifest(tmp_path))})

    line = _critical_line(result)
    assert result.returncode == 1
    assert line["event"] == "agent manifest rejected"
    assert "timestamp" in line  # 기본 설정 (PrintLogger) 에는 TimeStamper 가 없다
    assert MARKER not in result.stdout + result.stderr


# --- status: 깨진 모듈이 조용히 사라지지 않는다 ------------------------------------------------


def test_cli_status_reports_a_module_that_fails_its_schema(tmp_path: Path):
    root = _repo_copy(tmp_path)
    _edit(root / SKILLSET, lambda doc: doc["spec"].update({"zz_unknown": MARKER}))

    result = _cli(root, "--json", "status")

    payload = json.loads(result.stdout)
    assert result.returncode == 1
    assert "skillsets/web-search@0.2.0" not in payload["modules"]
    (problem,) = payload["problems"]
    assert str((root / SKILLSET).parent) in problem
    assert "[MOD_003]" in problem
    assert "spec.zz_unknown: Extra inputs are not permitted" in problem
    assert MARKER not in result.stdout + result.stderr


def test_cli_status_of_the_unmodified_repository_copy_is_clean(tmp_path: Path):
    """같은 명령이 멀쩡한 저장소에서는 0 이다 — 위 테스트의 rc 1 이 다른 이유가 아니다."""
    result = _cli(_repo_copy(tmp_path), "--json", "status")

    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert "problems" not in payload
    assert "skillsets/web-search@0.2.0" in payload["modules"]


# --- validate: 최종 판정과 어긋나는 로그가 없다 ------------------------------------------------


def test_cli_validate_does_not_log_a_pass_when_it_fails_on_the_catalog(tmp_path: Path):
    root = _repo_copy(tmp_path)
    # 아무 에이전트도 쓰지 않는 깨진 모듈 — 그래프 · 에이전트 대조는 통과하고 카탈로그만 실패한다
    unused = root / "modules/skillsets/unused/0.1.0"
    shutil.copytree((root / SKILLSET).parent, unused)

    def broken(document: dict[str, Any]) -> None:
        document["metadata"].update({"name": "unused", "version": "0.1.0"})
        document["spec"]["zz_unknown"] = 1

    _edit(unused / "skillset.yaml", broken)

    result = _cli(root, "--json", "validate")

    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "spec.zz_unknown" in result.stdout
    # 그 단계는 통과했다 (이 시나리오가 맞다는 확인) — 그러나 "검증 통과" 라고는 말하지 않는다
    assert "graph and agent checks passed" in output
    assert "validation passed" not in output
