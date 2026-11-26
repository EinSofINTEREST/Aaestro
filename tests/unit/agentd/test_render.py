"""Promptset rendering runs in a child process with memory and time limits (#715).

템플릿은 태스크 입력으로 임의 표현식을 평가한다 — 같은 프로세스에서 돌면 렌더 하나가 이벤트 루프를
멈추고 데몬의 메모리를 다 쓴다. 폭탄 템플릿은 실제 조립 경로 (``load_modules`` 의 바인딩) 로
태운다: 바인딩이 자식 프로세스를 거치지 않으면 이 테스트들이 실패해야 한다.
"""

from __future__ import annotations

import asyncio
import gc
import json
import multiprocessing
import os
import resource
import threading
import time
from dataclasses import dataclass
from multiprocessing import forkserver
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import yaml
from structlog.testing import capture_logs

from aaestro.agentd import __main__ as agentd
from aaestro.agentd import render as render_module
from aaestro.agentd.executor import Executor
from aaestro.agentd.render import (
    RENDER_INPUT_MAX_BYTES,
    RenderSandbox,
    warn_if_headroom_exceeds_memory,
)
from aaestro.core.agent import TaskStatus
from aaestro.core.errors import AaestroError
from aaestro.modules.promptset import PromptsetLoader
from aaestro.modules.registry import ModuleRegistry
from tests.fixtures.builders import make_manifest, make_task
from tests.fixtures.fake_model import FakeModel, text
from tests.fixtures.waiting import until

if TYPE_CHECKING:
    from multiprocessing.connection import Connection

PROMPTSET = "promptsets/test@0.1.0"
TEMPLATES = {
    "system": ("You plan.\n", {}),
    "default": ("hello {{ q }}\n", {"q": {"type": "string"}}),
    "center": (
        "{{ q | center(width) | length }}\n",
        {"q": {"type": "string"}, "width": {"type": "integer"}},
    ),
    # 출력 없는 중첩 반복 — 렌더 상한에 닿지 않고 CPU 만 쓴다. 같은 프로세스에서는 1초 남짓
    "loop": (
        "{% for i in range(n) %}{% for j in range(n) %}{% endfor %}{% endfor %}done\n",
        {"n": {"type": "integer"}},
    ),
    # 반복문의 정적 글이 렌더 상한을 넘긴다 — 값은 그 전에 잘린다
    "spill": (
        "{{ q }}{% for i in range(n) %}xxxxxxxxxx{% endfor %}\n",
        {"q": {"type": "string"}, "n": {"type": "integer"}},
    ),
}
MEMORY_BOMB = {"q": "a", "width": 300_000_000}
TIME_BOMB = {"n": 5000}
FAST_TIMEOUT_S = 0.2


@pytest.fixture
def registry(tmp_path: Path) -> ModuleRegistry:
    root = tmp_path / "modules" / "promptsets" / "test" / "0.1.0"
    (root / "templates").mkdir(parents=True)
    declared = {}
    for name, (body, variables) in TEMPLATES.items():
        (root / "templates" / f"{name}.j2").write_text(body, encoding="utf-8")
        declared[name] = {"file": f"templates/{name}.j2"}
        if variables:
            declared[name]["variables"] = variables
    document = {
        "apiVersion": "aaestro/v1",
        "kind": "Promptset",
        "metadata": {"name": "test", "version": "0.1.0"},
        "spec": {"templates": declared},
    }
    (root / "promptset.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return ModuleRegistry.under(tmp_path)


async def standard_executor(registry: ModuleRegistry, sandbox: RenderSandbox) -> Executor:
    """실제 조립 경로의 바인딩으로 실행기를 만든다 — 렌더 배선은 ``load_modules`` 의 것이다."""
    binding = await agentd.load_modules(
        make_manifest(), registry, memory=None, peers=None, sandbox=sandbox
    )
    executor = Executor(
        agent="test-agent",
        model=FakeModel([text("done")]),
        tools=binding.tools,
        render=binding.render,
        system=binding.system,
    )
    executor.rebind(binding)
    return executor


def promptset(registry: ModuleRegistry):
    return PromptsetLoader(registry).load(PROMPTSET)


# --- 실제 실행기 경로: 폭탄은 태스크 실패로 끝난다 ----------------------------------------


async def test_a_memory_bomb_template_fails_the_task_with_val_002_memory(registry):
    executor = await standard_executor(registry, RenderSandbox())

    result = await executor.execute(make_task(node_id="center", input=MEMORY_BOMB))

    assert result.status == TaskStatus.FAILED
    assert result.error is not None
    assert (result.error.code, result.error.details["kind"]) == ("VAL_002", "memory")
    assert result.error.retryable is False


async def test_a_time_bomb_fails_the_task_while_the_daemon_keeps_answering(registry):
    """렌더 동안 이벤트 루프가 돌아야 health 가 답한다 — 같은 프로세스 렌더는 루프를 멈춘다."""
    executor = await standard_executor(registry, RenderSandbox(timeout_s=FAST_TIMEOUT_S))
    app = agentd.build_app(make_manifest(), executor, token="agent-token")
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    running = asyncio.create_task(executor.execute(make_task(node_id="loop", input=TIME_BOMB)))
    try:
        await asyncio.sleep(0.05)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
        ) as client:
            health = await client.get("/v1/health")
        assert health.status_code == 200
        assert not running.done(), "the render blocked the event loop until it finished"
        result = await running
    finally:
        beat.cancel()

    assert result.error is not None
    assert result.error.code == "VAL_002"
    assert result.error.details["kind"] == "time"
    assert result.error.details["limit_s"] == FAST_TIMEOUT_S
    # CPU 기아 같은 일시 장애일 수 있다 — 멱등 캐시에 영구 실패로 남지 않게
    assert result.error.retryable is True
    assert ticks >= 10


async def test_the_system_template_is_rendered_in_the_child_process(registry):
    with pytest.raises(AaestroError) as exc_info:
        await agentd.load_modules(
            make_manifest(),
            registry,
            memory=None,
            peers=None,
            sandbox=RenderSandbox(child_hook=die),
        )

    assert exc_info.value.code == "INTERNAL_001"


# --- 샌드박스: 실패의 모양 ----------------------------------------------------------------


def die(conn: Connection) -> None:
    os._exit(3)


def oversized_reply(conn: Connection) -> None:
    conn.send_bytes(b"x" * (8 << 20))
    os._exit(0)


def stall(conn: Connection) -> None:
    time.sleep(60)


SECRET = "secret-value-from-task-input"


@dataclass(frozen=True)
class RaiseWithValue:
    """stderr 를 파일로 돌린 뒤 값이 든 예외를 던진다 — 자식 stderr 는 forkserver 의 것이다."""

    stderr_path: str

    def __call__(self, conn: Connection) -> None:
        fd = os.open(self.stderr_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.dup2(fd, 2)
        raise ValueError(SECRET)


async def test_a_child_that_dies_without_a_reply_is_internal_001(registry):
    with pytest.raises(AaestroError) as exc_info:
        await RenderSandbox(child_hook=die).render(
            promptset(registry), "default", {"q": "x"}, agent="a", task_id="t"
        )

    error = exc_info.value
    assert (error.code, error.retryable, error.details["exitcode"]) == ("INTERNAL_001", False, 3)
    assert (error.agent, error.task_id) == ("a", "t")


async def test_a_reply_beyond_the_size_limit_is_internal_001(registry):
    loaded = promptset(registry).model_copy(update={"render_limit": 1000})

    with pytest.raises(AaestroError) as exc_info:
        await RenderSandbox(child_hook=oversized_reply).render(
            loaded, "default", {"q": "x"}, agent="a"
        )

    assert exc_info.value.code == "INTERNAL_001"


async def test_an_unexpected_child_error_is_internal_001_with_its_type_only(registry, tmp_path):
    """multiprocessing 이 traceback 을 찍으면 예외 문자열의 입력 값이 컨테이너 로그로 샌다."""
    stderr = tmp_path / "child-stderr"

    with pytest.raises(AaestroError) as exc_info:
        await RenderSandbox(child_hook=RaiseWithValue(str(stderr))).render(
            promptset(registry), "default", {"q": "x"}, agent="a", task_id="t"
        )

    assert stderr.read_text(encoding="utf-8") == ""
    error = exc_info.value
    assert (error.code, error.details.get("cause"), error.retryable) == (
        "INTERNAL_001",
        "ValueError",
        False,
    )
    assert SECRET not in str(error.payload())


async def test_render_warnings_survive_a_failed_render(registry):
    """잘림 WARN 뒤의 실패에서 WARN 이 버려지면 왜 실패했는지의 단서가 사라진다."""
    loaded = promptset(registry).model_copy(update={"render_limit": 2000})

    with capture_logs() as logs, pytest.raises(AaestroError) as exc_info:
        await RenderSandbox().render(
            loaded, "spill", {"q": "y" * 5000, "n": 1000}, agent="a", task_id="t-2"
        )

    assert exc_info.value.code == "VAL_002"
    [warning] = [log for log in logs if log["event"] == "template variable truncated"]
    assert (warning["agent"], warning["task_id"], warning["variable"]) == ("a", "t-2", "q")


async def test_an_error_raised_in_the_render_comes_back_unchanged(registry):
    loaded = promptset(registry)
    with pytest.raises(AaestroError) as expected:
        loaded.render("missing")

    with pytest.raises(AaestroError) as exc_info:
        await RenderSandbox().render(loaded, "missing", {}, agent="a")

    assert exc_info.value.payload() == expected.value.payload()
    assert exc_info.value.code == "MOD_004"


async def test_canceling_a_render_removes_the_child(registry):
    before = {child.pid for child in multiprocessing.active_children()}
    rendering = asyncio.create_task(
        RenderSandbox(timeout_s=30, child_hook=stall).render(
            promptset(registry), "default", {"q": "x"}, agent="a"
        )
    )
    await until(lambda: started(before) is not None)
    child = started(before)

    rendering.cancel()
    with pytest.raises(asyncio.CancelledError):
        await rendering

    assert not proc_exists(child)


def started(before: set[int | None]) -> int | None:
    pids = {child.pid for child in multiprocessing.active_children()} - before
    return next(iter(pids), None)


def proc_exists(pid: int | None) -> bool:
    return Path(f"/proc/{pid}").exists()


async def test_render_warnings_reach_the_parent_log_with_the_task(registry):
    with capture_logs() as logs:
        text_ = await RenderSandbox().render(
            promptset(registry), "default", {"q": "y" * 400_000}, agent="a", task_id="t-1"
        )

    [warning] = [log for log in logs if log["event"] == "template variable truncated"]
    assert (warning["agent"], warning["task_id"], warning["variable"]) == ("a", "t-1", "q")
    assert warning["log_level"] == "warning"
    assert "hello" in text_


async def test_a_render_returns_the_same_text_as_in_process(registry):
    loaded = promptset(registry)

    rendered = await RenderSandbox().render(loaded, "default", {"q": "x"}, agent="a")

    assert rendered == loaded.render("default", q="x")


# --- 태스크 입력의 전송: 상한 안에서, 마감 안에서, 루프 밖에서 (#726 리뷰) --------------------

MARKER = "input-marker-"


def probe_before_input(conn: Connection) -> None:
    """입력을 받기 전의 자식 — 주소 공간 상한이 걸렸는지, 입력이 벌써 메모리에 있는지 답한다."""
    soft, _hard = resource.getrlimit(resource.RLIMIT_AS)
    present = any(
        isinstance(obj, list) and obj and isinstance(obj[0], str) and obj[0].startswith(MARKER)
        for obj in gc.get_objects()
    )
    seen = {"limited": soft != resource.RLIM_INFINITY, "input_present": present}
    conn.send_bytes(json.dumps({"text": json.dumps(seen), "logs": []}).encode())
    os._exit(0)


def slow_reader(conn: Connection) -> None:
    time.sleep(2)


async def test_the_child_limits_its_memory_before_it_receives_the_input(registry):
    """입력이 Process 인자면 진입 전에 풀려 상한 밖에서 입력 크기만큼 메모리를 쓴다."""
    seen = await RenderSandbox(child_hook=probe_before_input).render(
        promptset(registry), "default", {"q": [MARKER + "x"] * 10}, agent="a"
    )

    assert json.loads(seen) == {"limited": True, "input_present": False}


async def test_a_large_input_under_the_limit_renders_while_the_loop_keeps_running(registry):
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.002)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        rendered = await RenderSandbox().render(
            promptset(registry), "default", {"q": "y" * (RENDER_INPUT_MAX_BYTES - 1024)}, agent="a"
        )
    finally:
        beat.cancel()

    assert "hello" in rendered
    # 직렬화 · 전송 · 렌더 동안 루프가 돌았다 — 회차 비율은 부하에 따라 흔들리므로 하한만 본다
    assert ticks >= 5


@pytest.mark.parametrize(
    "variables",
    [
        {"q": "x" * (RENDER_INPUT_MAX_BYTES + 1)},  # 하한만 세어도 넘는다 — 직렬화 전에 멈춘다
        {"q": list(range(1_000_000))},  # 하한은 안 넘고 직렬화한 크기가 넘는다
    ],
    ids=["estimate", "serialized"],
)
async def test_an_input_over_the_transfer_limit_is_refused_without_a_child(
    registry, monkeypatch, variables
):
    sandbox = RenderSandbox()

    def no_child(*args: object, **kwargs: object) -> None:
        raise AssertionError("a child was started for an input over the limit")

    monkeypatch.setattr(sandbox._context, "Process", no_child)

    with pytest.raises(AaestroError) as exc_info:
        await sandbox.render(promptset(registry), "default", variables, agent="a")

    error = exc_info.value
    assert (error.code, error.details["kind"]) == ("VAL_002", "input")
    assert error.details["size_bytes"] > error.details["limit_bytes"] == RENDER_INPUT_MAX_BYTES
    assert "xxxx" not in str(error.payload())


async def test_sending_the_input_counts_against_the_render_deadline(registry):
    """자식이 입력을 읽지 않으면 전송이 막힌다 — 그 시간도 렌더 마감 안이다."""
    started = time.perf_counter()

    with pytest.raises(AaestroError) as exc_info:
        await RenderSandbox(timeout_s=FAST_TIMEOUT_S, child_hook=slow_reader).render(
            promptset(registry), "default", {"q": "z" * (1 << 20)}, agent="a"
        )

    assert (exc_info.value.code, exc_info.value.details["kind"]) == ("VAL_002", "time")
    assert time.perf_counter() - started < 1.5


async def test_the_input_is_serialized_off_the_event_loop_thread(registry, monkeypatch):
    seen: list[threading.Thread] = []
    encode = render_module._encode_input

    def recording(variables):
        seen.append(threading.current_thread())
        return encode(variables)

    monkeypatch.setattr(render_module, "_encode_input", recording)

    await RenderSandbox().render(promptset(registry), "default", {"q": "x"}, agent="a")

    assert seen
    assert seen[0] is not threading.main_thread()


def test_serializing_an_oversized_input_stops_near_the_limit():
    """끝까지 직렬화하면 상한 초과 입력도 입력 크기 (여기서 약 25 MB) 만큼 만든다."""
    huge = {"q": [10**40 + i for i in range(1_300_000)]}

    written = render_module._encode_input(huge)

    assert isinstance(written, int)
    # 상한을 넘은 쓰기에서 멈춘다 — protocol 4+ 의 프레임 (약 64 KB) 하나 남짓까지만
    assert RENDER_INPUT_MAX_BYTES < written <= RENDER_INPUT_MAX_BYTES + (1 << 17)


# --- forkserver 는 첫 렌더의 마감 밖에서 뜬다 --------------------------------------------


async def test_a_render_after_warm_reuses_the_running_fork_server(registry):
    sandbox = RenderSandbox()
    await sandbox.warm()
    server = forkserver._forkserver._forkserver_pid

    await sandbox.render(promptset(registry), "default", {"q": "x"}, agent="a")

    assert server is not None
    assert forkserver._forkserver._forkserver_pid == server


class RecordingSandbox(RenderSandbox):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def warm(self) -> None:
        self.calls.append("warm")
        await super().warm()

    async def render(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append("render")
        return await super().render(*args, **kwargs)


async def test_agentd_warms_the_fork_server_before_the_system_render(registry):
    sandbox = RecordingSandbox()

    await agentd.load_modules(make_manifest(), registry, memory=None, peers=None, sandbox=sandbox)

    assert sandbox.calls[:2] == ["warm", "render"]


# --- 기동 경고 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("memory_max", "warned"),
    [(str(256 << 20), True), (str(8 << 30), False), ("max", False)],
)
def test_headroom_warning_compares_against_half_the_container_memory(tmp_path, memory_max, warned):
    path = tmp_path / "memory.max"
    path.write_text(f"{memory_max}\n", encoding="utf-8")

    with capture_logs() as logs:
        warn_if_headroom_exceeds_memory("a", 4, memory_max=path)

    assert bool(logs) is warned


def test_an_unreadable_container_memory_is_not_warned_about(tmp_path):
    with capture_logs() as logs:
        warn_if_headroom_exceeds_memory("a", 4, memory_max=tmp_path / "missing")

    assert logs == []
