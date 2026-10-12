"""Validation errors name the field and never the rejected value (#386).

거절 에러는 키의 경로와 사유만 말하고 값은 말하지 않는다. message · details · 출력 · 로그,
원인 체인을 포함한 traceback 렌더링, validator 메시지에 보간된 값까지 본다.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
import structlog
import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator
from structlog.testing import capture_logs

from aaestro.access.steward import PermissionAgent
from aaestro.agentd.__main__ import MANIFEST_ENV, load_manifest
from aaestro.agentd.__main__ import main as agentd_main
from aaestro.catalog import Catalog, _validate_module, load_yaml, parse_declaration
from aaestro.cli.main import main as cli_main
from aaestro.config import load_config
from aaestro.core.errors import (
    REDACTED,
    AaestroError,
    ErrorCategory,
    field_problems,
    raise_detached,
)
from aaestro.core.manifest import AgentManifest
from aaestro.modules.memoryset import MemorysetLoader
from aaestro.modules.promptset import PromptsetLoader, PromptsetManifest
from aaestro.modules.registry import ModuleRegistry
from aaestro.modules.skillset import SkillsetLoader
from aaestro.observability.metrics import Metrics
from aaestro.orchestrator.bodies import parsed
from aaestro.orchestrator.control import Draft
from aaestro.orchestrator.state import _coerced, validate_state
from tests.fixtures.builders import make_task, manifest_dict
from tests.fixtures.declarations import (
    MARKER,
    REPO_ROOT,
    _edit,
    _first_doc,
    _repo_copy,
    _secret_manifest,
)
from tests.fixtures.topologies import mission_dict


def test_module_loader_error_names_the_field_and_hides_the_value(tmp_path: Path):
    src = REPO_ROOT / "tests" / "fixtures" / "modules" / "modules"
    shutil.copytree(src, tmp_path / "modules")
    target = tmp_path / "modules" / "skillsets/web-search/0.2.0/skillset.yaml"
    _edit(target, lambda d: d["spec"]["requires"].update({"secrets": {"SEARCH_API_KEY": MARKER}}))

    with pytest.raises(AaestroError) as exc_info:
        SkillsetLoader(ModuleRegistry.under(tmp_path)).load("skillsets/web-search@0.2.0")

    err = exc_info.value
    assert err.details["errors"] == [
        {"field": "spec.requires.secrets", "problem": "Extra inputs are not permitted"}
    ]
    assert MARKER not in json.dumps({"m": err.message, "d": err.details}, default=str)


def test_agentd_load_manifest_error_names_the_field_and_hides_the_value(tmp_path: Path):
    with pytest.raises(AaestroError) as exc_info:
        load_manifest(_secret_manifest(tmp_path))

    err = exc_info.value
    assert err.code == "CFG_001"
    assert err.details["errors"][0]["field"] == "spec.runtime.env"
    assert MARKER not in json.dumps({"m": err.message, "d": err.details}, default=str)
    # 원인 체인이 pydantic 에러를 물고 있으면 처리되지 않은 traceback 이 값을 싣는다
    assert err.__cause__ is None


def test_agentd_main_exits_with_a_log_and_no_traceback_or_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv(MANIFEST_ENV, str(_secret_manifest(tmp_path)))
    # 로깅 설정 · 메트릭 서버는 이 테스트 프로세스에 남는다 (로거 캐시, 포트) — 실제 설정 순서는
    # 별도 프로세스 테스트가 본다 (test_declaration_entrypoints)
    monkeypatch.setattr("aaestro.agentd.__main__._setup_observability", Metrics)

    with capture_logs() as logs, pytest.raises(SystemExit) as exc_info:
        agentd_main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    # 다른 테스트가 structlog 를 이미 구성했다면 캡처되지 않고 stdout 으로 나간다 — 어느 쪽이든 본다
    emitted = json.dumps(logs, default=str) + captured.out + captured.err
    assert "agent manifest rejected" in emitted
    assert "CFG_001" in emitted
    assert "spec.runtime.env" in emitted
    assert "Traceback" not in emitted
    assert MARKER not in emitted


def test_cli_graph_error_reports_fields_without_traceback_or_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    doc = mission_dict()
    doc["spec"]["nodes"][0]["env"] = {"API_KEY": MARKER}
    graph = tmp_path / "graph.yaml"
    graph.write_text(yaml.safe_dump(doc), encoding="utf-8")

    code = cli_main(["--root", str(_repo_copy(tmp_path / "root")), "deploy", str(graph)])

    captured = capsys.readouterr()
    assert code != 0
    assert "spec.nodes.0.env" in captured.err
    assert "Traceback" not in captured.err
    assert MARKER not in captured.out + captured.err


# --- 원인 체인: 거절된 값이 traceback / exc_info 로깅으로 새지 않는다 ---------------------------


class _Counter(BaseModel):
    n: int


def _module_loader_failure(tmp_path: Path, kind: str) -> AaestroError:
    rel, loader_cls, ref = {
        "skillset": (
            "skillsets/web-search/0.2.0/skillset.yaml",
            SkillsetLoader,
            "skillsets/web-search@0.2.0",
        ),
        "promptset": (
            "promptsets/researcher/0.1.0/promptset.yaml",
            PromptsetLoader,
            "promptsets/researcher@0.1.0",
        ),
        "memoryset": (
            "memorysets/agent-longterm/0.1.0/memoryset.yaml",
            MemorysetLoader,
            "memorysets/agent-longterm@0.1.0",
        ),
    }[kind]
    shutil.copytree(REPO_ROOT / "tests" / "fixtures" / "modules" / "modules", tmp_path / "modules")
    _edit(tmp_path / "modules" / rel, lambda d: d["spec"].update({"bogus": {"k": MARKER}}))
    with pytest.raises(AaestroError) as exc_info:
        loader_cls(ModuleRegistry.under(tmp_path)).load(ref)
    return exc_info.value


def _catalog_declaration_failure(tmp_path: Path) -> AaestroError:
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(manifest_dict(spec={"runtime": {"env": {"K": MARKER}}})))
    with pytest.raises(AaestroError) as exc_info:
        parse_declaration(path, AgentManifest)
    return exc_info.value


def _catalog_module_failure(tmp_path: Path) -> AaestroError:
    doc = _first_doc("Skillset")
    doc["spec"]["bogus"] = {"k": MARKER}
    with pytest.raises(AaestroError) as exc_info:
        _validate_module("skillsets", "skillsets/x@0.1.0", doc)
    return exc_info.value


def _catalog_promptset_failure(tmp_path: Path) -> AaestroError:
    root = _repo_copy(tmp_path)
    _edit(
        root / "modules/promptsets/researcher/0.1.0/promptset.yaml",
        lambda d: d["spec"].update({"bogus": {"k": MARKER}}),
    )
    with pytest.raises(AaestroError) as exc_info:
        Catalog.under(root).promptset("promptsets/researcher@0.1.0")
    return exc_info.value


def _agentd_failure(tmp_path: Path) -> AaestroError:
    with pytest.raises(AaestroError) as exc_info:
        load_manifest(_secret_manifest(tmp_path))
    return exc_info.value


def _config_failure(tmp_path: Path) -> AaestroError:
    (tmp_path / "dev.yaml").write_text(yaml.safe_dump({"runtime": {"bogus": {"k": MARKER}}}))
    with pytest.raises(AaestroError) as exc_info:
        load_config("dev", config_dir=tmp_path, environ={})
    return exc_info.value


def _body_failure(tmp_path: Path) -> AaestroError:
    with pytest.raises(AaestroError) as exc_info:
        parsed({"graphs": [], "bogus": {"k": MARKER}}, Draft)
    return exc_info.value


def _state_field_failure(tmp_path: Path) -> AaestroError:
    with pytest.raises(AaestroError) as exc_info:
        _coerced(_Counter, "n", MARKER, node_id="n1")
    return exc_info.value


def _state_failure(tmp_path: Path) -> AaestroError:
    with pytest.raises(AaestroError) as exc_info:
        validate_state(_Counter, {"n": MARKER})
    return exc_info.value


def _steward_failure(tmp_path: Path) -> AaestroError:
    task = make_task(
        input={"kind": "memory", "target": "x", "ttl_s": 5, "reason": "r", "z": MARKER}
    )
    with pytest.raises(AaestroError) as exc_info:
        PermissionAgent(base_url="http://registry.invalid", credential="c")._parse(task, "worker")
    return exc_info.value


BROKEN_YAML = f"key: {MARKER} : [oops\n"
"""ScannerError 의 str() 은 해당 줄을 스니펫으로 싣는다 — 값이 든 깨진 줄."""


def _yaml_catalog_failure(tmp_path: Path) -> AaestroError:
    path = tmp_path / "x.yaml"
    path.write_text(BROKEN_YAML, encoding="utf-8")
    with pytest.raises(AaestroError) as exc_info:
        load_yaml(path)
    return exc_info.value


def _yaml_registry_failure(tmp_path: Path) -> AaestroError:
    shutil.copytree(REPO_ROOT / "tests" / "fixtures" / "modules" / "modules", tmp_path / "modules")
    target = tmp_path / "modules" / "skillsets/web-search/0.2.0/skillset.yaml"
    target.write_text(BROKEN_YAML, encoding="utf-8")
    with pytest.raises(AaestroError) as exc_info:
        SkillsetLoader(ModuleRegistry.under(tmp_path)).load("skillsets/web-search@0.2.0")
    return exc_info.value


def _yaml_agentd_failure(tmp_path: Path) -> AaestroError:
    path = tmp_path / "manifest.yaml"
    path.write_text(BROKEN_YAML, encoding="utf-8")
    with pytest.raises(AaestroError) as exc_info:
        load_manifest(path)
    return exc_info.value


def _yaml_config_failure(tmp_path: Path) -> AaestroError:
    (tmp_path / "dev.yaml").write_text(BROKEN_YAML, encoding="utf-8")
    with pytest.raises(AaestroError) as exc_info:
        load_config("dev", config_dir=tmp_path, environ={})
    return exc_info.value


CHAIN_BOUNDARIES = {
    "skillset-loader": lambda p: _module_loader_failure(p, "skillset"),
    "promptset-loader": lambda p: _module_loader_failure(p, "promptset"),
    "memoryset-loader": lambda p: _module_loader_failure(p, "memoryset"),
    "catalog-declaration": _catalog_declaration_failure,
    "catalog-module": _catalog_module_failure,
    "catalog-promptset": _catalog_promptset_failure,
    "agentd-load-manifest": _agentd_failure,
    "config-file": _config_failure,
    "request-body": _body_failure,
    "state-field": _state_field_failure,
    "state-schema": _state_failure,
    "steward-request": _steward_failure,
    "yaml-catalog": _yaml_catalog_failure,
    "yaml-registry": _yaml_registry_failure,
    "yaml-agentd": _yaml_agentd_failure,
    "yaml-config": _yaml_config_failure,
}


def _renderings(err: BaseException) -> list[str]:
    """예외가 출력될 수 있는 모든 경로 — traceback 모듈과 structlog 의 예외 렌더러들."""
    event = {"event": "boundary failed", "exc_info": err}
    return [
        "".join(traceback.format_exception(err)),
        structlog.processors.format_exc_info(None, "error", dict(event))["exception"],
        # 프레임 지역 변수는 체인과 별개의 문제다 — 끄고 체인만 본다
        json.dumps(
            structlog.processors.ExceptionRenderer(
                structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
            )(None, "error", dict(event)),
            default=str,
        ),
        structlog.dev.ConsoleRenderer(colors=False)(None, "error", dict(event)),
    ]


@pytest.mark.parametrize("boundary", sorted(CHAIN_BOUNDARIES))
def test_validation_boundary_error_never_renders_the_rejected_value(boundary: str, tmp_path: Path):
    err = CHAIN_BOUNDARIES[boundary](tmp_path)

    # 원인을 이은 채로는 pydantic 에러의 input_value 가 체인을 따라 출력된다
    assert err.__cause__ is None
    assert err.__suppress_context__
    for rendered in _renderings(err):
        assert MARKER not in rendered
    assert MARKER not in json.dumps({"m": err.message, "d": err.details}, default=str)


def test_structlog_exception_rendering_does_not_follow_a_suppressed_context():
    """``from None`` 이 ``__context__`` 를 남겨도 structlog 렌더러가 억제를 존중하는지 직접 본다."""
    try:
        try:
            _Counter.model_validate({"n": MARKER})
        except ValidationError:
            raise AaestroError(ErrorCategory.VALIDATION, "VAL_002", "x") from None
    except AaestroError as err:
        assert MARKER in str(err.__context__)
        for rendered in _renderings(err):
            assert MARKER not in rendered


def _exceptions_in_frames(err: BaseException) -> list[BaseException]:
    """traceback 의 프레임 지역 변수에 남은 **다른** 예외 — 수집기가 지역 변수를 덤프하면 나간다."""
    found: list[BaseException] = []
    tb = err.__traceback__
    while tb is not None:
        for value in tb.tb_frame.f_locals.values():
            if isinstance(value, BaseException) and value is not err:
                found.append(value)
        tb = tb.tb_next
    return found


def test_raise_detached_cuts_the_context_not_only_its_display():
    """``from None`` 은 출력만 억제한다 — ``__context__`` 를 직접 따라가도 원인이 없다."""
    try:
        try:
            _Counter.model_validate({"n": MARKER})
        except ValidationError:
            raise_detached(AaestroError(ErrorCategory.VALIDATION, "VAL_002", "x"))
    except AaestroError as err:
        assert err.__context__ is None
        assert err.__cause__ is None
        assert err.__suppress_context__


@pytest.mark.parametrize("boundary", sorted(CHAIN_BOUNDARIES))
def test_validation_boundary_error_keeps_no_cause_in_context_or_frame_locals(
    boundary: str, tmp_path: Path
):
    err = CHAIN_BOUNDARIES[boundary](tmp_path)

    assert err.__context__ is None
    # 프레임 지역 변수에 입력 값을 실은 원인 예외 (ValidationError · YAMLError · UnicodeError) 가
    # 남지 않는다. 파싱된 입력 문서 자체 (``raw`` 등) 는 경계 함수의 지역 변수로 남는다 — 그것은
    # 이 테스트의 대상이 아니다 (프레임 지역 변수를 덤프하는 수집기를 쓰지 않는 것이 전제다)
    assert _exceptions_in_frames(err) == []


def test_agentd_process_exits_with_one_structured_line_and_no_value(tmp_path: Path):
    """진짜 별도 프로세스 — 기동 실패 한 줄이 stdout 에 나오고 traceback 은 없다."""
    manifest = _secret_manifest(tmp_path)

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", "from aaestro.agentd.__main__ import main; main()"],
        # 로깅 · 메트릭 설정이 manifest 로드보다 먼저다 — 고정 포트와 부딪히지 않게 임시 포트 (0)
        env={**os.environ, MANIFEST_ENV: str(manifest), "AAESTRO_METRICS_PORT": "0"},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 1
    assert "agent manifest rejected" in result.stdout
    assert "CFG_001" in result.stdout
    assert "spec.runtime.env" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr
    assert MARKER not in result.stdout + result.stderr


def test_a_broken_yaml_line_is_reported_by_position_not_by_content(tmp_path: Path):
    """일반 파싱 실패에서도 운영자는 줄 번호를 안다 — 줄 내용은 없다."""
    err = _yaml_config_failure(tmp_path)

    assert err.details["reason"].startswith("ScannerError at line 1, column ")
    assert MARKER not in json.dumps(err.details)


# --- 거절 메시지에 보간된 값: field_problems 가 성질로 가린다 ------------------------------------


class _InterpolatingProbe(BaseModel):
    """값을 메시지에 그대로 보간해 거절하는 커스텀 validator — 앞으로 추가될 validator 의 대역."""

    scalar: str | None = None
    nested: dict[str, Any] | None = None
    items: list[str] | None = None

    @field_validator("scalar", "nested", "items")
    @classmethod
    def _echo(cls, value: Any) -> Any:
        raise ValueError(f"bad: {value!r}")


PROBE_INPUTS = {
    "scalar": {"scalar": MARKER},
    "scalar-with-quote": {"scalar": f"it's {MARKER}"},
    "nested-dict": {"nested": {"outer": {"inner": MARKER}}},
    "list": {"items": ["ok-item-1", MARKER]},
}


@pytest.mark.parametrize("case", sorted(PROBE_INPUTS))
def test_field_problems_hide_a_value_a_validator_interpolated(case: str):
    with pytest.raises(ValidationError) as exc_info:
        _InterpolatingProbe.model_validate(PROBE_INPUTS[case])

    rendered = json.dumps(field_problems(exc_info.value))
    assert MARKER not in rendered
    assert REDACTED in rendered
    # 가림이 과하지 않다: 필드 경로와 고정 문구는 남는다
    problems = field_problems(exc_info.value)
    assert problems[0]["field"] in {"scalar", "nested", "items"}
    assert problems[0]["problem"].startswith("Value error, bad: ")


def test_field_problems_keep_short_and_boolean_inputs_readable():
    class Probe(BaseModel):
        flag: bool

        @field_validator("flag")
        @classmethod
        def _echo(cls, value: bool) -> bool:
            raise ValueError(f"flag {value!r} is not allowed")

    with pytest.raises(ValidationError) as exc_info:
        Probe.model_validate({"flag": True})

    assert field_problems(exc_info.value)[0]["problem"] == "Value error, flag True is not allowed"


def test_field_problems_hide_a_tag_pydantic_echoes_in_its_own_message():
    """내장 메시지 중 입력을 되비추는 것 (union_tag_invalid) 도 같은 성질로 가려진다."""

    class A(BaseModel):
        kind: Literal["a"]

    class B(BaseModel):
        kind: Literal["b"]

    class Holder(BaseModel):
        item: Annotated[A | B, Field(discriminator="kind")]

    with pytest.raises(ValidationError) as exc_info:
        Holder.model_validate({"item": {"kind": MARKER}})

    assert MARKER in str(exc_info.value)
    assert MARKER not in json.dumps(field_problems(exc_info.value))


def _http_url_case(url: str) -> dict[str, Any]:
    server = {"name": "corp", "transport": "streamable-http", "url": url}
    return manifest_dict(spec={"mcp": {"servers": [server]}})


VALIDATOR_CASES = {
    "http-url-bad-scheme-with-credentials": (
        _http_url_case(f"ftp://user:{MARKER}@host/x"),
        "must not carry credentials",
    ),
    "http-url-bad-port": (_http_url_case(f"http://host:{MARKER}/x"), "has an invalid port"),
    "http-url-no-host": (_http_url_case(f"http:///{MARKER}"), "must be an http(s) URL"),
    "egress-destination": (
        manifest_dict(spec={"runtime": {"egress": [f"*.{MARKER}"]}}),
        "without wildcards",
    ),
    "module-ref": (
        manifest_dict(spec={"promptset": {"ref": f"promptsets/{MARKER}"}}),
        "String should match pattern",
    ),
}


@pytest.mark.parametrize("case", sorted(VALIDATOR_CASES))
def test_validators_that_used_to_interpolate_the_value_do_not_any_more(case: str):
    document, phrase = VALIDATOR_CASES[case]

    with pytest.raises(ValidationError) as exc_info:
        AgentManifest.model_validate(document)

    problems = field_problems(exc_info.value)
    assert MARKER not in json.dumps(problems)
    assert phrase in " ".join(p["problem"] for p in problems)
    # 원인 쪽 메시지 자체에도 값이 없다 — 가림에만 기대지 않는다
    assert all(MARKER not in str(e["msg"]) for e in exc_info.value.errors())


def test_promptset_validators_do_not_interpolate_the_value():
    document = _first_doc("Promptset")
    document["spec"]["templates"]["default"]["variables"] = {
        "n": {"type": "integer", "default": MARKER}
    }
    with pytest.raises(ValidationError) as exc_info:
        PromptsetManifest.model_validate(document)

    assert all(MARKER not in str(e["msg"]) for e in exc_info.value.errors())
    assert "does not match declared type" in json.dumps(field_problems(exc_info.value))


# --- 진입점: 설정 로드 실패는 구조화 로그 한 줄과 exit 1 --------------------------------------


@pytest.mark.parametrize("entrypoint", ["aaestro.orchestrator.__main__", "aaestro.memory.__main__"])
def test_entrypoint_rejects_a_misspelled_config_key_with_one_log_line(
    entrypoint: str, tmp_path: Path
):
    (tmp_path / "dev.yaml").write_text(
        yaml.safe_dump({"runtime": {"bogus": {"k": MARKER}}}), encoding="utf-8"
    )
    env = {
        **os.environ,
        "AAESTRO_ENV": "dev",
        "AAESTRO_CONFIG_DIR": str(tmp_path),
        # 설정 로드는 포트 bind 뒤에 실패한다 — 고정 포트와 부딪히지 않게 임시 포트 (0) 를 쓴다
        "AAESTRO_METRICS_PORT": "0",
    }

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", f"from {entrypoint} import main; main()"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "configuration rejected" in output
    assert "CFG_001" in output
    assert "runtime.bogus" in output
    assert str(tmp_path / "dev.yaml") in output
    assert "Traceback" not in output
    assert MARKER not in output
