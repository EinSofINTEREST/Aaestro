"""A2A SDK binding tests.

**실제 SDK 경로로 왕복시킨다** — 내가 만든 transport 를 mock 으로 대체하면
바인딩이 깨져도 통과한다. 서버는 ASGI 로 인메모리 기동해 네트워크 없이 돈다.
"""

from __future__ import annotations

import httpx
import pytest
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.types import a2a_pb2 as pb
from a2a.utils.constants import TransportProtocol
from fastapi import FastAPI

from aaestro.core.agent import TaskResult, TaskStatus, TraceContext
from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.protocols.a2a.allowlist import Allowlist, Edge, issue_token
from aaestro.protocols.a2a.client import A2AServer
from aaestro.protocols.a2a.sdk import (
    CALLER_HEADER,
    TOKEN_HEADER,
    SdkPeerTransport,
    build_message,
    read_output,
    state_name,
)
from aaestro.protocols.a2a.server import (
    GuardedExecutor,
    GuardedRequestHandler,
    InboundGuard,
    read_task,
)
from tests.fixtures.builders import make_task

CALLER = "researcher"
CALLEE = "planner"
SECRET = b"runtime-signing-secret"
BASE_URL = "http://planner.test"


def allowlist(*edges: tuple[str, str]) -> Allowlist:
    return Allowlist(
        edges=frozenset(Edge(caller=caller, callee=callee) for caller, callee in edges),
        secret=SECRET,
    )


def peer_card() -> pb.AgentCard:
    """수신 에이전트의 카드 — SDK 가 라우팅에 쓴다."""
    return pb.AgentCard(
        name=CALLEE,
        description="테스트 peer",
        version="0.1.0",
        capabilities=pb.AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        supported_interfaces=[
            pb.AgentInterface(
                url=f"{BASE_URL}/",
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version="1.0",
            )
        ],
    )


def serve(guard: InboundGuard, handler) -> FastAPI:
    """검증을 얹은 A2A 수신 앱 — **SDK 의 실제 라우트**를 쓴다."""
    card = peer_card()
    request_handler = GuardedRequestHandler(
        guard=guard,
        agent_executor=GuardedExecutor(guard, handler),
        agent_card=card,
    )
    app = FastAPI()
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(request_handler, rpc_url="/"),
    )
    return app


def transport_to(app: FastAPI) -> SdkPeerTransport:
    """인메모리로 그 앱에 말하는 transport — 네트워크 없이 SDK 경로가 돈다."""
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)
    return _WiredTransport(agent=CALLER, addresses={CALLEE: BASE_URL}, http=http)


class _WiredTransport(SdkPeerTransport):
    """미리 만든 httpx 클라이언트를 쓰는 transport — ASGI 로 붙이기 위해서."""

    def __init__(self, *, agent, addresses, http):
        super().__init__(agent=agent, addresses=addresses)
        self._http = http

    def _new_http(self):
        # 헤더·요청 훅은 **프로덕션 코드가** 정한다 — ASGI 전송만 갈아 끼운다.
        # 테스트가 헤더를 직접 실으면 그 배선을 지워도 통과한다
        production = super()._new_http()
        production._transport = self._http._transport  # noqa: SLF001
        production.base_url = self._http.base_url
        return production


# --- 순수 변환 ------------------------------------------------------------------


def test_a_task_survives_the_round_trip_through_the_wire_format():
    """변환이 한쪽만 맞으면 peer 가 받은 태스크가 원본과 달라진다."""
    original = make_task(input={"query": "sidecar 태그"})

    message = build_message(original)
    restored = read_task(message.parts[0].text)

    assert restored.task_id == original.task_id
    assert restored.run_id == original.run_id
    assert restored.input == original.input
    assert restored.trace.depth == original.trace.depth


def test_unknown_states_are_not_folded_into_success():
    """미지의 상태를 completed 로 접으면 실패한 위임이 성공으로 보인다."""
    assert state_name(99).startswith("unknown-")


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (1, "submitted"),
        (2, "working"),
        (3, "completed"),
        (4, "failed"),
        (5, "canceled"),
    ],
)
def test_sdk_states_map_onto_the_established_vocabulary(state, expected):
    """map_status 가 아는 이름이어야 기존 계약이 유지된다."""
    from aaestro.protocols.a2a.client import map_status

    name = state_name(state)

    assert name == expected
    map_status(name)  # 알 수 없는 이름이면 ValueError


def test_non_json_output_is_preserved_rather_than_dropped():
    """peer 가 평문을 돌려줬다고 결과를 버리면 위임이 조용히 빈다."""
    task = pb.Task(artifacts=[pb.Artifact(parts=[pb.Part(text="그냥 문장")])])

    assert read_output(task) == {"text": "그냥 문장"}


# --- 수신 검증 ------------------------------------------------------------------


def guard_for(*edges: tuple[str, str], max_depth: int = 3) -> InboundGuard:
    return InboundGuard(
        server=A2AServer(agent=CALLEE, allowlist=allowlist(*edges)), max_depth=max_depth
    )


def valid_headers() -> dict[str, str]:
    token = issue_token(SECRET, Edge(caller=CALLER, callee=CALLEE))
    return {CALLER_HEADER: CALLER, TOKEN_HEADER: token}


def test_a_declared_caller_is_authorized():
    guard = guard_for((CALLER, CALLEE))

    assert guard.check(valid_headers(), make_task()) == CALLER


def test_an_undeclared_caller_is_rejected_with_a2a_004():
    """같은 그룹이어도 선언이 없으면 거부된다 (group neutrality)."""
    guard = guard_for((CALLEE, CALLER))  # 반대 방향만 선언

    with pytest.raises(AaestroError) as exc_info:
        guard.check(valid_headers(), make_task())

    assert exc_info.value.code == ErrorCode.A2A_004


def test_a_forged_token_is_rejected():
    """이름만 주장하는 호출을 막는 것이 token 의 존재 이유다."""
    guard = guard_for((CALLER, CALLEE))

    with pytest.raises(AaestroError) as exc_info:
        guard.check({CALLER_HEADER: CALLER, TOKEN_HEADER: "forged"}, make_task())

    assert exc_info.value.code == ErrorCode.A2A_004


@pytest.mark.parametrize("missing", [CALLER_HEADER, TOKEN_HEADER])
def test_a_missing_header_is_rejected(missing):
    guard = guard_for((CALLER, CALLEE))
    headers = valid_headers()
    del headers[missing]

    with pytest.raises(AaestroError) as exc_info:
        guard.check(headers, make_task())

    assert exc_info.value.code == ErrorCode.A2A_004


def test_a_deep_chain_is_rejected_with_a2a_005():
    """caller 가 자기 depth 를 정직하게 실었다고 믿으면 순환 위임이 뚫린다."""
    guard = guard_for((CALLER, CALLEE), max_depth=2)
    deep = make_task(trace=TraceContext(trace_id="t", depth=5))

    with pytest.raises(AaestroError) as exc_info:
        guard.check(valid_headers(), deep)

    assert exc_info.value.code == ErrorCode.A2A_005


# --- 실제 SDK 경로 왕복 -----------------------------------------------------------


async def test_a_call_round_trips_between_two_agents():
    """#118 완료 조건 — 내 transport 를 mock 으로 대체하면 증명되지 않는다."""
    received: list[str] = []
    callers: list[str | None] = []

    async def handler(task):
        received.append(task.input["query"])
        callers.append(task.caller)
        return TaskResult.completed(task, output={"plan": "먼저 문서를 읽는다"})

    app = serve(guard_for((CALLER, CALLEE)), handler)
    transport = transport_to(app)

    result = await transport.send(
        callee=CALLEE,
        task=make_task(input={"query": "무엇부터?"}),
        token=issue_token(SECRET, Edge(caller=CALLER, callee=CALLEE)),
        headers={},
    )

    assert received == ["무엇부터?"]
    assert callers == [CALLER], "토큰이 확인한 호출자가 태스크에 실리지 않았다"
    assert result.output == {"plan": "먼저 문서를 읽는다"}
    assert result.status is TaskStatus.COMPLETED


async def test_an_undeclared_caller_is_refused_on_the_real_sdk_path():
    """단위 검증만으로는 SDK 경로에 검증이 실제로 얹혔는지 알 수 없다."""

    async def handler(task):  # pragma: no cover - 도달하면 안 된다
        raise AssertionError("undeclared caller reached the agent")

    app = serve(guard_for((CALLEE, CALLER)), handler)  # 반대 방향만 선언
    transport = transport_to(app)

    with pytest.raises(AaestroError) as exc_info:
        await transport.send(
            callee=CALLEE,
            task=make_task(),
            token=issue_token(SECRET, Edge(caller=CALLER, callee=CALLEE)),
            headers={},
        )

    # 거부 사유가 살아 있어야 한다 — 전부 A2A_003 으로 뭉개면 설정 문제(allowlist)와
    # 운영 문제(peer 실패)가 구분되지 않는다
    assert exc_info.value.code == ErrorCode.A2A_004


async def test_the_token_actually_travels_in_the_headers():
    """헤더에 실리지 않으면 callee 는 늘 거부하거나 늘 통과시킨다."""
    seen: dict[str, str] = {}

    async def handler(task):
        return TaskResult.completed(task, output={})

    guard = guard_for((CALLER, CALLEE))
    original = guard.check

    def recording(headers, task):
        seen.update(headers)
        return original(headers, task)

    guard.check = recording  # type: ignore[method-assign]
    transport = transport_to(serve(guard, handler))

    await transport.send(
        callee=CALLEE,
        task=make_task(),
        token=issue_token(SECRET, Edge(caller=CALLER, callee=CALLEE)),
        headers={},
    )

    assert seen[CALLER_HEADER] == CALLER
    assert seen[TOKEN_HEADER] == issue_token(SECRET, Edge(caller=CALLER, callee=CALLEE))


async def test_a_deep_chain_is_refused_on_the_real_sdk_path():
    """A2A_005 가 SDK 경로에서도 지켜지는지 — 순환 위임 방지의 실효성."""

    async def handler(task):  # pragma: no cover - 도달하면 안 된다
        raise AssertionError("deep chain reached the agent")

    app = serve(guard_for((CALLER, CALLEE), max_depth=1), handler)
    transport = transport_to(app)

    with pytest.raises(AaestroError) as exc_info:
        await transport.send(
            callee=CALLEE,
            task=make_task(trace=TraceContext(trace_id="t", depth=9)),
            token=issue_token(SECRET, Edge(caller=CALLER, callee=CALLEE)),
            headers={},
        )

    assert exc_info.value.code == ErrorCode.A2A_005


async def test_a_peer_failure_carries_its_structured_reason_to_the_caller():
    """상태만 보내면 호출자는 실패만 안다 — 권한 에이전트의 거절 코드 같은 **왜**가 필요하다."""
    from aaestro.core.errors import ErrorCategory

    async def handler(task):
        return TaskResult.failed(
            task,
            AaestroError(
                category=ErrorCategory.FORBIDDEN, code="ACC_003", message="beyond the ceiling"
            ),
        )

    app = serve(guard_for((CALLER, CALLEE)), handler)
    transport = transport_to(app)

    with pytest.raises(AaestroError) as exc_info:
        await transport.send(
            callee=CALLEE,
            task=make_task(),
            token=issue_token(SECRET, Edge(caller=CALLER, callee=CALLEE)),
            headers={},
        )

    assert exc_info.value.code == ErrorCode.A2A_003
    assert exc_info.value.details["peer_error"]["code"] == "ACC_003"
    assert exc_info.value.details["peer_error"]["message"] == "beyond the ceiling"
