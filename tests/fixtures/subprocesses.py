"""서브프로세스를 띄우는 테스트의 공통 실행 헬퍼 (#529).

서브프로세스 테스트는 실패해도 rc · 출력이 단언 메시지에 남지 않으면 원인을 알 수 없다
(부하 중 전체 스위트에서만 나온 실패를 재현하지 못한 사례). 이 헬퍼는 실행 결과에
경과 시간을 붙이고, 실패 메시지에 rc · 경과 · stdout · stderr 꼬리를 싣는다.
단언 대상은 바꾸지 않는다 — 메시지만 늘어난다.

비밀 누출 검사용 표식 · 가짜 토큰은 메시지에 싣기 전에 가린다. 가리지 않으면 "값이 출력에
나오지 않는다" 를 검사하는 테스트가 실패할 때 실패 보고가 값을 다시 퍼뜨린다.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

TAIL_CHARS = 2000
REDACTED = "<redacted>"
# tests/fixtures/declarations.py 의 MARKER · 테스트가 env 로 주는 가짜 에이전트 토큰.
# 값이 어긋나면 test_subprocesses_helper.py 의 드리프트 검사가 잡는다
DEFAULT_MASKED = ("sk-SECRET-123-marker", "issued-token")
_SECRET_ENV_KEY = re.compile(r"(TOKEN|SECRET|PASSWORD|API_KEY|_KEY)$", re.IGNORECASE)
_MIN_SECRET_LEN = 8


class Finished(subprocess.CompletedProcess[str]):
    """``CompletedProcess`` 에 경과 시간과 가림 대상을 붙인 결과."""

    elapsed_s: float
    masked: tuple[str, ...]


class ProcessTimeout(subprocess.TimeoutExpired):
    """시간 초과 — 그때까지의 출력을 메시지에 싣는 ``TimeoutExpired``."""

    def __init__(
        self,
        cmd: Any,
        timeout: float,
        output: str,
        stderr: str,
        elapsed_s: float,
        masked: tuple[str, ...],
    ) -> None:
        super().__init__(cmd, timeout, output=output, stderr=stderr)
        self.elapsed_s = elapsed_s
        self._detail = _format(
            f"timeout({timeout:g}s) after {elapsed_s:.2f}s", cmd, output, stderr, masked
        )

    def __str__(self) -> str:
        return self._detail


def mask(text: str, masked: Sequence[str] = DEFAULT_MASKED) -> str:
    """``masked`` 의 값을 대체 문자열로 바꾼다."""
    for value in masked:
        if value:
            text = text.replace(value, REDACTED)
    return text


def _tail(text: str) -> str:
    return text if len(text) <= TAIL_CHARS else "..." + text[-TAIL_CHARS:]


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _format(head: str, cmd: Any, stdout: str, stderr: str, masked: Sequence[str]) -> str:
    argv = " ".join(map(str, cmd)) if isinstance(cmd, (list, tuple)) else str(cmd)
    # 가림이 꼬리 자르기보다 먼저다 — 자른 뒤에 가리면 경계에 걸린 값이 반쪽으로 남아 샌다
    return (
        f"{head}\ncmd: {mask(argv, masked)}\n"
        f"--- stdout (tail {TAIL_CHARS}) ---\n{_tail(mask(stdout, masked))}\n"
        f"--- stderr (tail {TAIL_CHARS}) ---\n{_tail(mask(stderr, masked))}"
    )


def _secret_env_values(env: Mapping[str, str] | None) -> tuple[str, ...]:
    if env is None:
        return ()
    return tuple(
        v for k, v in env.items() if _SECRET_ENV_KEY.search(k) and len(v) >= _MIN_SECRET_LEN
    )


def describe(result: subprocess.CompletedProcess[str]) -> str:
    """rc · 경과 시간 · stdout · stderr 꼬리를 한 메시지로 — 단언 메시지에 쓴다."""
    masked = getattr(result, "masked", DEFAULT_MASKED)
    elapsed = getattr(result, "elapsed_s", float("nan"))
    return _format(
        f"rc={result.returncode} elapsed={elapsed:.2f}s",
        result.args,
        _as_text(result.stdout),
        _as_text(result.stderr),
        masked,
    )


def assert_returncode(result: subprocess.CompletedProcess[str], expected: int = 0) -> None:
    """종료 코드를 단언한다 — 실패하면 rc · 출력 꼬리를 싣는다."""
    assert result.returncode == expected, f"expected rc={expected}; {describe(result)}"


def run_process(
    args: Sequence[str | Path],
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    masked: Sequence[str] = DEFAULT_MASKED,
    check: bool = False,
) -> Finished:
    """서브프로세스를 실행하고 결과를 돌려준다.

    ``timeout`` 을 넘기면 ``ProcessTimeout`` (``TimeoutExpired`` 의 하위) 이 그때까지의 출력과
    함께 올라온다. ``check`` 이면 rc != 0 을 ``AssertionError`` 로 올린다.
    """
    all_masked = (*masked, *_secret_env_values(env))
    start = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 — 호출 측이 고정 인자를 넘긴다
            list(args),
            env=None if env is None else dict(env),
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as err:
        raise ProcessTimeout(
            err.cmd,
            float(err.timeout),
            _as_text(err.stdout),
            _as_text(err.stderr),
            time.monotonic() - start,
            all_masked,
        ) from err
    result = Finished(completed.args, completed.returncode, completed.stdout, completed.stderr)
    result.elapsed_s = time.monotonic() - start
    result.masked = all_masked
    if check:
        assert_returncode(result, 0)
    return result
