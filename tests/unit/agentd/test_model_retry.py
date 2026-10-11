"""Model-call retry wiring.

05 Retry Layering 은 모델 호출의 재시도 주체를 **agentd** 로 정한다. 정책이
정의만 되고 아무도 부르지 않으면 provider 가 흔들릴 때마다 태스크가 그대로
실패한다 (#175).

06 에 따라 실제로 자지 않는다 — 대기를 주입해 기록만 한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from malkuth.agentd.executor import MODEL_RETRY_POLICIES, Executor, ExecutorConfig
from malkuth.core.agent import TaskStatus
from malkuth.core.errors import (
    NETWORK_RETRY,
    RATE_LIMIT_RETRY,
    ErrorCategory,
    ErrorCode,
    MalkuthError,
)
from tests.fixtures.builders import make_task
from tests.fixtures.fake_model import FakeModel, FakeTools, text

REPO_ROOT = Path(__file__).resolve().parents[3]


def model_error(category: ErrorCategory, code: ErrorCode, *, retryable: bool = True):
    return MalkuthError(
        category=category, code=code, message="provider failed", retryable=retryable
    )


def rate_limited():
    return model_error(ErrorCategory.RATE_LIMIT, ErrorCode.LLM_001)


def unreachable():
    return model_error(ErrorCategory.NETWORK, ErrorCode.NET_001)


@pytest.fixture
def waits() -> list[float]:
    """재시도가 실제로 자지 않게 하고, 얼마나 기다리려 했는지만 남긴다."""
    return []


def executor(responses, waits: list[float], *, policies=MODEL_RETRY_POLICIES) -> Executor:
    async def sleep(delay: float) -> None:
        waits.append(delay)

    return Executor(
        agent="researcher",
        model=FakeModel(responses),
        tools=FakeTools(),
        render=lambda _task: "prompt",
        config=ExecutorConfig(retry_policies=policies, retry_sleep=sleep),
    )


async def test_a_transient_failure_is_retried(waits):
    """#175 — 이 배선이 없어 provider 가 한 번 흔들리면 태스크가 죽었다."""
    result = await executor([unreachable(), text("done")], waits).execute(make_task())

    assert result.status is TaskStatus.COMPLETED
    assert len(waits) == 1


async def test_rate_limit_waits_on_its_own_schedule(waits):
    """1초 간격으로 rate limit 을 두드리면 상황이 악화된다."""
    await executor([rate_limited(), text("done")], waits).execute(make_task())

    assert waits[0] > NETWORK_RETRY.initial_delay_s
    assert waits[0] <= RATE_LIMIT_RETRY.initial_delay_s


async def test_network_failure_uses_the_shorter_schedule(waits):
    """네트워크 실패까지 10초를 기다리면 복구가 느려진다."""
    await executor([unreachable(), text("done")], waits).execute(make_task())

    assert waits[0] <= NETWORK_RETRY.initial_delay_s


async def test_a_permanent_failure_is_not_retried(waits):
    """05 Rules 2 — retryable=False 는 카테고리와 무관하게 즉시 중단."""
    permanent = model_error(ErrorCategory.MODEL, ErrorCode.LLM_002, retryable=False)

    result = await executor([permanent], waits).execute(make_task())

    assert result.status is TaskStatus.FAILED
    assert waits == []


async def test_exhausted_retries_fail_the_task(waits):
    """무한히 재시도하면 태스크가 영원히 끝나지 않는다."""
    result = await executor([unreachable()], waits).execute(make_task())

    assert result.status is TaskStatus.FAILED
    assert result.error is not None
    assert result.error.code == ErrorCode.NET_001
    # 마지막 시도 뒤에는 기다리지 않는다
    assert len(waits) == NETWORK_RETRY.max_attempts - 1


async def test_retry_is_off_unless_the_assembly_turns_it_on(waits):
    """기본으로 켜면 실패를 스크립트하는 모든 테스트가 초 단위로 기다린다."""
    result = await executor([unreachable()], waits, policies=()).execute(make_task())

    assert result.status is TaskStatus.FAILED
    assert waits == []


async def test_the_production_assembly_turns_retry_on(monkeypatch):
    """#177 의 핵심 — 정책을 정의만 하고 조립에서 켜지 않으면 아무 일도 없다.

    실행기를 **실제 조립 경로로** 만든다. 소스 문자열을 보면 조립이 바뀌어도
    통과하고, Executor 를 직접 만들면 이 배선 자체를 건너뛴다.
    """
    from malkuth.agentd.__main__ import build_executor, load_manifest

    monkeypatch.delenv("MALKUTH_EXECUTOR", raising=False)
    monkeypatch.setenv("MALKUTH_ROOT", str(REPO_ROOT))

    built = await build_executor(load_manifest(REPO_ROOT / "agents/researcher/manifest.yaml"))

    # 재시도 정책은 모델 체인이 들고 온다 (#359) — 선언에 retry 가 없으면 기본 정책이다
    assert built._chain.retry_policies == MODEL_RETRY_POLICIES
