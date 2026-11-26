"""실행 칸 빌려주기 (#416) — 겹친 빌려주기와 실행 종료 뒤의 빌려주기."""

from __future__ import annotations

import asyncio

import pytest

from aaestro.agentd.slots import HELD_SLOT, Slot, lend_slot
from tests.fixtures.waiting import spin, until


async def _overlap_during_take_back(limit: int) -> tuple[asyncio.Semaphore, Slot]:
    """도구 b 가 칸 재획득을 기다리는 사이 병렬 도구 c 가 빌려주기를 시작 · 종료한다."""
    semaphore = asyncio.Semaphore(limit)
    await semaphore.acquire()  # 이 실행의 칸
    slot = Slot(semaphore, asyncio.get_running_loop().time() + 5)
    HELD_SLOT.set(slot)
    lending, answered = asyncio.Event(), asyncio.Event()

    async def tool_b() -> None:
        async with lend_slot():
            lending.set()
            await answered.wait()

    async def tool_c() -> None:
        async with lend_slot():
            pass

    b = asyncio.create_task(tool_b())
    await lending.wait()
    # 빌려준 사이 다른 실행이 남은 칸을 모두 잡는다 — b 의 재획득이 기다리게
    for _ in range(limit):
        await semaphore.acquire()
    answered.set()
    await until(lambda: slot.taking is not None)
    c = asyncio.create_task(tool_c())
    await spin(10)  # c 가 빌려주기를 끝내고 재획득에 닿을 때까지
    for _ in range(limit):
        semaphore.release()

    await asyncio.wait_for(asyncio.gather(b, c), timeout=2)
    return semaphore, slot


@pytest.mark.parametrize("limit", [1, 2])
async def test_overlapping_lends_take_the_slot_back_once(limit):
    """재획득이 실행당 하나가 아니면 상한 1 은 자기 칸을 기다려 멈추고, 상한 2 는 칸을 잃는다."""
    semaphore, slot = await _overlap_during_take_back(limit)

    assert slot.held
    assert semaphore._value == limit - 1
    slot.release()
    assert semaphore._value == limit  # 종료 뒤 원래대로


async def test_lending_after_the_run_ended_does_nothing():
    """실행이 끝난 뒤 같은 문맥을 물려받은 태스크의 빌려주기가 칸을 잡고 아무도 놓지 않으면 샌다."""
    semaphore = asyncio.Semaphore(2)
    await semaphore.acquire()
    slot = Slot(semaphore, asyncio.get_running_loop().time() + 5)
    HELD_SLOT.set(slot)
    slot.release()

    async with lend_slot():
        pass

    assert semaphore._value == 2
    assert not slot.held
