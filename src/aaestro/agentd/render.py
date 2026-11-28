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
import multiprocessing
import os
import pickle
import struct
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from aaestro.core.errors import AaestroError, AaestroErrorPayload, ErrorCategory, ErrorCode
from aaestro.modules.boundary import DEFAULT_RENDER_LIMIT
from aaestro.modules.render_child import RenderLimits, ready, run

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from multiprocessing.connection import Connection
    from multiprocessing.process import BaseProcess

    from aaestro.modules.promptset import LoadedPromptset

RENDER_TIMEOUT_S = 2.0
"""렌더 한 번의 벽시계 상한 — 넘으면 자식을 죽이고 ``VAL_002`` (``kind: time``)."""

RENDER_MEMORY_HEADROOM = 64 << 20
"""자식이 시작할 때의 주소 공간 위로 렌더가 더 쓸 수 있는 바이트 — 넘으면 ``kind: memory``."""

RENDER_INPUT_MAX_BYTES = 16 * DEFAULT_RENDER_LIMIT
"""자식에게 보내는 직렬화한 태스크 입력의 상한 (약 4 MB) — 넘으면 자식을 띄우지 않고 ``VAL_002``
(``kind: input``). 아래 근거: 렌더가 쓰는 입력은 입력 예산 (렌더 상한의 3/4, 192,000자) 이고
그 밖은 잘린다 — 글자당 4바이트로 직렬화해도 768 KB 다. 렌더 상한의 16배는 그 다섯 배 남짓으로,
잘릴 입력도 넉넉히 받아 렌더가 자르게 둔다 (04 Promptset Rules 2 의 "입력 탓으로 실패하지
않는다" 의 예외는 이 상한뿐이다). 위로는 자식의 메모리 여유분 (``RENDER_MEMORY_HEADROOM``) 이 받은
바이트와 풀어 놓은 객체를 함께 담아야 하고, 데몬도 직렬화 사본 하나를 이만큼 더 쥔다.

직렬화는 이 상한에서 멈춘다 (``_BoundedWriter``) — 상한을 넘는 입력도 상한 남짓까지만 만든다.
글자 수가 상한을 넘는 문자열 하나는 직렬화 전에 거절한다 — pickler 가 그 UTF-8 버퍼를 통째로 만든
뒤에 넘기므로, 순간 메모리는 문자열 하나당 4 × 상한 + 상한이다 (``_encode_input``).
직렬화는 스레드에서 돌지만 C pickler 는 내장 타입을 쓰는 동안 GIL 을 놓지 않아 그동안 이벤트
루프도 선다. 그 정지는 이 상한만큼 만드는 시간에 묶인다 (실측은 PR #726)."""

REPLY_OVERHEAD = 64 << 10
"""응답 상한에서 본문 밖의 몫 (JSON 틀 · 로그)."""

EXIT_GRACE_S = 0.5
"""응답을 보낸 자식이 스스로 끝나기를 기다리는 상한 — 넘으면 그때 죽인다."""

REPLY_BYTES_PER_CHAR = 6
"""JSON 이 글자 하나를 쓰는 최대 바이트 — 제어 문자는 ``\\u0000`` 여섯 바이트다."""

CGROUP_MEMORY_MAX = Path("/sys/fs/cgroup/memory.max")

_PRELOAD = [
    "aaestro.modules.render_child",
    "aaestro.modules.promptset",
    "aaestro.modules.registry",
    "jinja2",
]
"""forkserver 가 미리 import 하는 모듈 — 렌더마다 import 하면 렌더당 수백 ms 다.
자식 진입점은 agentd 밖에 있다: 여기 agentd 를 싣으면 fastapi · 실행기 · 서버가 함께 상주한다."""

log = structlog.get_logger(__name__)


class RenderSandbox:
    """Renders promptset templates in a short-lived child process.

    렌더마다 forkserver 에서 자식 하나를 띄워 렌더하고, 끝나면 자식을 남기지 않는다.
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
        self._limits = RenderLimits(
            timeout_s=timeout_s,
            memory_headroom=memory_headroom,
            input_max_bytes=RENDER_INPUT_MAX_BYTES,
        )
        # 테스트가 자식 진입 직후를 흉내 내는 자리 (사망 · 큰 응답) — 모듈 최상위 함수여야 한다
        self._child_hook = child_hook

    async def warm(self) -> None:
        """Start the fork server ahead of the first render, outside any render deadline.

        forkserver 를 첫 렌더 전에 띄운다 — 띄우는 데 수백 ms 가 들고 ``Process.start`` 가 그것을
        동기로 기다리므로, 첫 렌더 (agentd 기동의 ``system`` 렌더) 의 마감을 먹지 않게 마감 밖에서.
        서버를 띄우기만 하면 preload import 가 끝나기 전에 돌아올 수 있다 — 빈 자식 하나를 띄워 거둬
        준비 왕복까지 끝낸다 (``start`` 는 서버가 preload 를 마치고 fork 한 뒤에 돌아온다).
        """
        await asyncio.to_thread(self._round_trip)

    def _round_trip(self) -> None:
        process = self._context.Process(target=ready, daemon=True)
        process.start()
        process.join()
        process.close()

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
        # 직렬화 · 시작 · 입력 전송 · 렌더가 한 마감이다. 직렬화가 루프를 세우는 시간은 입력 크기가
        # 아니라 전송 상한까지 만드는 시간에 묶인다 (RENDER_INPUT_MAX_BYTES)
        try:
            async with asyncio.timeout(self._limits.timeout_s):
                payload = await asyncio.to_thread(_encode_input, variables)
                if isinstance(payload, int):
                    raise self._input_error(payload, promptset, template, agent, task_id)
                raw = await self._run_child(payload, promptset, template, agent, task_id)
        except TimeoutError:
            raise self._limit_error("time", promptset, template, agent, task_id) from None
        return self._reply(raw, promptset, template, agent, task_id)

    async def _run_child(
        self,
        payload: bytearray,
        promptset: LoadedPromptset,
        template: str,
        agent: str,
        task_id: str | None,
    ) -> bytes:
        reader, writer = self._context.Pipe(duplex=False)
        inputs, sender = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=run,
            args=(writer, inputs, promptset, template, self._limits, self._child_hook),
            daemon=True,
        )
        maxlength = REPLY_BYTES_PER_CHAR * promptset.render_limit + REPLY_OVERHEAD
        answered = False
        try:
            process.start()
            writer.close()
            inputs.close()
            await _send(sender, payload)
            raw = await _receive(reader, maxlength)
            answered = True
        except TimeoutError:
            raise  # 마감은 render 가 VAL_002 로 바꾼다 — TimeoutError 도 OSError 다
        except (EOFError, OSError) as err:
            if process.pid is None:
                raise  # 띄우지도 못했다 — 렌더의 결과가 아니다
            # 응답 없이 끝났거나 (EOF — 종료 코드를 기다린다) 상한을 넘는 길이를 알렸다 (곧 kill)
            await _reap(process, grace_s=EXIT_GRACE_S if isinstance(err, EOFError) else 0.0)
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
            if process.pid is not None:
                # 응답을 보낸 자식은 스스로 끝난다 — 거둬진 pid 에 신호를 보내지 않게 기다린다.
                # 시간 초과 · 취소는 기다리지 않고 끝낸다
                await _reap(process, grace_s=EXIT_GRACE_S if answered else 0.0)
                process.close()
            for conn in (writer, inputs, sender, reader):
                conn.close()
        return raw

    def _reply(
        self,
        raw: bytes,
        promptset: LoadedPromptset,
        template: str,
        agent: str,
        task_id: str | None,
    ) -> str:
        reply = json.loads(raw.decode("utf-8", "surrogatepass"))
        # 실패 응답에도 실린다 — 잘림 WARN 뒤의 실패에서 왜 잘렸는지가 사라지지 않게
        for entry in reply["logs"]:
            event = entry.pop("event", "render warning")
            log.warning(event, **{**entry, "agent": agent, "task_id": task_id})
        if "error" in reply:
            payload = AaestroErrorPayload.model_validate(reply["error"])
            raise AaestroError.from_payload(payload)
        if "limit" in reply:
            raise self._limit_error(reply["limit"], promptset, template, agent, task_id)
        if "crash" in reply:
            raise AaestroError(
                category=ErrorCategory.INTERNAL,
                code=ErrorCode.INTERNAL_001,
                message="template render failed unexpectedly",
                agent=agent,
                task_id=task_id,
                retryable=False,
                details={"cause": reply["crash"], "promptset": promptset.ref},
            )
        text: str = reply["text"]
        return text

    def _input_error(
        self,
        size: int,
        promptset: LoadedPromptset,
        template: str,
        agent: str,
        task_id: str | None,
    ) -> AaestroError:
        return AaestroError(
            category=ErrorCategory.VALIDATION,
            code=ErrorCode.VAL_002,
            message="task input exceeds the render transfer limit",
            agent=agent,
            task_id=task_id,
            # 크기는 넘은 시점까지 쓴 바이트 또는 긴 문자열의 글자 수 — 실제 크기의 하한. 값은 없다
            details={
                "kind": "input",
                "size_bytes": size,
                "limit_bytes": RENDER_INPUT_MAX_BYTES,
                "promptset": promptset.ref,
                "template": template,
            },
        )

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
            # 시간 초과는 CPU 기아 같은 일시 장애일 수 있다 — 멱등 캐시에 영구 실패로 남기지 않는다
            retryable=kind == "time",
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


class _InputTooLargeError(Exception):
    """직렬화가 전송 상한을 넘었다 — ``written`` 은 넘은 쓰기까지의 바이트 (실제 크기의 하한)."""

    def __init__(self, written: int) -> None:
        super().__init__("render input exceeds the transfer limit")
        self.written = written


class _BoundedWriter:
    """pickler 가 쓰는 버퍼 — 상한을 넘는 쓰기에서 예외를 던져 직렬화를 멈춘다.

    protocol 4 이상은 약 64 KB 프레임마다 ``write`` 하므로 상한 + 한 프레임 근처에서 멈춘다.
    """

    def __init__(self, limit: int) -> None:
        self.buffer = bytearray()
        self._limit = limit

    def write(self, data: bytes | memoryview) -> int:
        size = len(self.buffer) + len(data)
        if size > self._limit:
            raise _InputTooLargeError(size)
        self.buffer += data
        return len(data)


def _encode_input(variables: Mapping[str, Any]) -> bytearray | int:
    """입력을 피클 바이트로 — 상한을 넘으면 실제 크기의 하한을 돌려준다. 스레드에서 돈다.

    누적은 writer 가 상한에서 멈춘다. 그러나 C pickler 는 비ASCII 문자열 하나의 UTF-8 버퍼를 통째로
    만든 뒤에 writer 에 넘긴다 — 그래서 상한보다 긴 (글자 수) 문자열은 직렬화 전에 거절한다. 순간
    메모리는 문자열 하나당 4 × 상한 (UTF-8 버퍼) + 상한 (writer) 에 묶인다.
    """
    longest = _longest_string(variables, RENDER_INPUT_MAX_BYTES)
    if longest > RENDER_INPUT_MAX_BYTES:
        return longest  # UTF-8 바이트 수 ≥ 글자 수 — 하한이다
    writer = _BoundedWriter(RENDER_INPUT_MAX_BYTES)
    try:
        pickle.Pickler(writer, protocol=pickle.HIGHEST_PROTOCOL).dump(dict(variables))
    except _InputTooLargeError as err:
        return err.written
    return writer.buffer  # 복사하지 않는다 — 데몬이 쥐는 사본은 이것 하나다


def _longest_string(value: Any, limit: int) -> int:
    """상한을 넘는 문자열의 글자 수, 없으면 0 — 합산하지 않는다 (공유 · memo 로 과대 계산된다).

    반복문으로 훑고 컨테이너는 id 로 한 번만 본다 — 깊이 · 공유 구조와 무관하게 원소 수만큼만 돈다.
    """
    seen: set[int] = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if len(item) > limit:
                return len(item)
        elif isinstance(item, dict | list | tuple) and id(item) not in seen:
            seen.add(id(item))
            if isinstance(item, dict):
                stack.extend(item.keys())
                stack.extend(item.values())
            else:
                stack.extend(item)
    return 0


async def _receive(reader: Connection, maxlength: int) -> bytes:
    """응답 프레임 하나를 루프에서 읽는다 — 읽기 가능은 첫 바이트뿐이라 ``recv_bytes`` 는 막힌다.

    프레임은 ``multiprocessing.connection`` 의 것이다 (4바이트 big-endian 길이, -1 이면 뒤에 8바이트
    길이). 길이가 상한을 넘으면 본문을 읽기 전에 ``OSError``, 중간에 끝나면 ``EOFError``.
    """
    fd = reader.fileno()
    os.set_blocking(fd, False)
    (size,) = struct.unpack("!i", await _read_exact(fd, 4))
    if size == -1:
        (size,) = struct.unpack("!Q", await _read_exact(fd, 8))
    if size > maxlength:
        raise OSError("bad message length")
    return bytes(await _read_exact(fd, size))


async def _read_exact(fd: int, size: int) -> bytearray:
    buffer = bytearray()
    while len(buffer) < size:
        try:
            chunk = os.read(fd, min(size - len(buffer), 1 << 16))
        except BlockingIOError:
            await _ready(fd, readable=True)
            continue
        if not chunk:
            raise EOFError
        buffer += chunk
    return buffer


async def _reap(process: BaseProcess, *, grace_s: float) -> None:
    """자식을 거둔다 — 종료는 sentinel 을 루프에서 기다린다 (``join`` 은 루프를 막는다).

    ``grace_s`` 동안 스스로 끝나기를 기다리고, 넘으면 kill 한 뒤 다시 기다린다. 대기 중에 취소가
    와도 ``finally`` 가 kill 하고 동기로 거둔다 — SIGKILL 된 자식은 막을 수 없이 곧 끝나므로 그
    ``join`` 은 커널이 거두는 시간뿐이다.
    """
    try:
        if grace_s and process.exitcode is None:
            try:
                async with asyncio.timeout(grace_s):
                    await _ready(process.sentinel, readable=True)
            except TimeoutError:
                pass
        if process.exitcode is None:
            process.kill()
            await _ready(process.sentinel, readable=True)
    finally:
        if process.exitcode is None:
            process.kill()
        process.join()


async def _send(sender: Connection, payload: bytearray) -> None:
    """입력을 자식에게 쓰고 닫는다 — 파이프가 차면 이벤트 루프가 기다린다 (블로킹 쓰기 없음).

    스레드에서 쓰지 않는다: 마감 · 취소로 떠난 뒤에도 스레드가 fd 를 쥐고 쓰면 닫힌 fd 번호가
    재사용될 때 엉뚱한 곳에 쓴다.
    """
    fd = sender.fileno()
    os.set_blocking(fd, False)
    view = memoryview(payload)
    try:
        while view:
            try:
                written = os.write(fd, view)
            except BlockingIOError:
                await _ready(fd, readable=False)
                continue
            view = view[written:]
    except BrokenPipeError:
        # 자식이 입력을 다 읽기 전에 끝났다 — 무엇이 일어났는지는 응답 (또는 EOF) 이 말한다
        return
    finally:
        sender.close()


async def _ready(fd: int, *, readable: bool) -> None:
    """fd 가 읽기 · 쓰기 가능해질 때까지 이벤트 루프에서 기다린다."""
    # 스레드에서 poll 하면 취소된 뒤에도 스레드가 닫힌 fd 를 잡고 있다 — 이벤트 루프가 기다린다
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()

    def wake() -> None:
        if not ready.done():
            ready.set_result(None)

    add, remove = (
        (loop.add_reader, loop.remove_reader) if readable else (loop.add_writer, loop.remove_writer)
    )
    add(fd, wake)
    try:
        await ready
    finally:
        remove(fd)
