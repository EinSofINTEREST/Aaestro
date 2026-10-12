"""Unit tests for the structured error taxonomy."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aaestro.core.errors import (
    NETWORK_RETRY,
    RATE_LIMIT_RETRY,
    AaestroError,
    AaestroErrorPayload,
    CircuitBreaker,
    CircuitState,
    ErrorCategory,
    ErrorCode,
    RetryPolicy,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
RULES = REPO_ROOT / ".claude" / "rules"


class FakeClock:
    """테스트용 수동 시계 — 실제 sleep 없이 시간 경과를 만든다."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_error_str_includes_category_and_code():
    err = AaestroError(
        category=ErrorCategory.MCP,
        code=ErrorCode.MCP_003,
        message="tool call failed",
    )

    assert str(err) == "[mcp:MCP_003] tool call failed"


def test_payload_round_trip_preserves_fields():
    err = AaestroError(
        category=ErrorCategory.A2A,
        code=ErrorCode.A2A_004,
        message="connection not allowlisted",
        agent="researcher",
        task_id="task-1",
        retryable=False,
        details={"callee": "planner"},
    )

    payload = err.payload()
    restored = AaestroError.from_payload(payload)

    assert isinstance(payload, AaestroErrorPayload)
    assert payload.category is ErrorCategory.A2A
    assert payload.code == "A2A_004"
    assert payload.details == {"callee": "planner"}
    assert restored.payload() == payload


def test_payload_details_are_copied_not_shared():
    details = {"attempt": 1}
    err = AaestroError(
        category=ErrorCategory.NETWORK,
        code=ErrorCode.NET_001,
        message="connection refused",
        details=details,
    )

    err.payload().details["attempt"] = 99

    assert err.details == {"attempt": 1}


def test_cause_chain_is_preserved():
    original = ConnectionError("boom")

    try:
        try:
            raise original
        except ConnectionError as cause:
            raise AaestroError(
                category=ErrorCategory.MCP,
                code=ErrorCode.MCP_004,
                message="mcp transport disconnected",
                retryable=True,
            ) from cause
    except AaestroError as err:
        assert err.__cause__ is original


@pytest.mark.parametrize(
    ("category", "retryable", "expected"),
    [
        (ErrorCategory.NETWORK, True, True),
        (ErrorCategory.TIMEOUT, True, True),
        # retryable=False 면 카테고리가 목록에 있어도 재시도하지 않는다
        (ErrorCategory.NETWORK, False, False),
        (ErrorCategory.VALIDATION, True, False),
    ],
)
def test_network_retry_should_retry(category, retryable, expected):
    err = AaestroError(category=category, code="X", message="m", retryable=retryable)

    assert NETWORK_RETRY.should_retry(err) is expected


def test_should_retry_rejects_plain_exceptions():
    assert NETWORK_RETRY.should_retry(ValueError("plain")) is False


def test_rate_limit_policy_targets_rate_limit_only():
    rate_limited = AaestroError(
        category=ErrorCategory.RATE_LIMIT,
        code=ErrorCode.LLM_001,
        message="provider rate limited",
        retryable=True,
    )
    network = AaestroError(
        category=ErrorCategory.NETWORK, code=ErrorCode.NET_001, message="m", retryable=True
    )

    assert RATE_LIMIT_RETRY.should_retry(rate_limited) is True
    assert RATE_LIMIT_RETRY.should_retry(network) is False


def test_delay_grows_exponentially_and_clamps_to_max():
    policy = RetryPolicy(max_attempts=5, initial_delay_s=1, max_delay_s=8, multiplier=2.0)

    delays = [policy.delay_for(a) for a in range(1, 6)]

    assert delays == [1, 2, 4, 8, 8]


def test_delay_for_rejects_zero_attempt():
    with pytest.raises(ValueError, match="attempt must be >= 1"):
        NETWORK_RETRY.delay_for(0)


def test_breaker_opens_after_max_failures():
    breaker = CircuitBreaker(max_failures=3, clock=FakeClock())

    for _ in range(3):
        breaker.record_failure()

    assert breaker.state is CircuitState.OPEN
    assert breaker.can_attempt() is False


def test_breaker_half_opens_after_reset_timeout():
    clock = FakeClock()
    breaker = CircuitBreaker(max_failures=1, reset_timeout_s=60, clock=clock)
    breaker.record_failure()

    clock.advance(59)
    assert breaker.state is CircuitState.OPEN

    clock.advance(1)
    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker.can_attempt() is True


def test_breaker_success_resets_to_closed():
    breaker = CircuitBreaker(max_failures=2, clock=FakeClock())
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()

    assert breaker.state is CircuitState.CLOSED


async def test_breaker_call_raises_retryable_error_when_open():
    breaker = CircuitBreaker(max_failures=1, clock=FakeClock())
    breaker.record_failure()

    async def never_called() -> str:  # pragma: no cover - 호출되지 않아야 정상
        raise AssertionError("call must not reach the target while open")

    with pytest.raises(AaestroError) as exc_info:
        await breaker.call(never_called)

    assert exc_info.value.message == "circuit open"
    assert exc_info.value.retryable is True
    assert exc_info.value.category is ErrorCategory.INTERNAL


async def test_breaker_call_records_failure_and_propagates():
    breaker = CircuitBreaker(max_failures=2, clock=FakeClock())

    async def failing() -> str:
        raise ConnectionError("boom")

    with pytest.raises(ConnectionError):
        await breaker.call(failing)

    assert breaker.state is CircuitState.CLOSED  # 아직 임계 미달

    with pytest.raises(ConnectionError):
        await breaker.call(failing)

    assert breaker.state is CircuitState.OPEN


async def test_breaker_call_returns_value_and_resets():
    breaker = CircuitBreaker(max_failures=2, clock=FakeClock())
    breaker.record_failure()

    async def ok() -> str:
        return "value"

    assert await breaker.call(ok) == "value"
    assert breaker.state is CircuitState.CLOSED


def documented_error_codes() -> set[str]:
    """05 의 코드 목록을 읽는다.

    하드코딩된 사본과 대조하면 **문서에만 추가된 코드를 잡지 못한다** — 이
    테스트의 이름이 주장하는 것과 다른 일을 하게 된다.
    """
    text = (RULES / "05-error-handling.md").read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9]*_\d{3}):", text, re.MULTILINE))


def test_all_documented_error_codes_exist():
    """룰셋(05-error-handling.md)에 명시된 코드가 전부 정의되어 있는지."""
    documented = documented_error_codes()

    assert documented, "05 의 에러 코드 목록을 찾지 못했다"
    assert {code.value for code in ErrorCode} == documented


def test_explicit_empty_details_is_preserved():
    """빈 dict 는 유효한 입력이다 — falsy 라는 이유로 대체하지 않는다."""
    err = AaestroError(
        category=ErrorCategory.INTERNAL, code=ErrorCode.INTERNAL_001, message="m", details={}
    )

    assert err.details == {}


def test_payload_details_default_is_not_shared():
    """가변 기본값 공유 방지 — 인스턴스마다 새 dict."""
    first = AaestroErrorPayload(
        category=ErrorCategory.INTERNAL, code=ErrorCode.INTERNAL_001, message="a"
    )
    second = AaestroErrorPayload(
        category=ErrorCategory.INTERNAL, code=ErrorCode.INTERNAL_001, message="b"
    )

    first.details["k"] = "v"

    assert second.details == {}


async def test_breaker_open_error_uses_injected_category_and_code():
    """브레이커는 붙는 대상에 따라 카테고리/코드가 다르다 — 소유자가 주입한다."""
    breaker = CircuitBreaker(
        max_failures=1,
        target="mcp:filesystem",
        open_category=ErrorCategory.MCP,
        open_code=ErrorCode.MCP_004,
        clock=FakeClock(),
    )
    breaker.record_failure()

    async def never_called() -> str:  # pragma: no cover - 도달하면 안 된다
        raise AssertionError("must not be called")

    with pytest.raises(AaestroError) as exc_info:
        await breaker.call(never_called)

    assert exc_info.value.category is ErrorCategory.MCP
    assert exc_info.value.code == "MCP_004"
    assert exc_info.value.details["target"] == "mcp:filesystem"
