"""Run scope spaces through the Memory Service — the ``X-Aaestro-Run`` header (#609).

실제 레지스트리 + run 저장소 + control plane 권한 라우트 + Memory Service 를 ASGI 로 잇는다.
헤더는 run 을 고르는 선택자이고, 닿는지는 레지스트리가 run 기록으로 판정한다 (#608).
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import httpx
import pytest

from aaestro.access.baselines import MemoryBaseline
from aaestro.access.client import AccessClient
from aaestro.access.model import ResourceKind
from aaestro.access.registry import AccessRegistry
from aaestro.access.store import InMemoryAccessStore
from aaestro.catalog import Catalog
from aaestro.config import load_config
from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.memory.bootstrap import build_deployment
from aaestro.memory.client import MEMORY_RUN_HEADER, HttpMemoryAccess
from aaestro.modules.memoryset import MemoryKind
from aaestro.orchestrator.control import create_app as control_app
from aaestro.orchestrator.run import RunManager, RunStatus
from aaestro.orchestrator.runstore import InMemoryRunStore, RunRecord
from tests.fixtures.topologies import make_mission
from tests.fixtures.waiting import until

REPO_ROOT = Path(__file__).resolve().parents[3]
ENFORCER = "enforcer-token"
GRAPH = "research-pipeline"
SCRATCH = {"ref": "memorysets/run-scratch@0.1.0", "as": "scratch"}
RUN = "run-1"


class Stack:
    def __init__(self) -> None:
        catalog = Catalog.under(REPO_ROOT)
        self.runs = InMemoryRunStore()
        self.manager = RunManager(store=self.runs)
        self.registry = AccessRegistry(
            store=InMemoryAccessStore(),
            catalog=catalog,
            baselines={ResourceKind.MEMORY: MemoryBaseline(catalog, runs=self.runs)},
            declarations_poll_s=0.0,
            runs=self.runs,
        )
        self.registry_down = False
        asgi = httpx.ASGITransport(
            app=control_app(
                self.runs,
                catalog=catalog,
                token="control-token",
                access=self.registry,
                enforcer_token=ENFORCER,
            )
        )

        async def to_registry(request: httpx.Request) -> httpx.Response:
            if self.registry_down:
                raise httpx.ConnectError("control plane is down")
            return await asgi.handle_async_request(request)

        self.client = AccessClient(
            base_url="http://cp",
            enforcer_token=ENFORCER,
            component="memory",
            http=httpx.AsyncClient(
                transport=httpx.MockTransport(to_registry), base_url="http://cp"
            ),
            feed_wait_s=0.2,
            recheck_s=0.05,
        )
        config = load_config("dev", config_dir=REPO_ROOT / "configs", environ={})
        self.memory = build_deployment(config, root=REPO_ROOT, access=self.client)
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.memory.app), base_url="http://memory"
        )

    def start(self, run_id: str = RUN) -> str:
        self.manager.acquire(make_mission(memory={"spaces": [SCRATCH]}), run_id=run_id)
        return run_id

    def agent(self, name: str, *, run_id: str | None = RUN, graph: str = GRAPH) -> HttpMemoryAccess:
        credential = self.registry.issue_identity(name, f"dep-{name}-{graph}", graph=graph)
        return HttpMemoryAccess(
            base_url="http://memory", token=credential, client=self.http, run_id=run_id
        )

    async def settled(self) -> None:
        await until(lambda: self.client._version == self.registry.version())  # noqa: SLF001


@pytest.fixture
async def stack():
    found = Stack()
    watching = asyncio.create_task(found.client.watch())
    try:
        yield found
    finally:
        watching.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watching
        await found.client.aclose()


def fact(content: str = "run 안의 관찰") -> dict:
    return {"kind": MemoryKind.FACT, "content": content}


async def refused(call) -> AaestroError:
    with pytest.raises(AaestroError) as exc_info:
        await call
    return exc_info.value


# --- 완료 조건 1 — 진행 중 run ----------------------------------------------------


async def test_a_node_agent_appends_and_searches_its_live_runs_space(stack):
    stack.start()
    await stack.settled()
    planner = stack.agent("planner")

    stored = await planner.append("scratch", **fact())
    await planner.search("관찰", spaces=["scratch"])  # 색인은 비동기라 결과가 아니라 허용을 본다

    assert stored.space == f"run:{RUN}:scratch"
    assert stored.source.run_id == RUN, "출처의 run 은 레지스트리가 참여를 인정한 run 이다"
    assert any(s["alias"] == "scratch" and s["mode"] == "rw" for s in await planner.spaces())


async def test_without_the_header_the_run_space_is_not_declared(stack):
    stack.start()
    await stack.settled()

    error = await refused(stack.agent("planner", run_id=None).append("scratch", **fact()))

    assert error.code == ErrorCode.MEM_001


# --- 완료 조건 2 — 끝난 run 은 읽기 전용 --------------------------------------------


async def test_an_ended_runs_space_refuses_writes_and_keeps_reads(stack):
    stack.start()
    await stack.settled()
    planner = stack.agent("planner")
    stored = await planner.append("scratch", **fact())

    stack.manager.release(RUN, RunStatus.COMPLETED)
    await stack.settled()

    assert (await refused(planner.append("scratch", **fact("b")))).code == ErrorCode.MEM_001
    assert [e.entry_id for e in await planner.read("scratch")] == [stored.entry_id]
    await planner.search("관찰", spaces=["scratch"])
    modes = {s["alias"]: s["mode"] for s in await planner.spaces()}
    assert modes["scratch"] == "ro"


# --- 완료 조건 3 — 참여자가 아닌 신원 ------------------------------------------------


async def test_an_agent_that_is_not_a_node_is_refused(stack):
    stack.start()
    await stack.settled()
    writer = stack.agent("writer")  # make_mission 의 노드는 planner · researcher 뿐이다

    assert (await refused(writer.append("scratch", **fact()))).code == ErrorCode.MEM_001
    assert (await refused(writer.read("scratch"))).code == ErrorCode.MEM_001
    assert "scratch" not in {s["alias"] for s in await writer.spaces()}


async def test_a_non_participant_cannot_stamp_the_run_on_its_own_memory(stack):
    """헤더는 주장이다 — 참여가 확인되지 않으면 출처에 run 이 실리지 않는다 (09 Entry Rules 4)."""
    stack.start()
    await stack.settled()

    stored = await stack.agent("writer").append("longterm", **fact())

    assert stored.source.run_id is None


async def test_an_identity_deployed_for_another_graph_is_refused(stack):
    stack.start()
    await stack.settled()
    other = stack.agent("planner", graph="feed-monitor")

    assert (await refused(other.append("scratch", **fact()))).code == ErrorCode.MEM_001


async def test_a_direct_run_has_no_run_space(stack):
    stack.runs.upsert(
        RunRecord(
            run_id="direct-1",
            graph=GRAPH,
            mode="mission",
            status="running",
            memory_spaces={"scratch": SCRATCH["ref"]},
            agents=("planner",),
        )
    )
    await stack.settled()
    planner = stack.agent("planner", run_id="direct-1")

    assert (await refused(planner.append("scratch", **fact()))).code == ErrorCode.MEM_001
    stored = await planner.append("longterm", **fact())
    assert stored.source.run_id is None


async def test_an_unknown_run_is_refused(stack):
    await stack.settled()

    error = await refused(stack.agent("planner", run_id="never-ran").read("scratch"))

    assert error.code == ErrorCode.MEM_001


# --- 완료 조건 4 — 레지스트리 장애 ----------------------------------------------------


async def test_while_the_registry_is_down_an_uncached_run_space_is_refused(stack):
    stack.start()
    planner = stack.agent("planner")
    await stack.settled()
    await planner.read("longterm")  # 신원이 캐시된다 — run space 판정은 아직 없다

    stack.registry_down = True
    await until(lambda: not stack.client._feed_alive)  # noqa: SLF001
    await asyncio.sleep(0.1)

    assert (await refused(planner.append("scratch", **fact()))).code == ErrorCode.MEM_001


# --- 완료 조건 5 — 신원이 헤더 · 본문보다 먼저 ------------------------------------------


@pytest.mark.parametrize("header", [None, RUN, " bad\n"])
async def test_without_identity_the_header_and_body_do_not_matter(stack, header):
    headers = {"content-type": "application/json"}
    if header is not None:
        headers[MEMORY_RUN_HEADER] = header

    response = await stack.http.post("/v1/append", content=b"{not json", headers=headers)

    assert response.status_code == 401
    assert response.json()["code"] == ErrorCode.MEM_001


async def test_a_malformed_header_is_val_002_without_the_value(stack):
    credential = stack.registry.issue_identity("planner", "dep-1", graph=GRAPH)

    response = await stack.http.post(
        "/v1/read",
        json={"space": "longterm"},
        headers={"authorization": f"Bearer {credential}", MEMORY_RUN_HEADER: " run-1"},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == ErrorCode.VAL_002
    assert body["details"] == {"field": MEMORY_RUN_HEADER, "reason": "surrounding_whitespace"}
    assert " run-1" not in response.text


# --- 토큰 모드 — 헤더를 무시한다 (09 Access Enforcement 8) -----------------------------


async def test_token_mode_ignores_a_valid_header_and_still_checks_its_form():
    from aaestro.memory.http import TokenRegistry, create_app
    from aaestro.memory.index import IndexRegistry
    from aaestro.memory.recall import Recall
    from aaestro.memory.service import MemoryService, build_token
    from aaestro.memory.store import SqliteMemoryStore

    store = SqliteMemoryStore()
    tokens = TokenRegistry()
    secret = tokens.issue(
        build_token(agent="researcher", group=None, local=[("longterm", "researcher")])
    )
    indexer = IndexRegistry()
    app = create_app(
        MemoryService(store=store), Recall(indexes=indexer.indexes, store=store), tokens
    )
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://memory")
    try:
        stored = await HttpMemoryAccess(
            base_url="http://memory", token=secret, client=http, run_id=RUN
        ).append("longterm", **fact())
        assert stored.source.run_id is None, "토큰 모드의 토큰은 run 을 모른다"

        error = await refused(
            HttpMemoryAccess(
                base_url="http://memory", token=secret, client=http, run_id="bad\nrun"
            ).read("longterm")
        )
        assert error.code == ErrorCode.VAL_002
    finally:
        store.close()
