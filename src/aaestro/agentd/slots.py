"""Concurrency slots held by running tasks.

실행 중 태스크가 쥔 동시 실행 칸. 칸은 **실행 가능한 일** 에만 쓰인다 — peer 의 답을 기다리는
동안 (나가는 A2A 위임) 은 칸을 놓고, 돌아오면 같은 마감 안에서 다시 잡는다. 칸을 쥔 채 기다리면
A 가 B 에 위임하고 B 가 A 로 돌려보낸 태스크가 그 칸을 기다리다 교착한다 (#416).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass
class Slot:
    """한 실행이 얻은 칸 — 빌려준 동안 ``held`` 가 거짓이다 (놓을 칸이 없다).

    재획득은 실행당 한 번만 진행한다 (``taking``) — 겹친 빌려주기가 각자 잡으면 칸을 두 번
    잡거나 (놓는 것은 한 번이라 칸 손실), 상한 1 에서 자기 실행의 칸을 기다려 멈춘다.
    """

    semaphore: asyncio.Semaphore
    deadline: float
    held: bool = True
    lent: int = 0
    closed: bool = False
    taking: asyncio.Future[None] | None = field(default=None, repr=False)

    def release(self) -> None:
        """실행이 끝나 칸을 놓는다 — 빌려준 채 끝났으면 놓을 것이 없다. 뒤의 빌려주기는 무시된다."""
        self.closed = True
        if self.held:
            self.held = False
            self.semaphore.release()

    async def take_back(self) -> None:
        """빌려준 칸을 다시 잡는다 — 진행 중인 재획득이 있으면 그것을 기다리기만 한다."""
        if self.taking is None:
            self.taking = asyncio.ensure_future(self._acquire())
            # 기다리던 쪽이 모두 떠나도 결과를 거둔다 — "never retrieved" 로 새지 않게
            self.taking.add_done_callback(lambda done: done.cancelled() or done.exception())
        await asyncio.shield(self.taking)

    async def _acquire(self) -> None:
        try:
            async with asyncio.timeout_at(self.deadline):
                await self.semaphore.acquire()
        finally:
            self.taking = None
        if self.closed or self.lent:
            # 잡는 사이 실행이 끝났거나 다시 빌려주기 시작했다 — 쥐지 않는다
            # (다시 빌려준 쪽은 마지막 빌려주기가 잡는다)
            self.semaphore.release()
            return
        self.held = True


HELD_SLOT: ContextVar[Slot | None] = ContextVar("aaestro_held_slot", default=None)
"""지금 태스크가 쥔 칸 — runtime 이 실행 직전에 싣는다. 없으면 빌려줄 칸도 없다."""


@asynccontextmanager
async def lend_slot() -> AsyncIterator[None]:
    """Give up this task's slot while waiting on a peer, and take it back afterwards.

    peer 를 기다리는 동안 칸을 놓고, 끝나면 같은 마감 안에서 다시 잡는다. 겹친 대기 (병렬 도구) 는
    수를 세어 처음에 놓고 마지막에 잡는다. 실행이 끝난 뒤 (같은 문맥을 물려받은 태스크) 의
    빌려주기는 아무 일도 하지 않는다.

    Raises:
        AaestroError: TIMEOUT/``TO_001`` if the slot cannot be taken back before the deadline.
    """
    slot = HELD_SLOT.get()
    if slot is None or slot.closed:
        yield
        return
    slot.lent += 1
    if slot.held:
        slot.held = False
        slot.semaphore.release()
    try:
        yield
    finally:
        slot.lent -= 1
        if not slot.lent and not slot.held and not slot.closed:
            try:
                await slot.take_back()
            except TimeoutError as err:
                # shortcut: 재획득 TO_001 뒤 실행은 칸 없이 계속 돈다 — 실행기 마감이 같은 시각이라
                # 곧 끝난다. 마감이 갈라지면 (도구 결과로 받고 계속 도는 경로) 실행을 끊도록 바꾼다
                raise AaestroError(
                    category=ErrorCategory.TIMEOUT,
                    code=ErrorCode.TO_001,
                    message="task timeout exceeded while taking its slot back",
                    retryable=True,
                ) from err
