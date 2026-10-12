"""Unit tests for structured logging configuration and secret masking."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest
import structlog

from aaestro.observability.logging import (
    REDACTED,
    STANDARD_FIELDS,
    TRUNCATED,
    LogField,
    bind_a2a,
    bind_agent,
    bind_mcp,
    bind_module,
    bind_run,
    bind_task,
    configure,
    get_logger,
    mask_secrets,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
RULES_DOC = REPO_ROOT / ".claude" / "rules" / "05-error-handling.md"

SECRET = "super-secret-value"  # noqa: S105 - 마스킹 검증용 리터럴


def apply_mask(event: dict) -> dict:
    """processor 를 단독 호출해 마스킹 결과만 본다."""
    return dict(mask_secrets(None, "info", dict(event)))


# --- 표준 필드 계약 ---------------------------------------------------------


def documented_fields() -> set[str]:
    """룰셋 05 의 필드 표에서 이름을 추출한다."""
    lines: list[str] = []
    collecting = False
    for line in RULES_DOC.read_text(encoding="utf-8").splitlines(keepends=True):
        if line.startswith("| Field Key"):
            collecting = True
            continue
        if collecting:
            if not line.startswith("|"):
                break
            lines.append(line)
    return set(re.findall(r"^\|\s*`([a-z0-9_]+)`", "".join(lines), re.MULTILINE))


def test_standard_fields_match_the_ruleset():
    """필드 이름은 05 의 표가 단일 소스다 — 누락/오타를 여기서 잡는다."""
    assert documented_fields() == STANDARD_FIELDS


def test_no_semantically_duplicate_field_names():
    """`agent_name` 같은 변형을 만들면 검색이 갈라진다 (05 규칙)."""
    forbidden = {"agent_name", "graph_name", "run", "task", "node", "error"}

    assert not (STANDARD_FIELDS & forbidden)


def test_duration_fields_are_millisecond_named():
    """시간 값은 밀리초 int, 키는 duration_ms / delay_ms (05 규칙)."""
    assert LogField.DURATION_MS == "duration_ms"
    assert LogField.DELAY_MS == "delay_ms"
    assert not {f for f in STANDARD_FIELDS if f.endswith(("_sec", "_seconds", "_s"))}


def test_field_names_are_snake_case():
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", field) for field in STANDARD_FIELDS)


# --- secret 마스킹 ----------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "api_key",
        "apikey",
        "password",
        "passwd",
        "client_secret",
        "credential",
        "authorization",
        "private_key",
        "ANTHROPIC_API_KEY",
        "Authorization",
    ],
)
def test_secret_looking_keys_are_redacted(key):
    masked = apply_mask({"event": "x", key: SECRET})

    assert masked[key] == REDACTED


def test_ordinary_fields_are_untouched():
    masked = apply_mask({"event": "task done", LogField.AGENT: "researcher", "duration_ms": 12})

    assert masked[LogField.AGENT] == "researcher"
    assert masked["duration_ms"] == 12


def test_nested_dict_secrets_are_redacted():
    masked = apply_mask({"env": {"ANTHROPIC_API_KEY": SECRET, "LOG_LEVEL": "INFO"}})

    assert masked["env"]["ANTHROPIC_API_KEY"] == REDACTED
    assert masked["env"]["LOG_LEVEL"] == "INFO"


def test_deeply_nested_secrets_are_redacted():
    masked = apply_mask({"a": {"b": {"c": {"token": SECRET}}}})

    assert masked["a"]["b"]["c"]["token"] == REDACTED


def test_secrets_inside_lists_are_redacted():
    masked = apply_mask({"servers": [{"name": "fs", "auth_token": SECRET}]})

    assert masked["servers"][0]["auth_token"] == REDACTED
    assert masked["servers"][0]["name"] == "fs"


def test_secrets_inside_tuples_are_redacted():
    masked = apply_mask({"pairs": ({"token": SECRET},)})

    assert masked["pairs"][0]["token"] == REDACTED


def test_masking_survives_json_rendering():
    """최종 출력에 값이 남지 않아야 의미가 있다."""
    masked = apply_mask({"event": "start", "env": {"API_KEY": SECRET}})

    assert SECRET not in json.dumps(masked)


def test_masking_does_not_recurse_forever():
    """자기 참조 구조에서도 멈춘다 — 로깅이 프로세스를 죽이면 안 된다.

    깊이 상한에서 원본을 그대로 돌려주면 순환 참조가 살아남아 JSON 렌더링이
    ``Circular reference detected`` 로 죽는다. 마스킹만이 아니라 **렌더까지**
    통과하는지 확인해야 이 경로가 실제로 막힌다.
    """
    cyclic: dict = {"token": SECRET}
    cyclic["self"] = cyclic

    masked = apply_mask(dict(cyclic))

    assert masked["token"] == REDACTED
    assert json.dumps(masked)  # 순환 참조가 남아 있으면 ValueError 로 실패한다


# --- 렌더링 파이프라인 ------------------------------------------------------


def test_configured_logger_emits_json_without_secrets(capsys):
    configure(level="INFO", json_output=True)
    log = get_logger("test")

    log.info("agent ready", agent="researcher", api_key=SECRET)

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["event"] == "agent ready"
    assert payload["agent"] == "researcher"
    assert payload["api_key"] == REDACTED
    assert SECRET not in out


def test_configured_logger_includes_level_and_timestamp(capsys):
    configure(level="INFO", json_output=True)

    get_logger("test").warning("retrying", attempt=1, max_attempts=3, delay_ms=1000)

    payload = json.loads(capsys.readouterr().out)
    assert payload["level"] == "warning"
    assert "timestamp" in payload
    assert payload["attempt"] == 1


def test_level_filtering_suppresses_lower_levels(capsys):
    configure(level="WARNING", json_output=True)

    get_logger("test").info("should not appear")

    assert capsys.readouterr().out == ""


def test_console_renderer_is_available(capsys):
    configure(level="INFO", json_output=False)

    get_logger("test").info("pretty", agent="researcher")

    assert "pretty" in capsys.readouterr().out


def test_extra_processors_run_before_masking(capsys):
    """주입된 processor 가 넣은 secret 도 마스킹된다."""

    def inject(_logger, _method, event_dict):
        event_dict["injected_token"] = SECRET
        return event_dict

    configure(level="INFO", json_output=True, extra_processors=[inject])

    get_logger("test").info("x")

    out = capsys.readouterr().out
    assert SECRET not in out
    assert json.loads(out)["injected_token"] == REDACTED


# --- 바인딩 헬퍼 ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_structlog():
    """테스트 간 전역 설정이 새지 않도록 초기화."""
    yield
    structlog.reset_defaults()


def bound_fields(logger) -> dict:
    """바인딩된 컨텍스트를 읽는다."""
    return dict(logger._context)


def test_bind_agent_carries_runtime_required_fields():
    log = bind_agent(get_logger(), agent="researcher", version="0.1.0", group="research")

    assert bound_fields(log) == {
        LogField.AGENT: "researcher",
        LogField.AGENT_VERSION: "0.1.0",
        LogField.GROUP: "research",
    }


def test_bind_agent_omits_absent_optionals():
    assert bound_fields(bind_agent(get_logger(), agent="researcher")) == {
        LogField.AGENT: "researcher"
    }


def test_bind_run_carries_orchestrator_required_fields():
    log = bind_run(get_logger(), graph="research-pipeline", run_id="run-1", mode="mission")

    assert bound_fields(log) == {
        LogField.GRAPH: "research-pipeline",
        LogField.RUN_ID: "run-1",
        LogField.MODE: "mission",
    }


def test_bind_task_carries_agentd_required_fields():
    log = bind_task(get_logger(), agent="researcher", task_id="task-1", node_id="planner")

    assert bound_fields(log) == {
        LogField.AGENT: "researcher",
        LogField.TASK_ID: "task-1",
        LogField.NODE_ID: "planner",
    }


def test_bind_a2a_carries_protocol_required_fields():
    log = bind_a2a(get_logger(), caller="researcher", callee="planner", task_id="a2a-1")

    assert bound_fields(log) == {
        LogField.A2A_CALLER: "researcher",
        LogField.A2A_CALLEE: "planner",
        LogField.A2A_TASK_ID: "a2a-1",
    }


def test_bind_mcp_carries_protocol_required_fields():
    log = bind_mcp(get_logger(), agent="researcher", server="filesystem", tool="read_file")

    assert bound_fields(log) == {
        LogField.AGENT: "researcher",
        LogField.MCP_SERVER: "filesystem",
        LogField.TOOL: "read_file",
    }


def test_bind_module_carries_module_ref():
    log = bind_module(get_logger(), module_ref="skillsets/web-search@0.2.0")

    assert bound_fields(log) == {LogField.MODULE_REF: "skillsets/web-search@0.2.0"}


def test_bindings_compose():
    log = bind_task(bind_run(get_logger(), graph="g", run_id="r"), agent="a", task_id="t")

    assert bound_fields(log) == {
        LogField.GRAPH: "g",
        LogField.RUN_ID: "r",
        LogField.AGENT: "a",
        LogField.TASK_ID: "t",
    }


def test_bound_fields_appear_in_output(capsys):
    configure(level="INFO", json_output=True)

    bind_run(get_logger("test"), graph="g", run_id="r").info("run started")

    payload = json.loads(capsys.readouterr().out)
    assert payload[LogField.GRAPH] == "g"
    assert payload[LogField.RUN_ID] == "r"


# --- 자유 문자열 안의 secret ------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "auth failed with token=sk-super-secret-value",
        "api_key: sk-super-secret-value",
        'config {"password": "sk-super-secret-value"}',
        "client_secret=sk-super-secret-value;retry",
        "private_key = sk-super-secret-value",
    ],
)
def test_secrets_embedded_in_strings_are_redacted(text):
    """키-값 구조가 아닌 문자열 안의 secret 도 가려야 한다."""
    masked = apply_mask({"event": "x", "detail": text})

    assert "sk-super-secret-value" not in masked["detail"]
    assert REDACTED in masked["detail"]


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "Basic"])
def test_authorization_schemes_are_redacted(scheme):
    masked = apply_mask({"header": f"{scheme} sk-super-secret-value"})

    assert "sk-super-secret-value" not in masked["header"]


def test_ordinary_strings_are_left_intact():
    """무해한 문자열까지 훼손하면 로그가 못 쓰게 된다."""
    masked = apply_mask({"event": "agent ready", "detail": "node=planner status=completed"})

    assert masked["event"] == "agent ready"
    assert masked["detail"] == "node=planner status=completed"


def test_exception_traceback_is_masked(capsys):
    """log.exception() 한 번으로 secret 이 새면 안 된다 (#30 완료 조건).

    트레이스백은 키 이름이 없는 자유 문자열이라, 이름 기반 판정만으로는
    예외 메시지에 리터럴로 섞인 토큰이 그대로 렌더링된다.
    """
    configure(level="INFO", json_output=True)

    try:
        raise ValueError(f"auth failed with token={SECRET}")
    except ValueError:
        get_logger("test").exception("model call failed", agent="researcher")

    out = capsys.readouterr().out
    assert SECRET not in out
    assert "Traceback" in json.loads(out)["exception"]


def test_secret_inside_nested_string_is_masked():
    masked = apply_mask({"env": {"detail": f"token={SECRET}"}})

    assert SECRET not in masked["env"]["detail"]


def test_secret_inside_list_of_strings_is_masked():
    masked = apply_mask({"lines": [f"api_key={SECRET}", "ok"]})

    assert SECRET not in masked["lines"][0]
    assert masked["lines"][1] == "ok"


def test_cyclic_structure_renders_through_the_logger(capsys):
    """순환 참조가 섞여도 로거가 죽지 않아야 한다."""
    configure(level="INFO", json_output=True)
    cyclic: dict = {"token": SECRET}
    cyclic["self"] = cyclic

    get_logger("test").info("cyclic payload", payload=cyclic)

    out = capsys.readouterr().out
    assert SECRET not in out
    assert json.loads(out)["event"] == "cyclic payload"


def test_deeply_nested_structure_is_truncated_not_dropped():
    """상한 너머는 자리표시자로 대체된다 — 원본을 흘리지 않는다."""
    deep: dict = {"v": "leaf"}
    for _ in range(12):
        deep = {"nested": deep}

    masked = apply_mask(deep)

    assert TRUNCATED in json.dumps(masked)


@pytest.mark.parametrize("level", ["TRACE", "verbose", ""])
def test_unknown_log_level_reports_the_valid_choices(level):
    """오타난 레벨이 KeyError 로 새어나가면 원인을 알기 어렵다."""
    with pytest.raises(ValueError, match="unknown log level"):
        configure(level=level)


def test_known_levels_are_accepted():
    for level in ("DEBUG", "info", "Warning", "ERROR", "CRITICAL"):
        configure(level=level)


def test_rules_doc_is_found_regardless_of_cwd(tmp_path, monkeypatch):
    """테스트 실행 디렉터리가 바뀌어도 룰셋을 찾아야 한다."""
    monkeypatch.chdir(tmp_path)

    assert documented_fields() == STANDARD_FIELDS


def test_a_third_party_registered_level_does_not_become_valid():
    """전역 레지스트리를 신뢰하면 서드파티가 우리 계약을 넓힌다.

    uvicorn 을 구동하면 `TRACE` 가 전역 등록되어, `getLevelNamesMapping()` 을
    쓰던 검증이 조용히 그것을 받아들였다 — 05 의 어느 표에도 없는 레벨인데도.
    """
    # 전역 레지스트리를 건드리므로 실패해도 되돌린다 — 되돌리지 않으면 이
    # 테스트가 바로 그것이 고발하는 순서 의존을 만든다.
    # `addLevelName(5, "Level 5")` 로는 역방향 매핑이 남아 TRACE 가 계속 풀린다
    logging.addLevelName(5, "TRACE")
    try:
        with pytest.raises(ValueError, match="unknown log level"):
            configure(level="TRACE")
    finally:
        with logging._lock:  # noqa: SLF001 — 공개 제거 API 가 없다
            logging._nameToLevel.pop("TRACE", None)  # noqa: SLF001
            logging._levelToName.pop(5, None)  # noqa: SLF001
