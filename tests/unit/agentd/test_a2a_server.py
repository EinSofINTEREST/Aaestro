"""agentd A2A server startup tests.

03 은 기동 순서 5단계로 "A2A 서버 기동 (enabled 시)" 를 규정하는데 그 단계가
비어 있었다 — manifest 가 선언하고 runtime 이 포트까지 열어 주는데 컨테이너
안에서 그 포트를 듣는 것이 없었다 (#166).

그래서 #118 이 만든 수신 검증(allowlist / token / depth)이 **한 번도 실행되지
않았다.**
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aaestro.agentd.a2a_server import (
    EDGES_ENV,
    MAX_DEPTH_ENV,
    PORT_ENV,
    SECRET_ENV,
    a2a_port,
    build_a2a_app,
    parse_edges,
)
from aaestro.core.manifest import AgentManifest
from aaestro.protocols.a2a.allowlist import Edge


async def invoke(task):  # pragma: no cover - 서버 구성만 검증한다
    raise AssertionError("inbound task should not run in these tests")


def manifest(name: str) -> AgentManifest:
    declared = Path(f"agents/{name}/manifest.yaml").read_text(encoding="utf-8")
    return AgentManifest.model_validate(yaml.safe_load(declared))


@pytest.fixture
def injected(monkeypatch):
    """runtime 이 주입하는 값들 — 에이전트는 자기 배선을 알지 못한다."""
    monkeypatch.setenv(SECRET_ENV, "runtime-signing-secret")
    monkeypatch.setenv(PORT_ENV, "9100")
    monkeypatch.setenv(EDGES_ENV, "researcher>planner,planner>writer")


# --- edge 파싱 -----------------------------------------------------------------


def test_declared_connections_become_edges():
    assert parse_edges("a>b,c>d") == frozenset(
        {Edge(caller="a", callee="b"), Edge(caller="c", callee="d")}
    )


@pytest.mark.parametrize("declared", ["", "  ", "a>", ">b", "garbage"])
def test_malformed_entries_are_dropped_not_fatal(declared):
    """배선 실수 하나로 에이전트가 통째로 못 뜨면 안 된다.

    선언되지 않은 방향은 어차피 `A2A_004` 로 거부된다.
    """
    assert parse_edges(declared) == frozenset()


# --- 서버 구성 -----------------------------------------------------------------


def test_a_declaring_agent_gets_a_server(injected):
    """#166 의 핵심 — 선언했으면 실제로 서빙해야 한다."""
    assert build_a2a_app(manifest("researcher"), invoke) is not None


def test_an_agent_without_a2a_gets_no_server(injected):
    """선언하지 않았는데 포트를 열면 격리 표면이 넓어진다 (02 Network 5)."""
    assert build_a2a_app(manifest("echo"), invoke) is None


def test_without_a_signing_secret_the_server_stays_down(monkeypatch):
    """키가 없으면 token 이 공개 키로 HMAC 되어 callee 측 방어가 무력화된다."""
    monkeypatch.delenv(SECRET_ENV, raising=False)
    monkeypatch.setenv(PORT_ENV, "9100")

    assert build_a2a_app(manifest("researcher"), invoke) is None


def test_the_port_comes_from_the_runtime(monkeypatch):
    """포트를 코드에 박으면 에이전트가 자기 배선을 정하게 된다 (03 Rule 2)."""
    monkeypatch.setenv(PORT_ENV, "9137")

    assert a2a_port() == 9137


@pytest.mark.parametrize("declared", ["", "not-a-number", "-1"])
def test_an_uninjected_port_is_none(monkeypatch, declared):
    monkeypatch.setenv(PORT_ENV, declared)

    assert a2a_port() is None


def test_the_depth_limit_is_injectable(monkeypatch, injected):
    """03 Rule 5 — 순환 위임 방지 상한도 배선의 문제다."""
    monkeypatch.setenv(MAX_DEPTH_ENV, "1")

    # 구성이 성공하면 주입값이 반영된 것 — 실제 거부는 #118 이 검증한다
    assert build_a2a_app(manifest("researcher"), invoke) is not None


# --- 레지스트리 모드 (#281) -----------------------------------------------------------


@pytest.fixture
def registry_mode(monkeypatch):
    """공유 서명 키 없이 레지스트리 주소와 에이전트 신원만 주입된 컨테이너."""
    from aaestro.access.client import ACCESS_URL_ENV
    from aaestro.access.model import ACCESS_CREDENTIAL_ENV

    monkeypatch.delenv(SECRET_ENV, raising=False)
    monkeypatch.setenv(PORT_ENV, "9100")
    monkeypatch.setenv(EDGES_ENV, "researcher>planner")
    monkeypatch.setenv(ACCESS_URL_ENV, "http://control-plane:8700")
    monkeypatch.setenv(ACCESS_CREDENTIAL_ENV, "researcher-identity")


def test_registry_mode_serves_without_a_shared_secret_and_verifies_with_the_registry(
    registry_mode,
):
    app = build_a2a_app(manifest("researcher"), invoke)

    assert app is not None
    guard = app.state.guard
    assert guard.verifier is not None, "레지스트리 모드인데 피호출자가 표를 확인하지 않는다"
    assert guard.verifier.enforcer_token == "researcher-identity", "자기 신원으로 확인해야 한다"


def test_token_mode_has_no_registry_verifier(injected):
    assert build_a2a_app(manifest("researcher"), invoke).state.guard.verifier is None


def test_registry_mode_calls_peers_with_tickets(registry_mode, monkeypatch):
    from aaestro.agentd.a2a_server import PEERS_ENV, build_peer_client
    from aaestro.protocols.a2a.tickets import TicketSource

    monkeypatch.setenv(PEERS_ENV, "planner=aaestro-planner-0:9101")

    client = build_peer_client(manifest("researcher"))

    assert client is not None and isinstance(client.tickets, TicketSource)
    assert (client.tickets.base_url, client.tickets.credential) == (
        "http://control-plane:8700",
        "researcher-identity",
    )


# --- 태스크 조회·취소 창구 (#414) -----------------------------------------------------

UNSUPPORTED = -32004
"""SDK 의 ``UnsupportedOperationError`` 가 JSON-RPC 로 나가는 코드."""


async def _post(app, method: str, params: dict, headers: dict | None = None):
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
    ) as http:
        return await http.post(
            "/",
            json={"jsonrpc": "2.0", "id": "probe", "method": method, "params": params},
            headers={"A2A-Version": "1.0", **(headers or {})},
        )


async def _ask(app, method: str, params: dict, headers: dict | None = None) -> dict:
    return (await _post(app, method, params, headers)).json()


@pytest.mark.parametrize("mode", ["injected", "registry_mode"])
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("GetTask", {"id": "any-task"}),
        ("ListTasks", {"includeArtifacts": True}),
        ("CancelTask", {"id": "any-task"}),
        ("SubscribeToTask", {"id": "any-task"}),
    ],
)
async def test_the_assembled_server_refuses_task_requests_in_both_modes(
    mode, method, params, request
):
    """조립이 SDK 기본 핸들러로 돌아가면 이 창구가 자격 없이 다시 열린다.

    거절 동작 자체는 `tests/unit/protocols/a2a/test_task_methods_guard.py` 가 본다 — 여기서는
    **프로덕션 조립**이 그 핸들러를 쓰는지만 본다. SDK 기본 구현은 없는 태스크에 "not found" 로,
    목록에는 결과로 답한다.
    """
    request.getfixturevalue(mode)
    app = build_a2a_app(manifest("researcher"), invoke)

    answer = await _ask(app, method, params)

    assert "result" not in answer
    assert answer["error"]["code"] == UNSUPPORTED


# --- 이 포트에 열린 것 전부 (#414) ----------------------------------------------------

OPEN_ROUTES = {("/.well-known/agent-card.json", "GET"), ("/", "POST")}
"""A2A 포트가 답하는 경로 — 카드와 JSON-RPC. 그 밖의 것은 자격 없이 닿는 새 창구다."""

METHOD_NOT_FOUND = -32601


@pytest.mark.parametrize("mode", ["injected", "registry_mode"])
def test_the_assembled_server_opens_only_the_card_and_the_rpc_endpoint(mode, request):
    """문서 라우트 · REST 바인딩처럼 조립이 덧붙인 경로는 핸들러의 거절을 거치지 않거나 넓힌다."""
    request.getfixturevalue(mode)
    app = build_a2a_app(manifest("researcher"), invoke)

    opened = {
        (route.path, method)
        for route in app.routes
        for method in (getattr(route, "methods", None) or {"*"})
    }

    assert opened == OPEN_ROUTES


@pytest.mark.parametrize("method", ["tasks/get", "tasks/cancel", "message/send"])
async def test_the_assembled_server_does_not_route_the_older_protocol_names(method, injected):
    """v0.3 호환을 켜면 같은 경로에 다른 이름의 요청이 열린다 — 라우트 표로는 보이지 않는다."""
    app = build_a2a_app(manifest("researcher"), invoke)

    answer = await _ask(app, method, {"id": "any-task"})

    assert answer["error"]["code"] == METHOD_NOT_FOUND


async def test_the_served_card_advertises_only_what_the_server_answers(injected):
    """push 설정과 확장 카드는 전부 거절한다 — 카드가 광고하면 광고와 동작이 어긋난다."""
    import httpx

    app = build_a2a_app(manifest("researcher"), invoke)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
    ) as http:
        card = (await http.get("/.well-known/agent-card.json")).json()

    assert not card["capabilities"].get("extendedAgentCard")
    assert not card["capabilities"].get("pushNotifications")


# --- 태스크를 쥐지 않는다 (#414) ------------------------------------------------------


def _new_task() -> dict:
    from a2a.types import a2a_pb2 as pb
    from google.protobuf.json_format import MessageToDict

    from aaestro.protocols.a2a.sdk import build_message
    from tests.fixtures.builders import make_task

    return MessageToDict(pb.SendMessageRequest(message=build_message(make_task())))


def _saves(app) -> list[str]:
    """store 에 닿은 저장 — 끝난 태스크는 남지 않으니 크기로는 '만들었다 지움' 이 안 보인다."""
    saved: list[str] = []
    store = app.state.task_store
    save = store.save

    async def recording(task, context):
        saved.append(task.id)
        await save(task, context)

    store.save = recording
    return saved


@pytest.mark.parametrize("mode", ["injected", "registry_mode"])
@pytest.mark.parametrize("method", ["SendMessage", "SendStreamingMessage"])
async def test_an_uncredentialed_send_creates_no_task_in_either_mode(mode, method, request):
    """자격 없는 요청마다 태스크를 만들면 그 수와 본문 크기만큼 컨테이너 메모리를 쓴다."""
    request.getfixturevalue(mode)
    app = build_a2a_app(manifest("researcher"), invoke)
    saved = _saves(app)

    for _ in range(3):
        answer = await _ask(app, method, _new_task())
        assert "a2a:A2A_004" in answer["error"]["message"]

    assert saved == []
    assert len(app.state.task_store) == 0


@pytest.mark.parametrize("method", ["SendMessage", "SendStreamingMessage"])
async def test_valid_sends_leave_nothing_in_the_assembled_store(method, injected):
    """읽는 창구가 닫혀 있으니 끝난 태스크는 쌓이기만 한다 (02 Statelessness)."""
    from aaestro.core.agent import TaskResult
    from aaestro.protocols.a2a.allowlist import issue_token
    from aaestro.protocols.a2a.sdk import CALLER_HEADER, TOKEN_HEADER

    async def answer(task):
        return TaskResult.completed(task, output={"plan": "ok"})

    app = build_a2a_app(manifest("planner"), answer)
    saved = _saves(app)
    token = issue_token(b"runtime-signing-secret", Edge(caller="researcher", callee="planner"))
    headers = {CALLER_HEADER: "researcher", TOKEN_HEADER: token}

    for _ in range(3):
        sent = await _post(app, method, _new_task(), headers)
        assert "TASK_STATE_COMPLETED" in sent.text

    assert len(saved) == 3, "태스크가 store 를 거치지 않았다 — 이 시험이 보는 경로가 아니다"
    assert len(app.state.task_store) == 0


async def test_lifespan_shutdown_yields_to_an_outer_cancellation():
    """종료 정리가 바깥 취소를 삼키면 서버 종료 timeout 이 사라진다 (#490)."""
    import asyncio

    from aaestro.agentd.a2a_server import _following

    watchers: list[asyncio.Task[None]] = []

    class Verifier:
        async def watch(self) -> None:
            watchers.append(asyncio.current_task())  # type: ignore[arg-type]
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.5)
                raise

        async def aclose(self) -> None:
            return None

    async def run() -> None:
        async with _following(Verifier())(None):
            await asyncio.sleep(0)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(run(), 0.2)

    assert all(task.done() and task.cancelled() for task in watchers)


async def test_lifespan_closes_the_verifier_even_when_cancelled_from_outside():
    import asyncio

    from aaestro.agentd.a2a_server import _following

    closed: list[bool] = []

    watchers: list[asyncio.Task[None]] = []

    class Verifier:
        async def watch(self) -> None:
            watchers.append(asyncio.current_task())  # type: ignore[arg-type]
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.5)
                raise

        async def aclose(self) -> None:
            closed.append(True)

    async def run() -> None:
        async with _following(Verifier())(None):
            await asyncio.sleep(0)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(run(), 0.2)

    assert closed == [True]
    assert all(task.done() and task.cancelled() for task in watchers)
