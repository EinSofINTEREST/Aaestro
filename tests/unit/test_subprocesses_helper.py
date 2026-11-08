"""``tests/fixtures/subprocesses.py`` 의 실패 진단 (#529).

서브프로세스 테스트가 실패하면 rc · 출력이 메시지에 남고, 비밀 표식은 가려지는지 본다.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from tests.fixtures.declarations import MARKER
from tests.fixtures.subprocesses import (
    DEFAULT_MASKED,
    REDACTED,
    TAIL_CHARS,
    ProcessTimeout,
    _secret_env_values,
    assert_returncode,
    describe,
    mask,
    run_process,
)

FAKE_TOKEN = "issued-token"
TIMEOUT_S = 5
SECRET_VALUE = "value-0123456789"


def _py(code: str, **kwargs):
    return run_process([sys.executable, "-c", code], **kwargs)


def test_default_masked_covers_the_declaration_marker():
    assert MARKER in DEFAULT_MASKED


def test_assert_returncode_failure_message_carries_rc_and_output():
    result = _py("import sys; print('out-x'); print('err-y', file=sys.stderr); sys.exit(3)")

    with pytest.raises(AssertionError) as exc_info:
        assert_returncode(result, 0)

    message = str(exc_info.value)
    assert "rc=3" in message
    assert "out-x" in message
    assert "err-y" in message
    assert "elapsed=" in message


def test_assert_returncode_passes_on_expected_code():
    assert_returncode(_py("import sys; sys.exit(3)"), 3)


def test_check_raises_assertion_on_nonzero():
    with pytest.raises(AssertionError, match="rc=3"):
        _py("import sys; sys.exit(3)", check=True)


def test_describe_masks_marker_and_env_token():
    code = f"import os; print({MARKER!r}); print(os.environ['AAESTRO_X_TOKEN'])"
    result = _py(code, env={"AAESTRO_X_TOKEN": "tok-abcdefgh-1234"})

    text = describe(result)

    assert MARKER not in text
    assert "tok-abcdefgh-1234" not in text
    assert REDACTED in text
    # 단언 대상 자체는 가려지지 않는다
    assert MARKER in result.stdout


def test_describe_masks_fake_agent_token():
    result = _py(f"print({FAKE_TOKEN!r})")

    assert FAKE_TOKEN not in describe(result)


def test_describe_truncates_to_tail():
    result = _py(f"print('a' * {TAIL_CHARS * 3}); print('END-OF-OUTPUT')")

    text = describe(result)

    assert "END-OF-OUTPUT" in text
    assert len(text) < TAIL_CHARS * 2 + 500


def test_timeout_is_reraised_with_output_collected_so_far():
    code = (
        "import time; print('partial-out', flush=True); "
        f"print({MARKER!r}, flush=True); time.sleep(30)"
    )

    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        _py(code, timeout=TIMEOUT_S)

    assert isinstance(exc_info.value, ProcessTimeout)
    message = str(exc_info.value)
    assert f"timeout({TIMEOUT_S}s)" in message
    assert "partial-out" in message
    assert MARKER not in message
    assert REDACTED in message


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_describe_masks_marker_straddling_the_tail_boundary(stream: str):
    # 표식이 꼬리 시작 경계에 걸치게 — 자른 뒤에 가리면 반쪽이 남아 샌다
    after = TAIL_CHARS - len(MARKER) // 2
    code = (
        "import sys; "
        f"sys.{stream}.write('a' * 500 + {MARKER!r} + 'b' * {after}); "
        f"sys.{stream}.flush()"
    )
    result = _py(code)

    text = describe(result)

    half = len(MARKER) // 2
    assert MARKER[:half] not in text
    assert MARKER[half:] not in text


@pytest.mark.parametrize(
    "name",
    [
        "GITHUB_TOKEN", "MY_SECRET", "DB_PASSWORD", "DB_PASSWD", "OPENAI_API_KEY",
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "DOCKER_AUTH_CONFIG", "SSH_AUTH_SOCK", "GOOGLE_APPLICATION_CREDENTIALS",
        "SERVICE_CREDENTIAL", "SENTRY_DSN", "DATABASE_URL", "github_token",
        "PGPASSWORD", "SYSTEM_ACCESSTOKEN",
    ],
)  # fmt: skip
def test_secret_env_values_picks_secret_looking_names(name: str):
    assert _secret_env_values({name: SECRET_VALUE}) == (SECRET_VALUE,)


@pytest.mark.parametrize(
    "name",
    [
        "PATH", "HOME", "KEYBOARD_LAYOUT", "AUTHOR_NAME", "MONKEY_MODE",
        "TOKENIZER_PARALLELISM", "PASSTHROUGH", "DSNAME", "OTHER_URL",
    ],
)  # fmt: skip
def test_secret_env_values_ignores_lookalike_names(name: str):
    assert _secret_env_values({name: SECRET_VALUE}) == ()


def test_mask_longer_value_wins_over_its_own_prefix():
    text = mask("a abcdefgh-XYZ12345 b", ("abcdefgh", "abcdefgh-XYZ12345"))

    assert "XYZ12345" not in text


def test_mask_partially_overlapping_values_leave_no_fragment():
    text = mask("abcdefgh-XYZ12345-tail", ("abcdefgh-XYZ12345", "XYZ12345-tail"))

    assert text == REDACTED
    assert mask("x abcdefgh-XYZ12345-tail y", ("abcdefgh-XYZ12345", "XYZ12345-tail")) == (
        f"x {REDACTED} y"
    )


def test_mask_separate_values_keep_the_text_between():
    assert mask("aaaaaaaa mid bbbbbbbb", ("aaaaaaaa", "bbbbbbbb")) == (f"{REDACTED} mid {REDACTED}")
