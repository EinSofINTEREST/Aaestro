"""The wait helpers themselves — a silent give-up is the bug being prevented.

회차를 세는 대기가 조용히 포기해 CI 를 깼다 (#215 / #224). 그 대체물이 같은
성질을 갖지 않는지 여기서 못 박는다 — 헬퍼가 조용해지면 이 파일이 먼저 운다.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.fixtures.waiting import reached, spin, until


async def test_until_returns_once_the_condition_holds():
    calls = {"n": 0}

    def eventually() -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    await until(eventually)

    assert calls["n"] == 3


async def test_until_fails_loudly_when_the_condition_never_holds():
    """#224 — 조용히 반환하면 뒤따르는 단언이 원인을 가린다."""
    with pytest.raises(AssertionError, match="did not hold"):
        await until(lambda: False, timeout_s=0.01)


async def test_until_checks_before_waiting():
    """이미 서 있는 조건에 마감을 쓰지 않는다 — 통과 경로는 즉시 끝나야 한다."""
    await until(lambda: True, timeout_s=0.0)


async def test_spin_yields_and_returns():
    """*일어나지 않음*을 확인하는 자리 — 조건이 없으므로 실패하지 않는다."""
    ticks = 0

    async def counter() -> None:
        nonlocal ticks
        for _ in range(10):
            ticks += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(counter())
    await spin(20)
    await task

    assert ticks == 10


async def test_reached_returns_when_the_task_sets_the_event():
    event = asyncio.Event()

    async def work() -> None:
        event.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(work())
    await reached(event, task)
    task.cancel()


async def test_reached_raises_the_tasks_own_failure_instead_of_hanging():
    """#568 — 이벤트를 세우기 전에 죽은 태스크의 예외가 그대로 보여야 한다."""

    async def dies() -> None:
        raise KeyError("lookup")

    with pytest.raises(KeyError):
        await reached(asyncio.Event(), asyncio.create_task(dies()))


async def test_reached_fails_loudly_at_the_deadline():
    task = asyncio.create_task(asyncio.sleep(3600))
    with pytest.raises(AssertionError, match="not set within"):
        await reached(asyncio.Event(), task, timeout_s=0.01)
    task.cancel()


async def test_reached_fails_when_the_task_ends_quietly_without_the_event():
    async def quiet() -> None:
        return None

    with pytest.raises(AssertionError, match="not set within"):
        await reached(asyncio.Event(), asyncio.create_task(quiet()))
