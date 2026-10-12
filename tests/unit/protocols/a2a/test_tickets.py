"""The caller's ticket source (#281)."""

from __future__ import annotations

import httpx
import pytest

from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.protocols.a2a.tickets import RENEW_BEFORE_S, TicketSource


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def source(handler, clock: Clock | None = None) -> TicketSource:
    return TicketSource(
        agent="researcher",
        base_url="http://cp",
        credential="researcher-identity",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://cp"),
        clock=clock or Clock(),
    )


async def test_a_ticket_is_reused_until_it_is_about_to_expire():
    issued = []
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer researcher-identity"
        issued.append(f"t{len(issued)}")
        return httpx.Response(201, json={"ticket": issued[-1], "expires_at": clock.now + 300})

    tickets = source(handler, clock)

    assert await tickets.ticket_for("planner") == "t0"
    clock.now += 300 - RENEW_BEFORE_S - 1
    assert await tickets.ticket_for("planner") == "t0"
    clock.now += 2
    assert await tickets.ticket_for("planner") == "t1", "만료 직전 표로 부르면 도착 전에 만료된다"
    assert await tickets.ticket_for("writer") == "t2", "표는 피호출자마다 따로다"


@pytest.mark.parametrize(
    ("respond", "code", "retryable"),
    [
        (lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")), ErrorCode.A2A_002, True),
        (lambda r: httpx.Response(503), ErrorCode.A2A_002, True),
        (lambda r: httpx.Response(403, json={}), ErrorCode.A2A_004, False),
        (lambda r: httpx.Response(404, json={}), ErrorCode.A2A_004, False),
    ],
)
async def test_failing_to_get_a_ticket_is_classified(respond, code, retryable):
    with pytest.raises(AaestroError) as exc_info:
        await source(respond).ticket_for("planner")

    assert (exc_info.value.code, exc_info.value.retryable) == (code, retryable)


async def test_concurrent_first_calls_share_one_ticket():
    """모두가 새 표를 받으면 간선마다 남기는 수를 넘겨 진행 중 호출의 표가 지워진다 (#291 리뷰)."""
    import asyncio

    issued = []
    clock = Clock()

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        issued.append(f"t{len(issued)}")
        return httpx.Response(201, json={"ticket": issued[-1], "expires_at": clock.now + 300})

    tickets = source(handler, clock)

    got = await asyncio.gather(*(tickets.ticket_for("planner") for _ in range(8)))

    assert issued == ["t0"] and set(got) == {"t0"}
