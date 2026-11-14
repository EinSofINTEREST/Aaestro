"""Waiting for a condition in async tests, without counting loop turns.

회차(event loop turn)를 세는 대기는 **동기화가 아니다** — 부하가 걸린 CI 에서
몇 회차 안에 조건이 설지는 보장되지 않는다. 회차를 다 쓰고 조용히 반환하면,
뒤따르는 단언이 엉뚱한 자리에서 터진다 (#215 의 CI 실패가 그랬다:
`replicas_of("echo")[0]` 이 재시작 도중의 빈 목록을 집었다).

여기 둘은 **의도가 반대**라 이름을 나눈다:

- `until` — 일어나야 할 일을 기다린다. 서지 않으면 그 사실로 실패한다
- `spin`  — 일어나지 *않음*을 확인한다. 정해진 회차만큼 양보하고 돌아온다
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_TIMEOUT_S = 5.0
POLL_S = 0.001


async def until(predicate: Callable[[], bool], *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
    """Wait for a condition, failing loudly if it never holds.

    조건이 설 때까지 기다립니다. 마감까지 서지 않으면 `AssertionError` 로
    그 사실을 말합니다 — 조용히 돌아가면 다음 단언이 원인을 가린다.

    Args:
        predicate: Condition to wait for.
        timeout_s: Deadline. 통과하는 경로는 첫 회차에 끝나므로 넉넉해도 느려지지 않는다.

    Raises:
        AssertionError: If the condition does not hold before the deadline.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"condition did not hold within {timeout_s}s")
        await asyncio.sleep(POLL_S)


async def reached(
    event: asyncio.Event, task: asyncio.Future, *, timeout_s: float = DEFAULT_TIMEOUT_S
) -> None:
    """Wait for ``event`` while the task that should set it is still running.

    이벤트를 세워야 할 태스크가 먼저 끝났다면 (예외로 죽은 경우) 그 예외를 바로 올린다 —
    상한 없는 ``event.wait()`` 는 그 실패를 hang 으로 바꾼다 (#568).

    Raises:
        AssertionError: If the task ended without setting the event, or the deadline passed.
    """
    waiter = asyncio.ensure_future(event.wait())
    try:
        await asyncio.wait({waiter, task}, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
    if event.is_set():
        return
    if task.done() and not task.cancelled() and task.exception() is not None:
        raise task.exception()
    outcome = task.result() if task.done() and not task.cancelled() else "still running"
    raise AssertionError(f"event was not set within {timeout_s}s (task: {outcome!r})")


async def spin(rounds: int) -> None:
    """Yield the event loop a fixed number of times.

    event loop 을 정해진 횟수만큼 양보합니다 — *일어나지 않음*을 확인할 때 씁니다.
    기다릴 조건이 있다면 `until` 을 쓰세요.
    """
    for _ in range(rounds):
        await asyncio.sleep(0)


__all__ = ["DEFAULT_TIMEOUT_S", "reached", "spin", "until"]
