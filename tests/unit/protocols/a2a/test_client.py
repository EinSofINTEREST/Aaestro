"""Unit tests for the A2A client, server guard, and AgentCard.

fake peer 로 호출 성공/거부/타임아웃을 검증한다 — 실제 peer 를 띄우지 않는다.
"""

from __future__ import annotations

import pytest

from aaestro.core.agent import TaskResult, TaskStatus
from aaestro.core.errors import AaestroError, AaestroErrorPayload
from aaestro.core.skill import SkillSpec
from aaestro.protocols.a2a.allowlist import Allowlist, Edge
from aaestro.protocols.a2a.card import build_card
from aaestro.protocols.a2a.client import (
    A2A_INFLIGHT_STATES,
    A2AClient,
    A2AServer,
    PeerTransport,
    map_status,
)
from tests.fixtures.builders import make_manifest, make_task

SECRET = b"runtime-secret"


class FakePeer:
    """peer 응답을 스크립트하는 전송 대역."""

    def __init__(self, result: TaskResult | None = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.sent: list[tuple[str, str, int]] = []

    async def send(self, *, callee, task, token, headers):
        self.sent.append((callee, token, task.trace.depth))
        if self._error is not None:
            raise self._error
        return self._result or TaskResult.completed(task, output={"answer": "42"})


def make_client(
    transport=None, *, edges=(("researcher", "planner"),), max_depth: int = 3
) -> A2AClient:
    allowlist = Allowlist(
        edges=frozenset(Edge(caller=c, callee=e) for c, e in edges),
        secret=SECRET,
        max_depth=max_depth,
    )
    return A2AClient(agent="researcher", allowlist=allowlist, transport=transport or FakePeer())


# --- 상태 매핑 ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("completed", TaskStatus.COMPLETED),
        ("failed", TaskStatus.FAILED),
        ("canceled", TaskStatus.CANCELED),
    ],
)
def test_terminal_states_map_to_task_status(state, expected):
    assert map_status(state) == expected


@pytest.mark.parametrize("state", sorted(A2A_INFLIGHT_STATES))
def test_inflight_states_map_to_none(state):
    """진행 중을 종료 상태로 뭉개면 호출자가 완료로 오해한다."""
    assert map_status(state) is None


def test_unknown_state_is_not_silently_interpreted():
    with pytest.raises(ValueError, match="unknown a2a task state"):
        map_status("mystery")


# --- 호출 ---------------------------------------------------------------------


async def test_declared_call_reaches_the_peer():
    peer = FakePeer()
    client = make_client(peer)

    result = await client.call("planner", make_task())

    assert result.status == TaskStatus.COMPLETED
    assert peer.sent[0][0] == "planner"


async def test_undeclared_call_is_rejected_before_transport():
    """caller 측 방어 — 전송까지 가지 않는다."""
    peer = FakePeer()
    client = make_client(peer)

    with pytest.raises(AaestroError) as exc_info:
        await client.call("writer", make_task())

    assert exc_info.value.code == "A2A_004"
    assert peer.sent == []


async def test_call_carries_the_edge_token():
    """peer 는 이 token 으로 caller 를 검증한다."""
    peer = FakePeer()
    client = make_client(peer)

    await client.call("planner", make_task())

    _callee, token, _depth = peer.sent[0]
    assert token == client.allowlist.token_for("researcher", "planner")


async def test_delegation_increments_trace_depth():
    """위임 체인이 깊이로 추적되어야 상한이 의미를 갖는다."""
    peer = FakePeer()
    client = make_client(peer)

    await client.call("planner", make_task())

    _callee, _token, depth = peer.sent[0]
    assert depth == 1


async def test_depth_limit_blocks_the_call():
    client = make_client(max_depth=2)
    deep = make_task()
    deep = deep.model_copy(update={"trace": deep.trace.model_copy(update={"depth": 2})})

    with pytest.raises(AaestroError) as exc_info:
        await client.call("planner", deep)

    assert exc_info.value.code == "A2A_005"


async def test_unreachable_peer_is_retryable_a2a_002():
    client = make_client(FakePeer(error=ConnectionError("refused")))

    with pytest.raises(AaestroError) as exc_info:
        await client.call("planner", make_task())

    assert exc_info.value.code == "A2A_002"
    assert exc_info.value.retryable is True


async def test_submission_failure_is_a2a_001():
    client = make_client(FakePeer(error=ValueError("bad payload")))

    with pytest.raises(AaestroError) as exc_info:
        await client.call("planner", make_task())

    assert exc_info.value.code == "A2A_001"
    assert exc_info.value.retryable is True


async def test_peer_failure_is_a2a_003():
    """callee 가 실패로 답하면 재시도해도 같은 결과다."""
    task = make_task()
    failed = TaskResult.failed(
        task,
        AaestroErrorPayload(
            category="internal", code="INTERNAL_001", message="peer exploded", retryable=False
        ),
    )
    client = make_client(FakePeer(result=failed))

    with pytest.raises(AaestroError) as exc_info:
        await client.call("planner", task)

    assert exc_info.value.code == "A2A_003"
    assert exc_info.value.retryable is False


async def test_call_timeout_reports_unreachable():
    import asyncio

    class Slow:
        async def send(self, *, callee, task, token, headers):
            await asyncio.sleep(10)

    client = make_client(Slow())
    client.timeout_s = 0.01

    with pytest.raises(AaestroError) as exc_info:
        await client.call("planner", make_task())

    assert exc_info.value.code == "A2A_002"


async def test_circuit_opens_after_repeated_peer_failures():
    """죽은 peer 를 계속 두드리지 않는다."""
    client = make_client(FakePeer(error=ConnectionError("gone")))

    for _ in range(5):
        with pytest.raises(AaestroError):
            await client.call("planner", make_task())

    with pytest.raises(AaestroError) as exc_info:
        await client.call("planner", make_task())

    assert exc_info.value.details.get("reason") == "circuit open"


def test_peers_reports_declared_callees():
    client = make_client(edges=(("researcher", "planner"), ("researcher", "writer")))

    assert client.peers() == ("planner", "writer")


def test_fake_peer_satisfies_the_transport_contract():
    assert isinstance(FakePeer(), PeerTransport)


# --- callee 측 방어 -----------------------------------------------------------


def test_server_accepts_a_valid_token():
    allowlist = Allowlist(
        edges=frozenset({Edge(caller="researcher", callee="planner")}), secret=SECRET
    )
    server = A2AServer(agent="planner", allowlist=allowlist)

    server.authorize("researcher", allowlist.token_for("researcher", "planner"))


def test_server_rejects_a_forged_token():
    allowlist = Allowlist(
        edges=frozenset({Edge(caller="researcher", callee="planner")}), secret=SECRET
    )
    server = A2AServer(agent="planner", allowlist=allowlist)

    with pytest.raises(AaestroError) as exc_info:
        server.authorize("researcher", "forged")

    assert exc_info.value.code == "A2A_004"


def test_server_rejects_an_undeclared_caller():
    allowlist = Allowlist(
        edges=frozenset({Edge(caller="researcher", callee="planner")}), secret=SECRET
    )
    server = A2AServer(agent="planner", allowlist=allowlist)

    with pytest.raises(AaestroError) as exc_info:
        server.authorize("intruder", "any")

    assert exc_info.value.code == "A2A_004"


# --- AgentCard ----------------------------------------------------------------


def test_card_is_derived_from_the_manifest():
    manifest = make_manifest()

    card = build_card(manifest)

    assert card.name == manifest.name
    assert card.version == manifest.metadata.version


def test_card_skills_match_the_loaded_tools():
    """수동 작성 card 는 실제 능력과 어긋난다 — 로드된 tool 에서 파생한다."""
    tools = [
        SkillSpec(name="search", description="웹 검색", parameters={"type": "object"}),
        SkillSpec(name="fetch_page", description="본문 추출", parameters={"type": "object"}),
    ]

    card = build_card(make_manifest(), tools)

    assert card.skill_names() == ("search", "fetch_page")
    assert card.skills[0].description == "웹 검색"


def test_card_without_tools_advertises_no_skills():
    assert build_card(make_manifest()).skills == ()


def test_card_reports_declared_capabilities():
    manifest = make_manifest(
        spec={
            "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
            "promptset": {"ref": "promptsets/test@0.1.0"},
            "a2a": {"enabled": True, "capabilities": {"streaming": True}},
        }
    )

    card = build_card(manifest)

    assert card.capabilities["streaming"] is True
    assert card.capabilities["push_notifications"] is False


async def test_peer_rejection_does_not_open_the_circuit():
    """A2A_003 은 peer 가 살아서 거부한 것 — 실패로 세면 멀쩡한 peer 가 차단된다."""
    task = make_task()
    failed = TaskResult.failed(
        task,
        AaestroErrorPayload(
            category="internal", code="INTERNAL_001", message="cannot do that", retryable=False
        ),
    )
    client = make_client(FakePeer(result=failed))

    for _ in range(6):
        with pytest.raises(AaestroError) as exc_info:
            await client.call("planner", make_task())
        assert exc_info.value.code == "A2A_003"

    # 6회 거부에도 회로는 닫혀 있어야 한다
    assert client._breaker("planner").can_attempt() is True


async def test_a_refusal_the_transport_already_classified_is_not_rewritten():
    """피호출자의 A2A_004 를 A2A_001 로 뭉개면 연결 회수와 제출 실패가 구분되지 않는다 (#281)."""
    from aaestro.protocols.a2a.errors import not_allowed

    client = make_client(FakePeer(error=not_allowed("researcher", "planner", owner="planner")))

    with pytest.raises(AaestroError) as exc_info:
        await client.call("planner", make_task())

    assert exc_info.value.code == "A2A_004"
