"""Revoking an A2A connection by editing the graph reaches the callee's cache (#332)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import httpx

from aaestro.access.baselines import A2ABaseline
from aaestro.access.client import AccessClient, DecisionSource
from aaestro.access.model import ResourceKind
from aaestro.access.registry import AccessRegistry
from aaestro.access.store import InMemoryAccessStore
from aaestro.catalog import Catalog
from tests.fixtures.access import access_workspace
from tests.fixtures.waiting import until
from tests.unit.access.test_tickets import REPO_ROOT

CONNECTION = "    - {caller: researcher, callee: planner}\n"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


async def test_dropping_a_connection_from_the_graph_denies_the_next_a2a_decision(tmp_path: Path):
    """그래프 파일만 다시 쓴다 — 레지스트리 버전이 오르고 알림을 따르는 캐시가 거부로 바뀐다."""
    workspace = access_workspace(tmp_path)
    graph = workspace / "graphs" / "research-pipeline.yaml"
    original = (REPO_ROOT / "graphs" / "research-pipeline.yaml").read_text(encoding="utf-8")
    assert CONNECTION in original
    graph.write_text(original, encoding="utf-8")

    clock = Clock()
    store = InMemoryAccessStore()
    catalog = Catalog.under(workspace)
    registry = AccessRegistry(
        store=store,
        catalog=catalog,
        clock=clock,
        baselines={ResourceKind.A2A: A2ABaseline(catalog, store)},
        declarations_poll_s=0,
    )
    credential = registry.issue_identity("researcher", "dep-1", graph="research-pipeline")

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/access/changes":
            after = int(request.url.params["after"])
            version = await registry.wait_for_change(after, timeout_s=0.05)
            return httpx.Response(200, json={"version": version})
        body = json.loads(request.content)
        decision = registry.decide(
            "researcher",
            ResourceKind.A2A,
            body["target"],
            identity=registry.identity_of(body["credential"]),
        )
        return httpx.Response(
            200,
            json={
                "agent": decision.agent,
                "decision": decision.outcome.value,
                "decided_by": decision.decided_by,
                "version": decision.version,
                "valid_until": decision.valid_until,
            },
        )

    async def no_sleep(_: float) -> None:
        await asyncio.sleep(0)

    client = AccessClient(
        base_url="http://cp",
        enforcer_token="enforcer",
        component="a2a",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://cp"),
        clock=clock,
        sleep=no_sleep,
    )
    watcher = asyncio.create_task(client.watch())
    try:
        await until(lambda: client._feed_alive)  # noqa: SLF001
        assert (await client.decide(credential, ResourceKind.A2A, "planner")).allowed
        clock.now += 60  # 알림이 살아 있으면 시간은 캐시를 바꾸지 못한다
        cached = await client.decide(credential, ResourceKind.A2A, "planner")
        assert cached.source is DecisionSource.CACHE and cached.allowed

        before = registry.version()
        # 한 연결을 지우고 다른 연결로 바꿔 쓴다 — connections 가 비면 그래프 자체가 무효다
        other = "    - {caller: writer, callee: planner}\n"
        graph.write_text(original.replace(CONNECTION, other), encoding="utf-8")
        assert registry.version() > before
        await until(lambda: client._version == registry.version())  # noqa: SLF001

        revoked = await client.decide(credential, ResourceKind.A2A, "planner")
        assert (revoked.allowed, revoked.source) == (False, DecisionSource.FRESH)
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        await client.aclose()
