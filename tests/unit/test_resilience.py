"""Unit tests for retry execution.

핵심 계약: **정책이 판정하고, 이 계층은 그 판정을 그대로 따른다.** 조건을
여기 다시 쓰면 `retryable=False` 즉시 중단(05 Rules 2)이 조용히 깨진다.

06 에 따라 실제로 sleep 하지 않는다 — asyncio.sleep 을 가로채 대기를 기록만
한다.
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Any

import pytest
from structlog.testing import capture_logs

from aaestro.core.errors import (
    NETWORK_RETRY,
    AaestroError,
    ErrorCategory,
    ErrorCode,
    RetryPolicy,
)
from aaestro.observability.logging import LogField
from aaestro.resilience import retrying

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """실제로 자지 않는다 — 대기 시간만 기록한다 (06 Async 2)."""
    waits: list[float] = []
    real = asyncio.sleep

    async def fake(delay: float, *args: Any, **kwargs: Any) -> Any:
        waits.append(delay)
        return await real(0)

    monkeypatch.setattr(asyncio, "sleep", fake)
    return waits


def fixed_rng() -> random.Random:
    """jitter 를 고정해 대기 시간을 결정적으로 만든다."""
    return random.Random(0)  # noqa: S311 — 암호용이 아니라 테스트 결정성용


def network_error(*, retryable: bool = True) -> AaestroError:
    return AaestroError(
        category=ErrorCategory.NETWORK,
        code=ErrorCode.NET_001,
        message="connection refused",
        retryable=retryable,
    )


def failing(times: int, *, error: Callable[[], BaseException] = network_error) -> Any:
    """`times` 번 실패한 뒤 성공하는 연산."""
    state = {"calls": 0}

    async def op() -> str:
        state["calls"] += 1
        if state["calls"] <= times:
            raise error()
        return "ok"

    op.calls = state  # type: ignore[attr-defined]
    return op


async def test_retries_until_it_succeeds(slept: list[float]):
    op = failing(2)

    assert await retrying(NETWORK_RETRY, op, rng=fixed_rng()) == "ok"
    assert op.calls["calls"] == 3
    assert len(slept) == 2


async def test_exhausted_attempts_raise_the_last_error(slept: list[float]):
    """감싸서 올리면 호출자의 카테고리 기반 처리가 깨진다."""
    op = failing(99)

    with pytest.raises(AaestroError) as excinfo:
        await retrying(NETWORK_RETRY, op, rng=fixed_rng())

    assert excinfo.value.code == ErrorCode.NET_001
    assert op.calls["calls"] == NETWORK_RETRY.max_attempts


async def test_non_retryable_error_stops_immediately(slept: list[float]):
    """05 Rules 2 — 카테고리가 목록에 있어도 retryable=False 면 즉시 중단."""
    op = failing(99, error=lambda: network_error(retryable=False))

    with pytest.raises(AaestroError):
        await retrying(NETWORK_RETRY, op, rng=fixed_rng())

    assert op.calls["calls"] == 1
    assert slept == []


async def test_unrelated_category_is_not_retried(slept: list[float]):
    """정책의 retryable_categories 밖이면 재시도하지 않는다."""

    def validation() -> AaestroError:
        return AaestroError(
            category=ErrorCategory.VALIDATION,
            code=ErrorCode.VAL_001,
            message="missing field",
            retryable=True,
        )

    op = failing(99, error=validation)

    with pytest.raises(AaestroError):
        await retrying(NETWORK_RETRY, op, rng=fixed_rng())

    assert op.calls["calls"] == 1


async def test_plain_exception_is_not_retried(slept: list[float]):
    """AaestroError 가 아니면 재시도 대상이 아니다 — 프로그래밍 오류일 수 있다."""
    op = failing(99, error=lambda: ValueError("plain"))

    with pytest.raises(ValueError):
        await retrying(NETWORK_RETRY, op, rng=fixed_rng())

    assert op.calls["calls"] == 1


async def test_backoff_grows_within_the_policy_bounds(slept: list[float]):
    policy = RetryPolicy(
        max_attempts=4,
        initial_delay_s=1,
        max_delay_s=30,
        retryable_categories=(ErrorCategory.NETWORK,),
    )
    op = failing(99)

    with pytest.raises(AaestroError):
        await retrying(policy, op, rng=fixed_rng())

    assert len(slept) == 3
    assert slept == sorted(slept)
    # jitter 는 대기를 줄이기만 한다 — 상한을 넘지 않는다
    assert all(0 < wait <= policy.delay_for(i + 1) for i, wait in enumerate(slept))


async def test_delay_is_capped_at_max(slept: list[float]):
    """상한이 없으면 백오프가 발산해 복구가 사실상 멈춘다."""
    policy = RetryPolicy(
        max_attempts=6,
        initial_delay_s=10,
        max_delay_s=15,
        retryable_categories=(ErrorCategory.NETWORK,),
    )

    with pytest.raises(AaestroError):
        await retrying(policy, failing(99), rng=fixed_rng())

    assert max(slept) <= 15


async def test_cancellation_propagates_while_waiting():
    """05 Rules 3 — 대기 중에도 취소가 즉시 전파된다."""
    entered = asyncio.Event()

    async def op() -> str:
        entered.set()
        raise network_error()

    task = asyncio.create_task(retrying(NETWORK_RETRY, op, rng=fixed_rng()))
    await entered.wait()
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_retry_warning_carries_the_standard_fields(slept: list[float]):
    """05 Rules 4 — attempt / max_attempts / delay_ms 는 필수다."""
    with capture_logs() as logs:
        await retrying(NETWORK_RETRY, failing(1), rng=fixed_rng(), agent="researcher")

    warned = [entry for entry in logs if entry["log_level"] == "warning"]
    assert warned, "재시도가 조용하면 운영자가 열화를 알 수 없다"
    logged = warned[0]
    assert logged[LogField.ATTEMPT] == 1
    assert logged[LogField.MAX_ATTEMPTS] == NETWORK_RETRY.max_attempts
    assert logged[LogField.DELAY_MS] > 0
    assert logged[LogField.AGENT] == "researcher"


async def test_success_on_the_first_try_does_not_wait(slept: list[float]):
    op = failing(0)

    assert await retrying(NETWORK_RETRY, op, rng=fixed_rng()) == "ok"
    assert slept == []
