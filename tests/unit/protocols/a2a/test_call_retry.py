"""A2A call retry.

05 Retry Layering 은 A2A 호출의 재시도 주체를 **caller 에이전트**로 정한다.
`retryable` 플래그는 정확히 붙어 있었는데(A2A_002 는 True, A2A_003 은 False)
그것을 보는 쪽이 없었다 (#184).
"""

from __future__ import annotations

from typing import Any

import pytest

from aaestro.core.agent import TaskResult
from aaestro.core.errors import NETWORK_RETRY, AaestroError, ErrorCategory, ErrorCode, RetryPolicy
from aaestro.protocols.a2a.client import A2AClient
from tests.fixtures.builders import make_task

A2A_RETRY = RetryPolicy(
    max_attempts=3,
    initial_delay_s=1.0,
    max_delay_s=10.0,
    retryable_categories=(ErrorCategory.A2A, ErrorCategory.NETWORK, ErrorCategory.TIMEOUT),
)


class FlakyTransport:
    """정해진 횟수만큼 실패한 뒤 성공하는 peer transport 대역.

    transport 는 **원래 예외**를 던지는 계층이다 — `_send` 가 그것을 A2A 코드로
    변환한다. 대역이 AaestroError 를 던지면 그 변환 경로를 건너뛰어, 실제와
    다른 코드로 검증하게 된다.
    """

    def __init__(self, failures: int, *, error: BaseException | None = None) -> None:
        self._left = failures
        self._error = error or ConnectionError("peer unreachable")
        self.sent: list[str] = []

    async def send(self, *, callee: str, task: Any, token: str, headers: Any) -> TaskResult:
        self.sent.append(task.task_id)
        if self._left > 0:
            self._left -= 1
            raise self._error
        return TaskResult.completed(task, output={})


class OpenAllowlist:
    """모든 방향을 허용하는 allowlist 대역 — 재시도만 보기 위함."""

    def check_call(self, caller: str, callee: str, trace: Any) -> None:
        return

    def token_for(self, caller: str, callee: str) -> str:
        return "edge-token"


@pytest.fixture
def waits() -> list[float]:
    return []


def client(transport: Any, waits: list[float], **kwargs: Any) -> A2AClient:
    async def sleep(delay: float) -> None:
        waits.append(delay)

    kwargs.setdefault("retry", A2A_RETRY)
    return A2AClient(
        agent="researcher",
        allowlist=OpenAllowlist(),  # type: ignore[arg-type]
        transport=transport,
        retry_sleep=sleep,
        **kwargs,
    )


async def test_an_unreachable_peer_is_retried(waits):
    """#184 — 이 배선이 없어 peer 가 한 번 흔들리면 위임이 죽었다."""
    transport = FlakyTransport(failures=2)

    result = await client(transport, waits).call("planner", make_task())

    assert result.status.value == "completed"
    assert len(transport.sent) == 3


async def test_retry_keeps_the_same_task_id(waits):
    """새 id 로 다시 보내면 callee 가 같은 위임을 두 번 수행한다 (05 Rules 5)."""
    transport = FlakyTransport(failures=2)

    await client(transport, waits).call("planner", make_task())

    assert len(set(transport.sent)) == 1


async def test_a_rejected_task_is_not_retried(waits):
    """A2A_003 은 peer 가 **살아서** 못 한다고 답한 것이다 — 다시 보내도 같다."""

    class RejectingTransport:
        """살아서 실패 결과를 돌려주는 peer — `_send` 가 A2A_003 으로 옮긴다."""

        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, *, callee: str, task: Any, token: str, headers: Any) -> TaskResult:
            self.sent.append(task.task_id)
            return TaskResult.failed(
                task,
                AaestroError(
                    category=ErrorCategory.MODEL,
                    code=ErrorCode.LLM_002,
                    message="context length exceeded",
                ),
            )

    transport = RejectingTransport()

    with pytest.raises(AaestroError) as excinfo:
        await client(transport, waits).call("planner", make_task())

    assert excinfo.value.code == ErrorCode.A2A_003
    assert len(transport.sent) == 1
    assert waits == []


async def test_retry_is_off_unless_the_assembly_turns_it_on(waits):
    """미주입이면 재시도하지 않는다 — 조립하는 쪽이 켠다."""
    transport = FlakyTransport(failures=1)

    with pytest.raises(AaestroError):
        await client(transport, waits, retry=None).call("planner", make_task())

    assert len(transport.sent) == 1


async def test_exhausted_retries_surface_the_last_error(waits):
    """감싸서 올리면 호출자의 카테고리 기반 처리가 깨진다."""
    transport = FlakyTransport(failures=99)

    with pytest.raises(AaestroError) as excinfo:
        await client(transport, waits).call("planner", make_task())

    assert excinfo.value.code == ErrorCode.A2A_002
    assert len(transport.sent) == A2A_RETRY.max_attempts


async def test_the_default_network_policy_would_also_claim_it(waits):
    """05 의 표준 정책으로도 도달 실패는 재시도 대상이어야 한다."""
    unreachable = AaestroError(
        category=ErrorCategory.NETWORK,
        code=ErrorCode.NET_001,
        message="connection refused",
        retryable=True,
    )

    assert NETWORK_RETRY.should_retry(unreachable)
