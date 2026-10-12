"""Claude Code executor tests.

CLI 를 **fake 로 대체**한다 — 실 API 를 호출하면 테스트가 비결정적이 되고
비용이 든다 (06 Determinism Around Non-Determinism).

명령을 env 로 주입받도록 설계한 것이 여기서 값을 한다: 프로덕션 코드의
프로세스 실행 경로를 그대로 태우면서도 모델은 부르지 않는다.
"""

from __future__ import annotations

import json
import sys

import pytest

from aaestro.core.agent import TaskStatus
from aaestro.core.errors import ErrorCode
from tests.fixtures.builders import make_manifest, make_task

# 실행기는 저장소의 agents/ 가 아니라 시드 재료에 있다 — 스토어로 올려 굽는다 (#266)
sys.path.insert(0, "examples/materials/claude-code/src")

from agent import (  # noqa: E402 — 경로 삽입 뒤에야 import 가능
    COMMAND_ENV,
    ClaudeCodeExecutor,
    build_prompt,
    cli_error,
    read_result,
    resolve_command,
)


def executor() -> ClaudeCodeExecutor:
    return ClaudeCodeExecutor(make_manifest())


def fake_cli(monkeypatch, *, stdout: str = "", stderr: str = "", code: int = 0, sleep: float = 0):
    """CLI 를 흉내내는 파이썬 한 줄짜리 프로그램을 명령으로 세운다."""
    script = (
        f"import sys,time;"
        f"time.sleep({sleep});"
        f"sys.stdout.write({stdout!r});"
        f"sys.stderr.write({stderr!r});"
        f"sys.exit({code})"
    )
    monkeypatch.setenv(COMMAND_ENV, f"{sys.executable} -c {json.dumps(script)}")


# --- 순수 변환 ------------------------------------------------------------------


def test_the_command_is_injectable(monkeypatch):
    """플래그를 코드에 박으면 CLI 가 바뀔 때마다 이미지가 아니라 코드를 고쳐야 한다."""
    monkeypatch.setenv(COMMAND_ENV, "claude -p --output-format stream-json")

    assert resolve_command() == ["claude", "-p", "--output-format", "stream-json"]


def test_the_default_command_is_used_when_unset(monkeypatch):
    monkeypatch.delenv(COMMAND_ENV, raising=False)

    assert resolve_command()[0] == "claude"


def test_the_prompt_key_is_used_when_present():
    task = make_task(input={"prompt": "테스트를 고쳐줘"})

    assert build_prompt(task) == "테스트를 고쳐줘"


def test_input_without_a_prompt_key_is_passed_as_json():
    """조용히 빈 프롬프트를 보내면 모델이 맥락 없이 답한다."""
    task = make_task(input={"query": "무엇부터?", "depth": 2})

    built = json.loads(build_prompt(task))

    assert built == {"query": "무엇부터?", "depth": 2}


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_prompt_falls_back_to_the_whole_input(blank):
    task = make_task(input={"prompt": blank, "query": "실제 질문"})

    assert "실제 질문" in build_prompt(task)


def test_structured_output_is_unwrapped():
    parsed = read_result(json.dumps({"result": "완료", "cost_usd": 0.01}))

    assert parsed["result"] == "완료"
    assert parsed["raw"]["cost_usd"] == 0.01


def test_plain_text_output_is_preserved():
    """파싱 실패로 결과를 버리면 태스크가 조용히 빈 채로 끝난다."""
    assert read_result("그냥 문장") == {"text": "그냥 문장"}


def test_empty_output_is_an_empty_result():
    assert read_result("   ") == {}


# --- 실행 --------------------------------------------------------------------


async def test_a_successful_run_completes_the_task(monkeypatch):
    fake_cli(monkeypatch, stdout=json.dumps({"result": "테스트를 고쳤습니다"}))

    result = await executor().execute(make_task(input={"prompt": "고쳐줘"}))

    assert result.status is TaskStatus.COMPLETED
    assert result.output["result"] == "테스트를 고쳤습니다"


async def test_a_nonzero_exit_fails_the_task_without_raising(monkeypatch):
    """태스크 실패가 데몬을 죽이면 안 된다 (02 Rule 4)."""
    fake_cli(monkeypatch, stderr="boom", code=2)

    result = await executor().execute(make_task())

    assert result.status is TaskStatus.FAILED
    assert result.error is not None
    assert result.error.code == ErrorCode.LLM_003


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ('claude -p "unbalanced', "AAESTRO_CLAUDE_COMMAND is not a valid command line"),
        ("/nonexistent/claude -p", "claude code could not be started"),
    ],
    ids=["malformed-command", "missing-executable"],
)
async def test_a_command_that_cannot_start_fails_the_task_without_raising(
    monkeypatch, command, message
):
    """명령을 세우지 못해도 typed 실패다 — 새면 최상위 INTERNAL 로 뭉개져 원인을 잃는다."""
    monkeypatch.setenv(COMMAND_ENV, command)
    task = make_task(input={"prompt": "do not leak me"})

    result = await executor().execute(task)

    assert result.status is TaskStatus.FAILED
    assert result.error is not None
    assert result.error.code == ErrorCode.LLM_003
    assert result.error.message == message
    assert "do not leak me" not in str(result.error.details), "프롬프트가 에러 세부로 샜다"


async def test_a_timeout_is_reported_as_to_001(monkeypatch):
    """TaskConfig.timeout_s 를 강제하지 않으면 노드가 영원히 매달린다."""
    from aaestro.core.agent import TaskConfig

    fake_cli(monkeypatch, sleep=5)

    result = await executor().execute(make_task(config=TaskConfig(timeout_s=0.2)))

    assert result.status is TaskStatus.FAILED
    assert result.error is not None
    assert result.error.code == ErrorCode.TO_001
    assert result.error.retryable


async def test_a_timeout_leaves_no_child_process(monkeypatch):
    """자식을 정리하지 않으면 컨테이너에 좀비가 쌓인다."""
    import asyncio

    from aaestro.core.agent import TaskConfig

    fake_cli(monkeypatch, sleep=5)
    started: list[asyncio.subprocess.Process] = []
    original = asyncio.create_subprocess_exec

    async def recording(*args, **kwargs):
        process = await original(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)

    await executor().execute(make_task(config=TaskConfig(timeout_s=0.2)))

    assert started
    assert all(process.returncode is not None for process in started)


async def test_cancellation_cleans_up_the_child(monkeypatch):
    """취소 시 tool 을 정리해야 한다 (02 Rule 1)."""
    import asyncio

    fake_cli(monkeypatch, sleep=5)
    started: list[asyncio.subprocess.Process] = []
    original = asyncio.create_subprocess_exec

    async def recording(*args, **kwargs):
        process = await original(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)

    running = asyncio.create_task(executor().execute(make_task()))
    while not started:  # noqa: ASYNC110 — 자식 기동을 알리는 Event 가 없다
        await asyncio.sleep(0.02)
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running

    assert started[0].returncode is not None


# --- 스트리밍 -------------------------------------------------------------------


async def test_stream_ends_with_a_done_event(monkeypatch):
    fake_cli(monkeypatch, stdout=json.dumps({"result": "완료"}))

    events = [event async for event in executor().stream(make_task())]

    assert events[-1].type == "done"
    assert events[-1].status is TaskStatus.COMPLETED


async def test_a_failed_stream_still_reports_done(monkeypatch):
    """done 이 없으면 소비자가 끝을 모른 채 기다린다."""
    fake_cli(monkeypatch, code=1)

    events = [event async for event in executor().stream(make_task())]

    assert events[-1].type == "done"
    assert events[-1].status is TaskStatus.FAILED


# --- CLI 가 stdout 에 싣는 실패 사유 ------------------------------------------------

# 실제 CLI 출력에서 가져온 모양 — 실패도 stdout 에 JSON 으로 남는다
NOT_LOGGED_IN = json.dumps(
    {"is_error": True, "subtype": "success", "result": "Not logged in · Please run /login"}
)


def test_a_reported_failure_is_read_from_stdout():
    """stderr 만 보면 details 가 비어 운영자가 원인을 잃는다 — 실제로 그랬다."""
    assert cli_error(NOT_LOGGED_IN) == "Not logged in · Please run /login"


def test_a_successful_result_is_not_an_error():
    assert cli_error(json.dumps({"result": "완료"})) is None


def test_plain_text_output_is_not_treated_as_an_error():
    assert cli_error("그냥 문장") is None


async def test_the_failure_reason_reaches_the_task_result(monkeypatch):
    """#133 을 실제로 돌려보고 잡은 결함 — 사유 없는 실패는 진단할 수 없다."""
    fake_cli(monkeypatch, stdout=NOT_LOGGED_IN, code=1)

    result = await executor().execute(make_task())

    assert result.status is TaskStatus.FAILED
    assert result.error is not None
    assert "Not logged in" in result.error.message


async def test_a_reported_failure_with_a_zero_exit_still_fails(monkeypatch):
    """종료코드만 믿으면 CLI 가 0 으로 끝낸 실패가 성공으로 보고된다."""
    fake_cli(monkeypatch, stdout=NOT_LOGGED_IN, code=0)

    result = await executor().execute(make_task())

    assert result.status is TaskStatus.FAILED


# --- JSON 뒤에 붙는 잡음 (실제 출력에서 발견) ------------------------------------------

# CLI 는 JSON 뒤에 경고문을 덧붙인다 — 실제로 관측한 모양
TRAILING_NOISE = (
    "Client.listTools() called but server does not advertise tools capability"
    " - returning empty list"
)


def test_trailing_noise_does_not_discard_the_result():
    """전체를 json.loads 로 읽으면 경고 한 줄에 **결과 전체가 평문으로 떨어진다**."""
    raw = json.dumps({"result": "완료", "is_error": False}) + "\n" + TRAILING_NOISE

    parsed = read_result(raw)

    assert parsed["result"] == "완료"
    assert "text" not in parsed


def test_a_reported_failure_survives_trailing_noise():
    raw = NOT_LOGGED_IN + "\n" + TRAILING_NOISE

    assert cli_error(raw) == "Not logged in · Please run /login"


async def test_a_task_result_is_unwrapped_despite_trailing_noise(monkeypatch):
    """#133 을 실제 컨테이너로 돌려보고 잡은 결함."""
    raw = json.dumps({"result": "hello from aaestro", "is_error": False})
    fake_cli(monkeypatch, stdout=f"{raw}\n{TRAILING_NOISE}")

    result = await executor().execute(make_task())

    assert result.status is TaskStatus.COMPLETED
    assert result.output["result"] == "hello from aaestro"


# --- 권한 모드 (실제 실행으로 결정) ---------------------------------------------------


def test_the_default_command_does_not_ask_for_permission(monkeypatch):
    """헤드리스에는 승인해 줄 사람이 없다 — 기본 모드면 tool 이 전부 거부된다.

    실제로 permission_denials 에 Write 가 쌓여 에이전트가 텍스트만 답하는
    껍데기가 됐다. 권한 경계는 컨테이너가 맡는다 (02 Docker Isolation).
    """
    monkeypatch.delenv(COMMAND_ENV, raising=False)

    command = resolve_command()

    assert "--permission-mode" in command
    assert command[command.index("--permission-mode") + 1] == "bypassPermissions"


def test_the_permission_mode_is_still_overridable(monkeypatch):
    """정책을 좁히고 싶은 배포는 같은 주입 지점으로 좁힌다."""
    monkeypatch.setenv(COMMAND_ENV, "claude -p --output-format json --permission-mode plan")

    assert resolve_command()[-1] == "plan"
