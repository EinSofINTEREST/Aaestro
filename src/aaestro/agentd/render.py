"""Promptset rendering in a separate process (#715).

프롬프트셋 렌더를 렌더마다 자식 프로세스 하나에서 돌린다. 템플릿은 태스크 입력으로 임의 표현식을
평가하고, 그 중간 결과의 메모리 · CPU 는 렌더러가 묶지 못한다 (04 Promptset Rules 2 "한계").
같은 프로세스에서 돌면 렌더 하나가 이벤트 루프를 멈추고 (health · 취소 · 다른 태스크) 메모리를
다 써 데몬을 죽인다. 자식은 시작하자마자 주소 공간 · CPU 상한을 걸고, 부모는 벽시계 상한에서
죽인다. 자원 격리이지 권한 경계가 아니다 — 자식은 같은 사용자 · 같은 파일시스템이다.
"""

from __future__ import annotations

import asyncio
import json
import math
import multiprocessing
import os
import resource
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from aaestro.core.errors import AaestroError, AaestroErrorPayload, ErrorCategory, ErrorCode

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from multiprocessing.connection import Connection

    from aaestro.modules.promptset import LoadedPromptset

RENDER_TIMEOUT_S = 2.0
"""렌더 한 번의 벽시계 상한 — 넘으면 자식을 죽이고 ``VAL_002`` (``kind: time``)."""

RENDER_MEMORY_HEADROOM = 64 << 20
"""자식이 시작할 때의 주소 공간 위로 렌더가 더 쓸 수 있는 바이트 — 넘으면 ``kind: memory``."""

MAX_REPLAYED_LOGS = 64
"""자식의 WARN 중 부모가 다시 내는 건수 상한."""

REPLY_OVERHEAD = 64 << 10
"""응답 상한에서 본문 밖의 몫 (JSON 틀 · 로그)."""

REPLY_BYTES_PER_CHAR = 6
"""JSON 이 글자 하나를 쓰는 최대 바이트 — 제어 문자는 ``\\u0000`` 여섯 바이트다."""

CGROUP_MEMORY_MAX = Path("/sys/fs/cgroup/memory.max")

_PRELOAD = ["aaestro.agentd.render", "aaestro.modules.promptset", "aaestro.modules.registry"]
"""forkserver 가 미리 import 하는 모듈 — 렌더마다 import 하면 렌더당 수백 ms 다."""

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _Limits:
    timeout_s: float
    memory_headroom: int


class RenderSandbox:
    """Renders promptset templates in a short-lived child process.

    렌더마다 forkserver 에서 자식 하나를 띄워 렌더하고 응답을 받은 뒤 반드시 죽인다.
    상주 워커 · ProcessPoolExecutor 를 쓰지 않는 이유는 실행 중인 렌더를 그것만 죽일 수
    없어서다 (#715 설계 결정).
    """

    def __init__(
        self,
        *,
        timeout_s: float = RENDER_TIMEOUT_S,
        memory_headroom: int = RENDER_MEMORY_HEADROOM,
        child_hook: Callable[[Connection], None] | None = None,
    ) -> None:
        self._context = multiprocessing.get_context("forkserver")
        # forkserver 가 이미 떠 있으면 효과가 없다 — 같은 목록이라 상관없다
        self._context.set_forkserver_preload(_PRELOAD)
        self._limits = _Limits(timeout_s=timeout_s, memory_headroom=memory_headroom)
        # 테스트가 자식 진입 직후를 흉내 내는 자리 (사망 · 큰 응답) — 모듈 최상위 함수여야 한다
        self._child_hook = child_hook

    async def render(
        self,
        promptset: LoadedPromptset,
        template: str,
        variables: Mapping[str, Any],
        *,
        agent: str,
        task_id: str | None = None,
    ) -> str:
        """Render one template in a child process.

        자식 프로세스에서 템플릿 하나를 렌더한다. 렌더 안의 ``AaestroError`` 는 그대로 복원된다.

        Raises:
            AaestroError: VALIDATION/``VAL_002`` if the render exceeds the time or memory limit
                (``details.kind``), INTERNAL/``INTERNAL_001`` if the child died without a reply
                or replied beyond the size limit, or the error the render raised.
        """
        reader, writer = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=_child,
            args=(writer, promptset, template, dict(variables), self._limits, self._child_hook),
            daemon=True,
        )
        maxlength = REPLY_BYTES_PER_CHAR * promptset.render_limit + REPLY_OVERHEAD
        try:
            process.start()
            writer.close()
            raw = await self._receive(reader, maxlength)
        except TimeoutError:
            raise self._limit_error("time", promptset, template, agent, task_id) from None
        except (EOFError, OSError) as err:
            if process.pid is None:
                raise  # 띄우지도 못했다 — 렌더의 결과가 아니다
            # 응답 없이 죽었거나 (EOF) 상한을 넘는 길이를 알렸다 (bad message length).
            # 이미 죽은 자식에 kill 은 아무것도 하지 않아 exitcode 는 실제 종료 코드다
            process.kill()
            process.join()
            raise AaestroError(
                category=ErrorCategory.INTERNAL,
                code=ErrorCode.INTERNAL_001,
                message="render process ended without a usable reply",
                agent=agent,
                task_id=task_id,
                retryable=False,
                details={"exitcode": process.exitcode, "promptset": promptset.ref},
            ) from err
        finally:
            # 응답을 보낸 자식도, 취소 · 마감으로 떠나는 경우도 남기지 않는다
            if process.pid is not None:
                process.kill()
                process.join()
                process.close()
            writer.close()
            reader.close()
        return self._reply(raw, promptset, template, agent, task_id)

    async def _receive(self, reader: Connection, maxlength: int) -> bytes:
        # 스레드에서 poll 하면 취소된 뒤에도 스레드가 닫힌 fd 를 잡고 있다 — 이벤트 루프가 기다린다
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        fd = reader.fileno()

        def readable() -> None:
            if not ready.done():
                ready.set_result(None)

        loop.add_reader(fd, readable)
        try:
            async with asyncio.timeout(self._limits.timeout_s):
                await ready
        finally:
            loop.remove_reader(fd)
        return reader.recv_bytes(maxlength)

    def _reply(
        self,
        raw: bytes,
        promptset: LoadedPromptset,
        template: str,
        agent: str,
        task_id: str | None,
    ) -> str:
        reply = json.loads(raw.decode("utf-8", "surrogatepass"))
        if "error" in reply:
            payload = AaestroErrorPayload.model_validate(reply["error"])
            raise AaestroError.from_payload(payload)
        if "limit" in reply:
            raise self._limit_error(reply["limit"], promptset, template, agent, task_id)
        for entry in reply["logs"]:
            event = entry.pop("event", "render warning")
            log.warning(event, **{**entry, "agent": agent, "task_id": task_id})
        text: str = reply["text"]
        return text

    def _limit_error(
        self,
        kind: str,
        promptset: LoadedPromptset,
        template: str,
        agent: str,
        task_id: str | None,
    ) -> AaestroError:
        details: dict[str, Any] = {"kind": kind, "promptset": promptset.ref, "template": template}
        if kind == "time":
            details["limit_s"] = self._limits.timeout_s
        return AaestroError(
            category=ErrorCategory.VALIDATION,
            code=ErrorCode.VAL_002,
            message=f"template render exceeded the {kind} limit",
            agent=agent,
            task_id=task_id,
            details=details,
        )


def warn_if_headroom_exceeds_memory(
    agent: str, slots: int, *, memory_max: Path = CGROUP_MEMORY_MAX
) -> None:
    """Warn once when concurrent renders could take more than half the container memory.

    동시 칸마다 렌더 자식이 하나씩 (리로드 · system 렌더 몫 하나 더) 여유분까지 쓸 수 있다.
    컨테이너 메모리를 읽지 못하거나 상한이 없으면 말하지 않는다.
    """
    try:
        limit = int(memory_max.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    needed = (slots + 1) * RENDER_MEMORY_HEADROOM
    if needed > limit // 2:
        log.warning(
            "render memory headroom exceeds half of the container memory",
            agent=agent,
            slots=slots,
            headroom_bytes=RENDER_MEMORY_HEADROOM,
            memory_max_bytes=limit,
        )


def _child(
    conn: Connection,
    promptset: LoadedPromptset,
    template: str,
    variables: dict[str, Any],
    limits: _Limits,
    hook: Callable[[Connection], None] | None,
) -> None:
    """자식 진입점 — 상한을 건 뒤에야 템플릿을 읽는다."""
    _confine(limits)
    logs: list[dict[str, Any]] = []
    structlog.configure(processors=[_collector(logs)], cache_logger_on_first_use=False)
    if hook is not None:
        hook(conn)
    reply: dict[str, Any]
    try:
        reply = {"text": promptset.render(template, **variables), "logs": logs}
    except MemoryError:
        reply = {"limit": "memory"}
    except AaestroError as err:
        reply = {"error": err.payload().model_dump(mode="json")}
    body = json.dumps(reply, ensure_ascii=False, default=str)
    conn.send_bytes(body.encode("utf-8", "surrogatepass"))


def _confine(limits: _Limits) -> None:
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
