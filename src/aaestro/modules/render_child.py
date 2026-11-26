"""The child-process side of a sandboxed promptset render (#715).

렌더 자식 프로세스의 진입점. forkserver 가 이 모듈을 미리 싣는다 — agentd 패키지 (fastapi ·
실행기 · 서버) 를 끌어오지 않게 core · modules 만 import 한다. 부모 쪽은 ``agentd/render.py``.

자식은 무엇이 일어나도 stderr 에 쓰지 않고 응답 하나로 답한다: multiprocessing 이 처리되지 않은
예외의 traceback 을 찍으면 예외 문자열에 실린 입력 값이 컨테이너 로그로 샌다 (04 Promptset
Rules 2 — 종류 이름만).
"""

from __future__ import annotations

import json
import math
import os
import pickle
import resource
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from aaestro.core.errors import AaestroError

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.connection import Connection

    from aaestro.modules.promptset import LoadedPromptset

MAX_REPLAYED_LOGS = 64
"""자식의 WARN 중 부모가 다시 내는 건수 상한."""

_MEMORY_REPLY = b'{"limit": "memory", "logs": []}'
"""응답을 만들다 메모리가 모자랄 때의 응답 — 미리 만든 바이트라 할당 없이 보낸다."""


@dataclass(frozen=True)
class RenderLimits:
    """자식이 스스로 거는 상한 — 부모의 벽시계 상한과 같은 값에서 CPU 안전망을 유도한다."""

    timeout_s: float
    memory_headroom: int
    input_max_bytes: int
    """부모가 보내는 직렬화한 입력의 상한 — 부모가 먼저 거르고, 자식은 넘으면 읽기를 멈춘다."""


def run(
    conn: Connection,
    inputs: Connection,
    promptset: LoadedPromptset,
    template: str,
    limits: RenderLimits,
    hook: Callable[[Connection], None] | None,
) -> None:
    """자식 진입점 — 상한을 건 뒤에야 입력을 받고 템플릿을 읽는다. 어떤 실패도 응답으로 돌려준다.

    태스크 입력은 Process 인자가 아니라 ``inputs`` 로 온다: 인자는 진입 전에 풀리므로 상한 밖에서
    입력 크기만큼 메모리를 쓴다 (#726 리뷰).

    응답: ``{"text"}`` / ``{"error": payload}`` / ``{"limit": "memory"}`` /
    ``{"crash": 예외 종류 이름}`` — 모두 ``logs`` 를 싣는다.
    """
    logs: list[dict[str, Any]] = []
    reply: dict[str, Any]
    try:
        confine(limits)
        structlog.configure(processors=[_collector(logs)], cache_logger_on_first_use=False)
        if hook is not None:
            hook(conn)
        variables = _receive_input(inputs, limits.input_max_bytes)
        reply = {"text": promptset.render(template, **variables)}
    except MemoryError:
        reply = {"limit": "memory"}
    except AaestroError as err:
        reply = {"error": err.payload().model_dump(mode="json")}
    except Exception as err:
        # 예외 문자열에는 입력 값이 실린다 — 종류 이름만 보낸다
        reply = {"crash": type(err).__name__}
    reply["logs"] = logs
    try:
        body = json.dumps(reply, ensure_ascii=False, default=str).encode("utf-8", "surrogatepass")
    except MemoryError:
        body = _MEMORY_REPLY
    conn.send_bytes(body)


def _receive_input(inputs: Connection, max_bytes: int) -> dict[str, Any]:
    """부모가 쓰고 닫을 때까지 읽는다 — 프레임 없이 피클 바이트 그대로."""
    fd = inputs.fileno()
    buffer = bytearray()
    while chunk := os.read(fd, 1 << 16):
        buffer += chunk
        if len(buffer) > max_bytes:
            raise ValueError("render input exceeds the transfer limit")
    inputs.close()
    variables: dict[str, Any] = pickle.loads(buffer)  # noqa: S301 — 부모가 만든 바이트다
    return variables


def confine(limits: RenderLimits) -> None:
    """주소 공간 · CPU · core 상한을 건다 — 지금의 주소 공간 위로 여유분만큼만."""
    page = os.sysconf("SC_PAGE_SIZE")
    size = int(Path("/proc/self/statm").read_text(encoding="utf-8").split()[0]) * page
    address_space = size + limits.memory_headroom
    resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
    # 벽시계 상한이 먼저 닿는다 — CPU 상한은 부모가 죽이지 못했을 때의 안전망이다
    cpu = math.ceil(limits.timeout_s) + 1
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 2))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _collector(logs: list[dict[str, Any]]) -> Callable[..., Any]:
    """자식의 로그를 모으고 버린다 — 자식 stdout 은 컨테이너 로그로 새지 않는다."""

    def collect(_logger: Any, method_name: str, event_dict: dict[str, Any]) -> Any:
        if method_name == "warning" and len(logs) < MAX_REPLAYED_LOGS:
            logs.append(dict(event_dict))
        raise structlog.DropEvent

    return collect
