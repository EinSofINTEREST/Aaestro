"""Structured logging configuration.

structlog 기반 구조화 로깅. ``run_id`` 하나로 orchestrator → runtime → agentd →
protocol 로그를 관통해 추적할 수 있어야 하므로, 필드 이름을 상수로 고정한다.

로그 메시지는 영어로 쓰고 f-string 보간을 하지 않는다 — 값은 필드로 분리해야
기계 판독과 검색이 가능하다.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from typing import TYPE_CHECKING, Any, Final, Literal

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterable, MutableMapping

DEFAULT_LOG_LEVEL = "INFO"

LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
"""05 가 규정한 레벨 — 이 프레임워크가 지원하는 전부다.

``logging.getLevelNamesMapping()`` 을 쓰면 **전역 레지스트리**를 신뢰하게 되어,
서드파티가 등록한 레벨(uvicorn 의 ``TRACE`` 등)이 조용히 유효해진다. 그러면
오타에 가까운 값이 통과하고, 그 레벨은 05 의 어느 표에도 없다.
"""

REDACTED: Final = "***"
TRUNCATED: Final = "<truncated>"


class LogField:
    """Standard structured log field names.

    표준 구조화 필드 이름. 05 의 필드 표와 1:1 대응하며, 의미가 중복되는 변형
    (``agent_name`` 등) 을 만들지 않기 위해 상수로 고정한다.
    """

    AGENT: Final = "agent"
    AGENT_VERSION: Final = "agent_version"
    GROUP: Final = "group"
    GRAPH: Final = "graph"
    RUN_ID: Final = "run_id"
    TASK_ID: Final = "task_id"
    NODE_ID: Final = "node_id"
    A2A_CALLER: Final = "a2a_caller"
    A2A_CALLEE: Final = "a2a_callee"
    A2A_TASK_ID: Final = "a2a_task_id"
    MCP_SERVER: Final = "mcp_server"
    TOOL: Final = "tool"
    SKILLSET: Final = "skillset"
    PROMPTSET: Final = "promptset"
    MODULE_REF: Final = "module_ref"
    MEMORY_SPACE: Final = "memory_space"
    MODEL: Final = "model"
    PROVIDER: Final = "provider"
    CONTAINER_ID: Final = "container_id"
    IMAGE: Final = "image"
    ERROR_CODE: Final = "error_code"
    STATUS: Final = "status"
    DURATION_MS: Final = "duration_ms"
    DELAY_MS: Final = "delay_ms"
    ATTEMPT: Final = "attempt"
    MAX_ATTEMPTS: Final = "max_attempts"
    INPUT_TOKENS: Final = "input_tokens"
    OUTPUT_TOKENS: Final = "output_tokens"
    TURN: Final = "turn"
    ITERATION: Final = "iteration"
    MODE: Final = "mode"
    PORT: Final = "port"
    # 권한 통제 — 01 Access Control
    RESOURCE: Final = "resource"
    TARGET: Final = "target"
    DECISION: Final = "decision"
    DECISION_SOURCE: Final = "decision_source"
    GRANT_ID: Final = "grant_id"
    DECIDED_BY: Final = "decided_by"
    # 결정 모델 (01 Decision Models)
    DECISION_USE: Final = "decision_use"
    QUESTION: Final = "question"
    BAND: Final = "band"
    PROBABILITY: Final = "probability"


STANDARD_FIELDS: Final[frozenset[str]] = frozenset(
    value
    for name, value in vars(LogField).items()
    if not name.startswith("_") and isinstance(value, str)
)
"""표준 필드 이름 집합 — 컴포넌트가 임의 키를 만들지 않았는지 검증할 때 쓴다."""


# 값이 secret 인 것으로 간주할 키 패턴 — 이름으로 판정해 값 형태에 의존하지 않는다
_SECRET_KEY_PATTERN: Final = re.compile(
    r"(secret|token|password|passwd|api_?key|credential|authorization|private_?key)",
    re.IGNORECASE,
)

_MAX_REDACT_DEPTH: Final = 6

# 문자열 안에 섞여 들어온 secret — `token=sk-...`, `"api_key": "..."`, `Bearer ...`
# 형태를 값째로 가린다. 예외 메시지/트레이스백은 키 이름이 없는 자유 문자열이라
# 이름 기반 판정만으로는 잡히지 않는다.
_SECRET_VALUE_PATTERN: Final = re.compile(
    r"""
    (?P<prefix>
        (?:secret|token|password|passwd|api[_-]?key|credential|private[_-]?key)
        ["']?            # JSON 형태의 닫는 따옴표: "password":
        \s* [=:] \s*
        ["']?            # 값을 여는 따옴표
    )
    (?P<value>[^\s"',;)}\]]+)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_BEARER_PATTERN: Final = re.compile(
    r"\b(?P<scheme>bearer|basic)\s+(?P<value>[A-Za-z0-9._\-+/=]+)",
    re.IGNORECASE,
)


def _looks_secret(key: str) -> bool:
    """키 이름이 secret 을 담는 것으로 보이는지."""
    return bool(_SECRET_KEY_PATTERN.search(key))


def _redact_text(text: str) -> str:
    """자유 문자열 안에 섞인 secret 을 가린다.

    예외 메시지와 트레이스백은 키-값 구조가 아니므로 이름 기반 판정으로는
    잡히지 않는다 — 값 패턴으로 한 번 더 훑는다.
    """
    redacted = _SECRET_VALUE_PATTERN.sub(lambda m: f"{m.group('prefix')}{REDACTED}", text)
    return _BEARER_PATTERN.sub(lambda m: f"{m.group('scheme')} {REDACTED}", redacted)


def _redact(value: Any, depth: int = 0) -> Any:
    """중첩 구조를 따라가며 secret 키의 값을 가린다.

    깊이 상한에 닿으면 원본이 아니라 **자리표시자 문자열**을 돌려준다 —
    원본을 그대로 넘기면 순환 참조가 살아남아 JSON 렌더링이
    ``Circular reference detected`` 로 죽는다 (로그 한 줄이 프로세스를 죽인다).
    """
    if depth >= _MAX_REDACT_DEPTH:
        return TRUNCATED
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {
            key: REDACTED if _looks_secret(str(key)) else _redact(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item, depth + 1) for item in value)
    return value


def mask_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Redact secret-looking values from a log event.

    Secret 으로 보이는 키의 값을 가립니다 — 중첩 dict/list 까지 따라갑니다.

    두 층으로 판정합니다:

    1. **키 이름** — ``api_key`` 같은 키의 값을 통째로 가린다
    2. **문자열 값** — ``token=...`` / ``Bearer ...`` 처럼 자유 문자열 안에 섞인
       secret 을 가린다. 예외 메시지와 트레이스백은 키-값 구조가 아니므로
       이 층이 없으면 ``log.exception()`` 한 번으로 값이 그대로 새어나간다
    """
    for key in list(event_dict):
        if _looks_secret(str(key)):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact(event_dict[key])
    return event_dict


class _StreamLogger:
    """Writes rendered log lines to a stream resolved at write time.

    스트림 객체를 붙잡아 두지 않고 **쓸 때마다 조회**합니다 — 캡처해 두면
    호출자가 스트림을 교체하거나 닫은 뒤 "I/O operation on closed file" 로
    터집니다 (pytest capsys, 로그 리다이렉션, 데몬 재바인딩 등).
    """

    def __init__(self, stream_name: Literal["stdout", "stderr"]) -> None:
        self._stream_name = stream_name
        self._lock = threading.Lock()

    def msg(self, message: str) -> None:
        """렌더된 한 줄을 기록한다."""
        stream = getattr(sys, self._stream_name)
        with self._lock:
            stream.write(message + "\n")
            stream.flush()

    log = debug = info = warn = warning = error = critical = exception = fatal = msg

    def __repr__(self) -> str:
        return f"<_StreamLogger({self._stream_name})>"


class _StreamLoggerFactory:
    """Builds stream loggers for the configured destination."""

    def __init__(self, stream_name: Literal["stdout", "stderr"]) -> None:
        self._stream_name = stream_name

    def __call__(self, *args: Any) -> _StreamLogger:
        return _StreamLogger(self._stream_name)


def configure(
    *,
    level: str = DEFAULT_LOG_LEVEL,
    json_output: bool = True,
    extra_processors: Iterable[Any] | None = None,
    stream_name: Literal["stdout", "stderr"] = "stdout",
) -> None:
    """Configure structlog for the process.

    프로세스 전역 로깅을 설정합니다.

    Args:
        level: Minimum level name (e.g. ``"INFO"``).
        json_output: JSON renderer for production; pretty console when False.
        extra_processors: Processors inserted before rendering.
    """
    resolved_level = LOG_LEVELS.get(level.upper())
    if resolved_level is None:
        raise ValueError(f"unknown log level: {level!r} (expected one of {sorted(LOG_LEVELS)})")

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # 마스킹은 렌더 직전에 — 그 사이에 어떤 processor 도 값을 되살리지 못하게
        *(extra_processors or []),
        mask_secrets,
        renderer,
    ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(resolved_level),
        logger_factory=_StreamLoggerFactory(stream_name),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> Any:
    """Return a bound logger.

    바인딩 가능한 로거를 반환합니다.
    """
    return structlog.get_logger(name) if name else structlog.get_logger()


def bind_agent(
    logger: Any, *, agent: str, version: str | None = None, group: str | None = None
) -> Any:
    """Bind the fields every ``runtime/`` and ``agentd/`` log must carry.

    runtime/agentd 로그의 필수 필드를 바인딩합니다.
    """
    bindings: dict[str, Any] = {LogField.AGENT: agent}
    if version is not None:
        bindings[LogField.AGENT_VERSION] = version
    if group is not None:
        bindings[LogField.GROUP] = group
    return logger.bind(**bindings)


def bind_run(logger: Any, *, graph: str, run_id: str, mode: str | None = None) -> Any:
    """Bind the fields every ``orchestrator/`` log must carry.

    orchestrator 로그의 필수 필드를 바인딩합니다.
    """
    bindings: dict[str, Any] = {LogField.GRAPH: graph, LogField.RUN_ID: run_id}
    if mode is not None:
        bindings[LogField.MODE] = mode
    return logger.bind(**bindings)


def bind_task(logger: Any, *, agent: str, task_id: str, node_id: str | None = None) -> Any:
    """Bind the fields every ``agentd/`` task log must carry.

    agentd 태스크 로그의 필수 필드를 바인딩합니다.
    """
    bindings: dict[str, Any] = {LogField.AGENT: agent, LogField.TASK_ID: task_id}
    if node_id is not None:
        bindings[LogField.NODE_ID] = node_id
    return logger.bind(**bindings)


def bind_a2a(logger: Any, *, caller: str, callee: str, task_id: str | None = None) -> Any:
    """Bind the fields every ``protocols/a2a/`` log must carry.

    A2A 로그의 필수 필드를 바인딩합니다.
    """
    bindings: dict[str, Any] = {
        LogField.A2A_CALLER: caller,
        LogField.A2A_CALLEE: callee,
    }
    if task_id is not None:
        bindings[LogField.A2A_TASK_ID] = task_id
    return logger.bind(**bindings)


def bind_mcp(logger: Any, *, agent: str, server: str, tool: str | None = None) -> Any:
    """Bind the fields every ``protocols/mcp/`` log must carry.

    MCP 로그의 필수 필드를 바인딩합니다.
    """
    bindings: dict[str, Any] = {LogField.AGENT: agent, LogField.MCP_SERVER: server}
    if tool is not None:
        bindings[LogField.TOOL] = tool
    return logger.bind(**bindings)


def bind_module(logger: Any, *, module_ref: str) -> Any:
    """Bind the field every ``modules/`` log must carry.

    모듈 레이어 로그의 필수 필드를 바인딩합니다.
    """
    return logger.bind(**{LogField.MODULE_REF: module_ref})
