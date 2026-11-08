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
# `_` 로 나눈 단어 단위로 판정한다 — 부분 문자열이면 KEYBOARD · AUTHOR · MONKEY 까지 잡는다.
# 이름 끝이 TOKEN · SECRET · PASSWORD · PASSWD 면 단어 경계 없이도 잡는다 (PGPASSWORD ·
# SYSTEM_ACCESSTOKEN 처럼 붙여 쓴 표준 이름). KEY 는 MONKEY 때문에 끝 일치에 넣지 않는다.
# DATABASE_URL 은 접속 문자열에 비밀번호가 실리는 관례라 이름 그대로 넣는다.
# SSH_AUTH_SOCK 처럼 경로를 담는 이름도 가린다 — 진단 정보를 조금 잃더라도 비밀이 새는 쪽보다 낫다
_SECRET_ENV_KEY = re.compile(
    r"(?:^|_)(?:TOKEN|SECRET|PASSWORD|PASSWD|KEY|AUTH|CREDENTIALS?|DSN)(?:_|$)"
    r"|(?:TOKEN|SECRET|PASSWORD|PASSWD)$"
    r"|^DATABASE_URL$",
    re.IGNORECASE,
)
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
    """``masked`` 의 값이 나온 구간을 합쳐 구간마다 대체 문자열 하나로 바꾼다."""
    # 값끼리 일부만 겹쳐도 어느 값의 조각이 남지 않도록 겹치는 출현 구간을 먼저 합친다
    spans: list[tuple[int, int]] = []
    for value in masked:
        if not value:
            continue
        start = text.find(value)
        while start != -1:
            spans.append((start, start + len(value)))
            start = text.find(value, start + 1)
    out: list[str] = []
    pos = 0
    for begin, end in _merge_spans(spans):
        out.extend((text[pos:begin], REDACTED))
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for begin, end in sorted(spans):
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((begin, end))
    return merged


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
