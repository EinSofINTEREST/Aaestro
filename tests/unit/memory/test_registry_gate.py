"""Memory permissions decided by the registry on every request (#278).

실제 레지스트리 + control plane 권한 라우트 + Memory Service 를 ASGI 로 잇는다 — 강제 지점이
판정을 캐시하고, 변경 알림으로 버리고, 레지스트리가 멈추면 규칙대로 거부·유지하는 것까지.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import httpx
import pytest

from aaestro.access.baselines import MemoryBaseline
from aaestro.access.client import AccessClient
from aaestro.access.model import Mode, ResourceKind
from aaestro.access.registry import AccessRegistry
from aaestro.access.store import InMemoryAccessStore
from aaestro.catalog import Catalog
from aaestro.config import load_config
from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.memory.bootstrap import build_deployment
from aaestro.memory.client import HttpMemoryAccess
from aaestro.memory.entry import MemoryEntry, MemorySource
from aaestro.modules.memoryset import MemoryKind
from aaestro.orchestrator.control import create_app as control_app
from aaestro.orchestrator.runstore import InMemoryRunStore
from tests.fixtures.waiting import until

REPO_ROOT = Path(__file__).resolve().parents[3]
LONGTERM = "local:researcher:longterm"
KNOWLEDGE = "group:research:knowledge"
ENFORCER = "enforcer-token"


class Stack:
    def __init__(self) -> None:
        catalog = Catalog.under(REPO_ROOT)
        self.registry = AccessRegistry(
            store=InMemoryAccessStore(),
            catalog=catalog,
            baselines={ResourceKind.MEMORY: MemoryBaseline(catalog)},
        )
        self.registry_down = False
        control = control_app(
            InMemoryRunStore(),
            catalog=catalog,
            token="control-token",
            access=self.registry,
            enforcer_token=ENFORCER,
        )
        asgi = httpx.ASGITransport(app=control)

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
        self.memory_http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.memory.app), base_url="http://memory"
        )

    def agent(self, credential: str) -> HttpMemoryAccess:
        return HttpMemoryAccess(base_url="http://memory", token=credential, client=self.memory_http)


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


def fact(space: str, content: str = "sidecar 이미지는 태그를 고정한다") -> MemoryEntry:
    return MemoryEntry(
        space=space, kind=MemoryKind.FACT, content=content, source=MemorySource(agent="researcher")
    )


async def refused(call) -> AaestroError:
    with pytest.raises(AaestroError) as exc_info:
        await call
    return exc_info.value


async def settled(stack: Stack) -> None:
    """변경 알림이 레지스트리의 현재 버전까지 따라왔다."""
    await until(lambda: stack.client._version == stack.registry.version())  # noqa: SLF001


# --- 신원 ---------------------------------------------------------------------


async def test_declared_spaces_work_with_the_agent_identity_and_no_token_is_issued(stack):
    researcher = stack.agent(stack.registry.issue_identity("researcher", "dep-1"))

    await researcher.append("longterm", entry=fact(LONGTERM))

    assert [e.content for e in await researcher.read("longterm")] == [
        "sidecar 이미지는 태그를 고정한다"
    ]
    assert stack.memory.tokens == {}, "정적 토큰을 함께 발급하면 회수가 닿지 않는 뒷문이 된다"


async def test_an_unknown_or_revoked_identity_is_refused(stack):
    credential = stack.registry.issue_identity("researcher", "dep-1")
    assert (await refused(stack.agent("made-up").spaces())).code == ErrorCode.MEM_001

    stack.registry.revoke_deployment("dep-1")
    await settled(stack)

    assert (await refused(stack.agent(credential).spaces())).code == ErrorCode.MEM_001


# --- 실시간 회수와 강등 ----------------------------------------------------------


async def test_a_revoked_space_is_refused_on_the_next_request(stack):
    researcher = stack.agent(stack.registry.issue_identity("researcher", "dep-1"))
    await researcher.append("longterm", entry=fact(LONGTERM))

    rule = stack.registry.revoke("researcher", ResourceKind.MEMORY, LONGTERM, reason="incident")
    await settled(stack)

    assert (await refused(researcher.append("longterm", entry=fact(LONGTERM, "b")))).code == (
        ErrorCode.MEM_001
    )
    assert (await refused(researcher.read("longterm"))).code == ErrorCode.MEM_001

    stack.registry.lift(rule.rule_id)
    await settled(stack)
    await researcher.append("longterm", entry=fact(LONGTERM, "c"))


async def test_demoting_to_read_only_stops_writes_and_keeps_reads(stack):
    researcher = stack.agent(stack.registry.issue_identity("researcher", "dep-1"))
    await researcher.append("knowledge", entry=fact(KNOWLEDGE))

    stack.registry.revoke("researcher", ResourceKind.MEMORY, KNOWLEDGE, mode=Mode.RW, reason="ro")
    await settled(stack)

    assert (await refused(researcher.append("knowledge", entry=fact(KNOWLEDGE, "b")))).code == (
        ErrorCode.MEM_001
    )
    assert len(await researcher.read("knowledge")) == 1
    modes = {s["alias"]: s["mode"] for s in await researcher.spaces()}
    assert modes["knowledge"] == "ro", "광고하는 mode 가 실제 권한과 다르다"


async def test_searching_everything_skips_a_revoked_space_instead_of_failing(stack):
    researcher = stack.agent(stack.registry.issue_identity("researcher", "dep-1"))
    stack.registry.revoke("researcher", ResourceKind.MEMORY, KNOWLEDGE, reason="incident")
    await settled(stack)

    listed = {s["alias"] for s in await researcher.spaces()}

    assert "knowledge" not in listed and "longterm" in listed
    await researcher.search("태그")  # 전체 검색이 회수된 space 하나로 실패하지 않는다


# --- 레지스트리 장애 -------------------------------------------------------------


async def test_while_the_registry_is_down_known_spaces_keep_working_and_new_ones_are_refused(
    stack,
):
    researcher = stack.agent(stack.registry.issue_identity("researcher", "dep-1"))
    # 레지스트리는 선언을 처음 볼 때 버전을 올린다 — 그 알림이 방금 쌓은 캐시를 비우면 안 된다
    await settled(stack)
    await researcher.append("longterm", entry=fact(LONGTERM))  # 쓰기 판정이 캐시된다

    stack.registry_down = True
    await until(lambda: not stack.client._feed_alive)  # noqa: SLF001
    await asyncio.sleep(0.1)  # 재확인 주기를 넘긴다

    await researcher.append("longterm", entry=fact(LONGTERM, "b"))
    assert (await refused(researcher.append("knowledge", entry=fact(KNOWLEDGE)))).code == (
        ErrorCode.MEM_001
    )


# --- 부여받은 선언 밖 space (#279) --------------------------------------------------------


async def test_a_granted_undeclared_space_is_reachable_by_its_space_id(stack):
    """권한 에이전트가 선언 밖 space 를 부여하면 에이전트에게는 별칭이 없다 — space id 로 부른다."""
    import time

    from aaestro.access.model import Effect, Rule

    elsewhere = "group:ops:runbook"
    researcher = stack.agent(stack.registry.issue_identity("researcher", "dep-1"))
    await settled(stack)
    assert (await refused(researcher.read(elsewhere))).code == ErrorCode.MEM_001, "부여 전에 열렸다"

    now = time.time()
    stack.registry._record(  # noqa: SLF001 — 권한 에이전트 부여와 같은 기록 경로
        Rule(
            rule_id="rule-granted",
            agent="researcher",
            kind=ResourceKind.MEMORY,
            target=elsewhere,
            mode=Mode.RW,
            effect=Effect.ALLOW,
            decided_by="permission-agent",
            requested_by="researcher",
            reason="needs the runbook",
            created_at=now,
            expires_at=now + 600,
        ),
        op="grant",
    )
    await settled(stack)

    await researcher.append(elsewhere, entry=fact(elsewhere, "runbook step"))
    assert [e.content for e in await researcher.read(elsewhere)] == ["runbook step"]
    assert (await refused(researcher.read("local:writer:longterm"))).code == ErrorCode.MEM_001, (
        "space id 로 부른다고 선언·부여 없는 space 가 열리면 안 된다"
    )


# --- 검색 경계 (#324) -----------------------------------------------------------


async def test_search_refuses_a_revoked_space_by_alias_and_by_id(stack):
    """회수된 space 는 별칭으로도 id 로도 검색되지 않는다 — 검색은 저장소를 점조회로 읽는다."""
    researcher = stack.agent(stack.registry.issue_identity("researcher", "dep-1"))
    await researcher.append("longterm", entry=fact(LONGTERM))
    stack.memory.indexer.drain()
    assert await researcher.search("sidecar", spaces=["longterm"]), "회수 전에는 검색돼야 한다"

    stack.registry.revoke("researcher", ResourceKind.MEMORY, LONGTERM, reason="incident")
    await settled(stack)

    for target in ("longterm", LONGTERM):
        assert (await refused(researcher.search("sidecar", spaces=[target]))).code == (
            ErrorCode.MEM_001
        )


async def test_search_refuses_another_agents_space_id_and_undeclared_alias(stack):
    researcher = stack.agent(stack.registry.issue_identity("researcher", "dep-1"))
    await settled(stack)

    for target in ("local:writer:longterm", "no-such-alias"):
        assert (await refused(researcher.search("sidecar", spaces=[target]))).code == (
            ErrorCode.MEM_001
        )
