"""Concurrency slots held by running tasks.

실행 중 태스크가 쥔 동시 실행 칸. 칸은 **실행 가능한 일** 에만 쓰인다 — peer 의 답을 기다리는
동안 (나가는 A2A 위임) 은 칸을 놓고, 돌아오면 같은 마감 안에서 다시 잡는다. 칸을 쥔 채 기다리면
A 가 B 에 위임하고 B 가 A 로 돌려보낸 태스크가 그 칸을 기다리다 교착한다 (#416).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass
class Slot:
    """한 실행이 얻은 칸 — 빌려준 동안 ``held`` 가 거짓이다 (놓을 칸이 없다)."""

    semaphore: asyncio.Semaphore
    deadline: float
    held: bool = True
    lent: int = 0

    def release(self) -> None:
        """실행이 끝나 칸을 놓는다 — 빌려준 채 끝났으면 놓을 것이 없다."""
        if self.held:
            self.held = False
            self.semaphore.release()


HELD_SLOT: ContextVar[Slot | None] = ContextVar("aaestro_held_slot", default=None)
"""지금 태스크가 쥔 칸 — runtime 이 실행 직전에 싣는다. 없으면 빌려줄 칸도 없다."""


@asynccontextmanager
async def lend_slot() -> AsyncIterator[None]:
    """Give up this task's slot while waiting on a peer, and take it back afterwards.

    peer 를 기다리는 동안 칸을 놓고, 끝나면 같은 마감 안에서 다시 잡는다. 겹친 대기 (병렬 도구) 는
    수를 세어 처음에 놓고 마지막에 잡는다.

    Raises:
        AaestroError: TIMEOUT/``TO_001`` if the slot cannot be taken back before the deadline.
    """
    slot = HELD_SLOT.get()
    if slot is None:
        yield
        return
    slot.lent += 1
    if slot.lent == 1 and slot.held:
        slot.held = False
        slot.semaphore.release()
    try:
        yield
    finally:
        slot.lent -= 1
        if not slot.lent and not slot.held:
            try:
                async with asyncio.timeout_at(slot.deadline):
                    await slot.semaphore.acquire()
            except TimeoutError as err:
                raise AaestroError(
                    category=ErrorCategory.TIMEOUT,
                    code=ErrorCode.TO_001,
                    message="task timeout exceeded while taking its slot back",
                    retryable=True,
                ) from err
            slot.held = True
