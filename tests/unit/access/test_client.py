"""The enforcement point's decision cache — invalidation and registry outages (#278)."""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
import pytest

from aaestro.access.client import AccessClient, DecisionSource
from aaestro.access.model import Mode, ResourceKind
from aaestro.observability.metrics import Metrics
from tests.fixtures.waiting import until

SPACE = "group:research:knowledge"


class FakeRegistry:
    """레지스트리 대역 — 답과 버전을 바꾸고, 멈추고, 받은 요청을 센다."""

    def __init__(self) -> None:
        self.version = 1
        self.allowed = True
        self.valid_until: float | None = None
        self.agent: str | None = "worker"
        self.down = False
        self.feed_down = False
        self.status: int | None = None  # 설정하면 모든 요청에 이 상태로 답한다
        self.calls: list[str] = []
        self.changed = asyncio.Event()

    def move(self) -> None:
        self.version += 1
        self.changed.set()
        self.changed = asyncio.Event()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if self.down or (self.feed_down and path == "/v1/access/changes"):
            raise httpx.ConnectError("registry is down")
        if self.status is not None:
            return httpx.Response(self.status, json={"detail": "nope"})
        assert request.headers["authorization"] == "Bearer enforcer"
        if path == "/v1/access/changes":
            after = int(request.url.params["after"])
            if self.version <= after:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.changed.wait(), 0.05)
            return httpx.Response(200, json={"version": self.version})
        if path == "/v1/access/identities":
            return httpx.Response(200, json={"agent": self.agent, "version": self.version})
        body = json.loads(request.content)
        assert body["credential"] == "cred"
        return httpx.Response(
            200,
            json={
                "agent": self.agent,
                "decision": "allow" if self.allowed else "deny",
                "decided_by": "declaration",
                "version": self.version,
                "valid_until": self.valid_until,
            },
        )

    def decisions(self) -> int:
        return self.calls.count("/v1/access/decisions")


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def client(registry, clock):
    metrics = Metrics()

    async def no_sleep(_: float) -> None:
        await asyncio.sleep(0)

    found = AccessClient(
        base_url="http://cp",
        enforcer_token="enforcer",
        component="memory",
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(registry.handle), base_url="http://cp"
        ),
        clock=clock,
        sleep=no_sleep,
        metrics=metrics,
    )
    yield found
    await found.aclose()


async def decide(client: AccessClient, mode: Mode = Mode.RO):
    return await client.decide("cred", ResourceKind.MEMORY, SPACE, mode)


@contextlib.asynccontextmanager
async def watching(client: AccessClient, registry: FakeRegistry):
    task = asyncio.create_task(client.watch())
    try:
        await until(lambda: client._feed_alive)  # noqa: SLF001
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# --- 캐시 ---------------------------------------------------------------------


async def test_a_decision_is_asked_once_and_then_served_from_the_cache(client, registry):
    first = await decide(client)
    second = await decide(client)

    assert (first.allowed, first.source) == (True, DecisionSource.FRESH)
    assert (second.allowed, second.source) == (True, DecisionSource.CACHE)
    assert registry.decisions() == 1


async def test_without_the_change_feed_a_cached_decision_is_rechecked_after_two_seconds(
    client, registry, clock
):
    await decide(client)
    registry.allowed = False
    clock.now += 1.9
    assert (await decide(client)).allowed, "재확인 주기 전에 다시 물었다"

    clock.now += 0.2

    assert not (await decide(client)).allowed
    assert registry.decisions() == 2


async def test_with_the_change_feed_a_change_drops_the_cache_at_once(client, registry, clock):
    async with watching(client, registry):
        await decide(client)
        clock.now += 60  # 알림이 살아 있으면 시간만으로는 다시 묻지 않는다
        assert (await decide(client)).source is DecisionSource.CACHE

        registry.allowed = False
        registry.move()
        await until(lambda: client._version == registry.version)  # noqa: SLF001

        assert not (await decide(client)).allowed
    assert (
        client.metrics.counter("aaestro_access_cache_invalidations_total")
        .labels(component="memory")
        ._value.get()
        == 1
    )


async def test_an_answer_older_than_the_last_notification_is_not_cached(client, registry):
    async with watching(client, registry):
        registry.version = 0  # 느린 판정 응답이 알림보다 옛 버전을 들고 온다

        assert (await decide(client)).source is DecisionSource.FRESH
        assert (await decide(client)).source is DecisionSource.FRESH


async def test_identities_are_cached_and_dropped_like_decisions(client, registry):
    async with watching(client, registry):
        assert await client.identify("cred") == ("worker", DecisionSource.FRESH)
        assert await client.identify("cred") == ("worker", DecisionSource.CACHE)

        registry.agent = None  # 해체로 신원 폐기
        registry.move()
        await until(lambda: client._version == registry.version)  # noqa: SLF001

        assert await client.identify("cred") == (None, DecisionSource.FRESH)


# --- 레지스트리 장애 -----------------------------------------------------------


async def test_an_uncached_decision_is_denied_while_the_registry_is_unreachable(client, registry):
    registry.down = True

    verdict = await decide(client)

    assert (verdict.allowed, verdict.source) == (False, DecisionSource.UNREACHABLE)
    assert (
        client.metrics.gauge("aaestro_access_registry_reachable")
        .labels(component="memory")
        ._value.get()
        == 0
    )


async def test_a_cached_allow_keeps_working_while_the_registry_is_unreachable(
    client, registry, clock
):
    await decide(client)
    registry.down = True
    clock.now += 3600  # 재확인 주기를 한참 넘겨도

    verdict = await decide(client)

    assert (verdict.allowed, verdict.source) == (True, DecisionSource.CACHE)
    assert not (await decide(client, Mode.RW)).allowed, "캐시에 없는 쓰기 판정이 허용됐다"


async def test_a_cached_decision_is_not_used_past_its_expiry_even_during_an_outage(
    client, registry, clock
):
    registry.valid_until = clock.now + 60
    await decide(client)
    registry.down = True

    clock.now += 61

    assert (await decide(client)).source is DecisionSource.UNREACHABLE


async def test_a_lost_feed_falls_back_to_rechecking(client, registry, clock):
    async with watching(client, registry):
        await decide(client)
        registry.feed_down = True  # 판정은 닿지만 알림만 끊겼다
        await until(lambda: not client._feed_alive)  # noqa: SLF001
        registry.allowed = False

        clock.now += 1.9
        assert (await decide(client)).allowed
        clock.now += 0.2
        assert not (await decide(client)).allowed


# --- 거절은 장애가 아니다 (#289 리뷰) ----------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 400])
async def test_a_registry_that_rejects_the_enforcement_point_gets_no_cached_allow(
    client, registry, clock, status
):
    """틀린 강제 지점 토큰을 장애로 보면 옛 허용을 계속 쓴다 — 설정 실수가 권한 우회가 된다."""
    await decide(client)
    registry.status = status
    clock.now += 3

    verdict = await decide(client)

    assert (verdict.allowed, verdict.source) == (False, DecisionSource.UNREACHABLE)
    registry.status = None
    assert (await decide(client)).source is DecisionSource.FRESH, "거절당한 판정이 캐시에 남았다"


async def test_a_server_error_is_an_outage_and_keeps_the_cached_allow(client, registry, clock):
    await decide(client)
    registry.status = 503
    clock.now += 3

    assert (await decide(client)).source is DecisionSource.CACHE


async def test_a_rejected_change_feed_drops_the_cache(client, registry):
    async with watching(client, registry):
        await decide(client)
        registry.status = 401
        await until(lambda: not client._decisions)  # noqa: SLF001
