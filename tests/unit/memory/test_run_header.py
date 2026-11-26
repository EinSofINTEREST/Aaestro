"""Run scope spaces through the Memory Service — the ``X-Aaestro-Run`` header (#609).

실제 레지스트리 + run 저장소 + control plane 권한 라우트 + Memory Service 를 ASGI 로 잇는다.
헤더는 run 을 고르는 선택자이고, 닿는지는 레지스트리가 run 기록으로 판정한다 (#608).
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from aaestro.access.baselines import MemoryBaseline
from aaestro.access.client import AccessClient, DecisionSource
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


@dataclass
class InRun:
    """한 태스크의 run 을 매 호출 인자로 싣는 래퍼 — 클라이언트는 run 을 쥐지 않는다 (#610)."""

    access: HttpMemoryAccess
    run_id: str | None

    async def append(self, space: str, **kwargs):
        return await self.access.append(space, run_id=self.run_id, **kwargs)

    async def search(self, query: str, **kwargs):
        return await self.access.search(query, run_id=self.run_id, **kwargs)

    async def read(self, space: str, **kwargs):
        return await self.access.read(space, run_id=self.run_id, **kwargs)

    async def latest(self, space: str, entry_id: str):
        return await self.access.latest(space, entry_id, run_id=self.run_id)

    async def spaces(self):
        return await self.access.spaces(run_id=self.run_id)


class Stack:
    def __init__(self, root: Path = REPO_ROOT) -> None:
        catalog = Catalog.under(root)
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
        self.memory = build_deployment(config, root=root, access=self.client)
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.memory.app), base_url="http://memory"
        )

    def start(self, run_id: str = RUN) -> str:
        self.manager.acquire(make_mission(memory={"spaces": [SCRATCH]}), run_id=run_id)
        return run_id

    def agent(self, name: str, *, run_id: str | None = RUN, graph: str = GRAPH) -> InRun:
        credential = self.registry.issue_identity(name, f"dep-{name}-{graph}", graph=graph)
        access = HttpMemoryAccess(base_url="http://memory", token=credential, client=self.http)
        return InRun(access, run_id)

    async def settled(self) -> None:
        await until(lambda: self.client._version == self.registry.version())  # noqa: SLF001


@contextlib.asynccontextmanager
async def watched(root: Path = REPO_ROOT):
    found = Stack(root)
    watching = asyncio.create_task(found.client.watch())
    try:
        yield found
    finally:
        watching.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watching
        await found.client.aclose()


@pytest.fixture
async def stack():
    async with watched() as found:
        yield found


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


async def test_latest_reaches_a_live_runs_space(stack):
    stack.start()
    await stack.settled()
    planner = stack.agent("planner")
    stored = await planner.append("scratch", **fact())

    found = await planner.latest("scratch", stored.entry_id)

    assert found is not None and found.entry_id == stored.entry_id


async def test_a_revoked_run_space_is_refused_while_local_spaces_still_search(stack):
    stack.start()
    planner = stack.agent("planner")
    stack.registry.revoke("planner", ResourceKind.MEMORY, f"run:{RUN}:scratch", reason="incident")
    await stack.settled()

    error = await refused(planner.search("관찰", spaces=["scratch", "longterm"]))
    assert error.code == ErrorCode.MEM_001
    assert error.details["memory_space"] == "scratch"
    await planner.search("관찰", spaces=["longterm"])
    await planner.search("관찰")  # 지정하지 않으면 회수된 run space 만 건너뛴다
    assert {s["alias"] for s in await planner.spaces()} >= {"longterm"}
    assert "scratch" not in {s["alias"] for s in await planner.spaces()}


async def test_another_graphs_run_alias_does_not_shadow_a_group_space(tmp_path):
    """후보는 이 run 의 스냅샷뿐이다 — 무관한 그래프가 같은 이름의 run 별칭을 선언해도 그룹 space 가
    가려지지 않는다 (run > group 우선순위는 이 run 이 실제로 선언한 별칭에만 적용된다)."""
    for name in ("agents", "graphs", "groups", "modules"):
        shutil.copytree(REPO_ROOT / name, tmp_path / name, symlinks=True)
    other = (REPO_ROOT / "graphs" / "feed-monitor.yaml").read_text(encoding="utf-8")
    (tmp_path / "graphs" / "other.yaml").write_text(
        other.replace("name: feed-monitor", "name: other").replace("as: scratch", "as: knowledge"),
        encoding="utf-8",
    )
    async with watched(tmp_path) as stack:
        stack.start()
        await stack.settled()

        await stack.agent("planner").read("knowledge")


async def test_a_node_of_a_run_without_run_spaces_keeps_the_run_as_provenance(stack):
    stack.manager.acquire(make_mission(), run_id="run-2")  # run space 를 선언하지 않은 그래프
    await stack.settled()

    node = await stack.agent("planner", run_id="run-2").append("longterm", **fact())
    outsider = await stack.agent("writer", run_id="run-2").append("longterm", **fact())

    assert node.source.run_id == "run-2"
    assert outsider.source.run_id is None


async def test_an_unknown_credential_has_no_run(stack):
    stack.start()
    await stack.settled()

    node, spaces, _ = await stack.client.run_participation("made-up", RUN)

    assert (node, spaces) == (False, {})


async def test_a_malformed_run_space_from_the_registry_closes_to_refusal(stack, monkeypatch):
    """레지스트리 답의 ref 모양이 틀리면 그 별칭만 후보에서 빠진다 — 500 이 아니라 MEM_001."""
    stack.start()
    await stack.settled()

    async def broken(credential: str, run_id: str):
        return True, {"scratch": "not a module ref"}, DecisionSource.FRESH

    monkeypatch.setattr(stack.client, "run_participation", broken)
    planner = stack.agent("planner")

    assert (await refused(planner.append("scratch", **fact()))).code == ErrorCode.MEM_001
    stored = await planner.append("longterm", **fact())
    assert stored.source.run_id == RUN, "출처의 run 은 노드 답대로 남는다"


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


async def test_a_direct_run_header_is_not_looked_up(stack, monkeypatch):
    """``direct-`` 헤더는 레지스트리에 묻지 않는다 — run 캐시가 direct 마다 자라지 않게 (#610)."""
    asked: list[str] = []

    async def counted(credential: str, run_id: str):
        asked.append(run_id)
        return False, {}, DecisionSource.FRESH

    monkeypatch.setattr(stack.client, "run_participation", counted)
    credential = stack.registry.issue_identity("planner", "dep-planner-direct", graph=GRAPH)

    response = await stack.http.get(
        "/v1/spaces",
        headers={"authorization": f"Bearer {credential}", MEMORY_RUN_HEADER: "direct-7"},
    )

    assert response.status_code == 200
    assert asked == []


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
    service = MemoryService(store=store)
    tokens = TokenRegistry()
    secret = tokens.issue(
        build_token(agent="researcher", group=None, local=[("longterm", "researcher")])
    )
    indexer = IndexRegistry()
    app = create_app(service, Recall(indexes=indexer.indexes, store=store), tokens)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://memory")
    try:
        stored = await HttpMemoryAccess(base_url="http://memory", token=secret, client=http).append(
            "longterm", run_id=RUN, **fact()
        )
        assert stored.source.run_id is None, "토큰 모드의 토큰은 run 을 모른다"

        error = await refused(
            HttpMemoryAccess(base_url="http://memory", token=secret, client=http).read(
                "longterm", run_id="bad\nrun"
            )
        )
        assert error.code == ErrorCode.VAL_002
        assert service.audit_log[-1]["op"] == "read"
        assert service.audit_log[-1]["status"] == "failed"

        # 연산이 아닌 경로 (spaces) 는 원래 감사 줄을 남기지 않는다 — 09 op 어휘 밖의 값 금지
        before = len(service.audit_log)
        response = await http.get(
            "/v1/spaces",
            headers={"authorization": f"Bearer {secret}", MEMORY_RUN_HEADER: ""},
        )
        assert response.status_code == 400
        assert len(service.audit_log) == before
    finally:
        store.close()
