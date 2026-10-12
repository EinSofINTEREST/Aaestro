"""A2A calls authorized by the registry on every request (#281).

실제 SDK 경로로 왕복시키고, 표 발급·확인은 **실제 control plane 권한 라우트**를 ASGI 로 부른다.
저장소의 `research-pipeline` 은 researcher → planner 만 선언한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from pathlib import Path

import httpx
import pytest

from aaestro.access.baselines import A2ABaseline
from aaestro.access.client import AccessClient
from aaestro.access.model import ResourceKind
from aaestro.access.registry import AccessRegistry
from aaestro.access.store import InMemoryAccessStore
from aaestro.catalog import Catalog
from aaestro.core.agent import TaskResult
from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.orchestrator.control import create_app as control_app
from aaestro.orchestrator.runstore import InMemoryRunStore
from aaestro.protocols.a2a.allowlist import Allowlist, Edge, issue_token
from aaestro.protocols.a2a.client import A2AClient, A2AServer
from aaestro.protocols.a2a.server import InboundGuard
from aaestro.protocols.a2a.tickets import TicketSource
from tests.fixtures.builders import make_task
from tests.fixtures.waiting import until
from tests.unit.protocols.a2a.test_sdk import serve, transport_to

REPO_ROOT = Path(__file__).resolve().parents[4]
GRAPH = "research-pipeline"
ENFORCER = "enforcer-token"


class Stack:
    def __init__(self) -> None:
        catalog = Catalog.under(REPO_ROOT)
        store = InMemoryAccessStore()
        self.registry = AccessRegistry(
            store=store, catalog=catalog, baselines={ResourceKind.A2A: A2ABaseline(catalog, store)}
        )
        self.credentials = {
            name: self.registry.issue_identity(name, "dep-1", graph=GRAPH)
            for name in ("planner", "researcher", "writer")
        }
        control = control_app(
            InMemoryRunStore(),
            catalog=catalog,
            token="control",
            access=self.registry,
            enforcer_token=ENFORCER,
        )
        self.control = httpx.ASGITransport(app=control)
        self.seen: list[dict[str, str]] = []
        self.verifier = AccessClient(
            base_url="http://cp",
            enforcer_token=self.credentials["planner"],
            component="a2a",
            http=self._http(),
            feed_wait_s=0.2,
        )
        guard = InboundGuard(
            server=A2AServer(agent="planner", allowlist=_allowlist()), verifier=self.verifier
        )

        self.callers: list[str | None] = []

        async def handle(task):
            self.callers.append(task.caller)
            return TaskResult.completed(task, output={"plan": "ok"})

        self.planner = serve(guard, handle)

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.control, base_url="http://cp")

    def caller(self, agent: str = "researcher", *, tickets: object | None = None) -> A2AClient:
        transport = transport_to(self.planner)
        transport.agent = agent
        return A2AClient(
            agent=agent,
            allowlist=_allowlist(),
            transport=transport,
            tickets=tickets
            or TicketSource(
                agent=agent,
                base_url="http://cp",
                credential=self.credentials[agent],
                http=self._http(),
            ),
        )


def _allowlist() -> Allowlist:
    # 레지스트리 모드의 allowlist 는 호출자 쪽 편의 검사 — 서명 키는 쓰이지 않는다
    return Allowlist(
        edges=frozenset({Edge("researcher", "planner"), Edge("writer", "planner")}),
        secret=secrets.token_bytes(16),
    )


@pytest.fixture
async def stack():
    found = Stack()
    watching = asyncio.create_task(found.verifier.watch())
    try:
        yield found
    finally:
        watching.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watching


async def refused(call) -> AaestroError:
    with pytest.raises(AaestroError) as exc_info:
        await call
    return exc_info.value


async def settled(stack: Stack) -> None:
    await until(lambda: stack.verifier._version == stack.registry.version())  # noqa: SLF001


# --- 실시간 회수 ------------------------------------------------------------------


async def test_a_declared_call_goes_through_on_a_ticket(stack):
    result = await stack.caller().call("planner", make_task())

    assert result.output == {"plan": "ok"}
    assert stack.callers == ["researcher"], "표가 확인한 호출자가 태스크에 실리지 않았다"


async def test_revoking_the_connection_denies_the_next_call_and_lifting_restores_it(stack):
    researcher = stack.caller()
    await researcher.call("planner", make_task())

    rule = stack.registry.revoke("researcher", ResourceKind.A2A, "planner", reason="incident")
    await settled(stack)
    assert (await refused(researcher.call("planner", make_task()))).code == ErrorCode.A2A_004

    stack.registry.lift(rule.rule_id)
    await settled(stack)
    await researcher.call("planner", make_task())


# --- 호출자 쪽 검사를 우회해도 --------------------------------------------------------


async def test_an_undeclared_caller_that_skips_its_own_check_is_refused_by_the_callee(stack):
    """writer → planner 는 그래프에 없다. 호출자 allowlist 를 넓혀 둬도 피호출자가 거부한다."""
    err = await refused(stack.caller("writer").call("planner", make_task()))

    assert err.code == ErrorCode.A2A_004


async def test_claiming_another_callers_name_with_your_own_ticket_is_refused(stack):
    """이름 주장과 표의 주인이 어긋나면 누구의 호출인지 믿을 수 없다.

    표의 주인(researcher)은 planner 를 부를 권한이 **있다** — 그래도 헤더에 writer 라고 적으면
    거부한다. 권한이 없는 호출자로 시험하면 이름 대조를 지워도 판정이 거부해 통과한다.
    """
    impostor = stack.caller("researcher")
    impostor.transport.agent = "writer"  # 헤더에는 writer 라고 적는다

    assert (await refused(impostor.call("planner", make_task()))).code == ErrorCode.A2A_004


async def test_a_valid_edge_token_is_not_accepted_in_registry_mode(stack):
    """공유 서명 키는 그래프의 모든 에이전트가 쥔다 — 그것으로 만든 토큰은 증명이 아니다."""

    class EdgeTokenOnly:
        async def ticket_for(self, callee: str) -> str:
            return ""

    secret = b"leaked-shared-secret"
    caller = stack.caller(tickets=EdgeTokenOnly())
    caller.allowlist = Allowlist(edges=caller.allowlist.edges, secret=secret)
    caller.tickets = None  # per-edge token 경로로 보낸다
    assert issue_token(secret, Edge("researcher", "planner"))

    assert (await refused(caller.call("planner", make_task()))).code == ErrorCode.A2A_004


def recording(stack: Stack) -> list[str]:
    """피호출자가 **실제로 받은** 표 — 호출자 쪽 클라이언트 상태가 아니라 도착한 것을 본다."""
    received: list[str] = []
    verify = stack.verifier.verify_ticket

    async def spy(ticket: str):
        received.append(ticket)
        return await verify(ticket)

    stack.verifier.verify_ticket = spy  # type: ignore[method-assign]
    return received


class Rotating:
    """부를 때마다 새 표 — 갱신이 겹치는 상황을 만든다."""

    def __init__(self, stack: Stack) -> None:
        self.stack = stack
        self.issued: list[str] = []

    async def ticket_for(self, callee: str) -> str:
        ticket, _ = self.stack.registry.issue_ticket(self.stack.credentials["researcher"], callee)
        self.issued.append(ticket)
        await asyncio.sleep(0)  # 다른 호출이 끼어들 틈을 준다
        return ticket


async def test_a_fresh_ticket_travels_on_every_call(stack):
    """표는 만료되어 새로 받는다 — 처음 실은 헤더를 계속 쓰면 옛 표가 나간다."""
    received = recording(stack)
    tickets = Rotating(stack)
    caller = stack.caller(tickets=tickets)

    await caller.call("planner", make_task(task_id="first"))
    await caller.call("planner", make_task(task_id="second"))

    assert received == tickets.issued and len(set(received)) == 2


async def test_concurrent_calls_each_carry_their_own_ticket(stack):
    """같은 peer 에 동시에 처음 부르면 공유 상태를 덮어써 서로의 표를 싣던 경합 (#291 리뷰)."""
    received = recording(stack)
    tickets = Rotating(stack)
    caller = stack.caller(tickets=tickets)

    await asyncio.gather(*(caller.call("planner", make_task(task_id=f"t{i}")) for i in range(4)))

    assert sorted(received) == sorted(tickets.issued), "어떤 호출은 남의 표를 실었다"
    assert len(caller.transport._clients) == 1, "peer 클라이언트를 여러 번 만들었다"  # noqa: SLF001


# --- 장애 ----------------------------------------------------------------------------


async def test_the_caller_cannot_get_a_ticket_while_the_registry_is_down(stack):
    down = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down"))),
        base_url="http://cp",
    )
    tickets = TicketSource(agent="researcher", base_url="http://cp", credential="x", http=down)

    err = await refused(stack.caller(tickets=tickets).call("planner", make_task()))

    assert (err.code, err.retryable) == (ErrorCode.A2A_002, True)


async def test_the_callee_still_checks_the_delegation_depth_in_registry_mode(stack):
    """깊이는 호출자가 정직하게 실었다고 믿지 않는다 — 레지스트리 모드에서도 수신 측이 본다."""
    from aaestro.core.agent import TraceContext

    deep = make_task(trace=TraceContext(trace_id="t", depth=9))
    caller = stack.caller()
    caller.allowlist = Allowlist(edges=caller.allowlist.edges, secret=b"x", max_depth=99)

    assert (await refused(caller.call("planner", deep))).code == ErrorCode.A2A_005
