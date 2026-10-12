"""Unit tests for the A2A connection allowlist.

핵심 계약 세 가지를 고정한다: 방향은 선언의 문제(peer symmetry),
그룹 소속은 권한을 주지 않음(group neutrality), 깊이 상한(순환 위임 방지).
"""

from __future__ import annotations

import pytest

from aaestro.core.agent import TraceContext
from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.orchestrator.topology import ConnectionSpec
from aaestro.protocols.a2a.allowlist import Allowlist, Edge, edges_from, issue_token

SECRET = b"runtime-signing-secret"


def make_allowlist(*pairs: tuple[str, str], max_depth: int = 3) -> Allowlist:
    return Allowlist(
        edges=frozenset(Edge(caller=c, callee=e) for c, e in pairs),
        secret=SECRET,
        max_depth=max_depth,
    )


def trace(depth: int = 0) -> TraceContext:
    return TraceContext(trace_id="trace-1", depth=depth)


# --- 방향성 -------------------------------------------------------------------


def test_declared_direction_is_allowed():
    allowlist = make_allowlist(("researcher", "planner"))

    assert allowlist.allows("researcher", "planner") is True


def test_reverse_direction_needs_its_own_declaration():
    """방향은 선언의 문제다 — 역방향이 자동으로 열리지 않는다."""
    allowlist = make_allowlist(("researcher", "planner"))

    assert allowlist.allows("planner", "researcher") is False


def test_mutual_declaration_enables_both_directions():
    """상호 선언하면 양방향 협업이 가능하다 — 우열 관계가 아니다."""
    allowlist = make_allowlist(("researcher", "planner"), ("planner", "researcher"))

    assert allowlist.allows("researcher", "planner")
    assert allowlist.allows("planner", "researcher")


def test_undeclared_call_is_a2a_004():
    allowlist = make_allowlist(("researcher", "planner"))

    with pytest.raises(AaestroError) as exc_info:
        allowlist.check_call("writer", "planner", trace())

    assert exc_info.value.code == "A2A_004"
    assert exc_info.value.category is ErrorCategory.A2A
    assert exc_info.value.retryable is False


def test_group_membership_grants_nothing():
    """같은 그룹이어도 선언 없는 호출은 거부된다 (group neutrality).

    그룹은 리소스 스코프, 연결은 배선 — 두 축은 직교한다.
    """
    allowlist = make_allowlist(("researcher", "planner"))

    with pytest.raises(AaestroError) as exc_info:
        allowlist.check_call("researcher", "summarizer", trace())

    assert exc_info.value.code == "A2A_004"


def test_no_transitive_permission():
    """A→B, B→C 가 있어도 A→C 는 열리지 않는다 — caller 권한은 전파되지 않는다."""
    allowlist = make_allowlist(("a", "b"), ("b", "c"))

    assert allowlist.allows("a", "c") is False


# --- discovery ---------------------------------------------------------------


def test_peers_lists_only_declared_callees():
    """에이전트는 주소를 직접 알지 못하고 이 목록으로만 discovery 한다."""
    allowlist = make_allowlist(
        ("researcher", "planner"), ("researcher", "writer"), ("writer", "planner")
    )

    assert allowlist.peers_of("researcher") == ("planner", "writer")


def test_agent_without_declarations_sees_no_peers():
    allowlist = make_allowlist(("researcher", "planner"))

    assert allowlist.peers_of("writer") == ()


# --- depth limit --------------------------------------------------------------


def test_call_within_depth_limit_is_allowed():
    allowlist = make_allowlist(("a", "b"), max_depth=3)

    allowlist.check_call("a", "b", trace(depth=1))


def test_depth_limit_blocks_deep_delegation():
    """순환 위임 폭주를 막는 유일한 장치."""
    allowlist = make_allowlist(("a", "b"), max_depth=3)

    with pytest.raises(AaestroError) as exc_info:
        allowlist.check_call("a", "b", trace(depth=3))

    assert exc_info.value.code == "A2A_005"
    assert exc_info.value.details["limit"] == 3
    assert exc_info.value.details["depth"] == 4


def test_allowlist_is_checked_before_depth():
    """선언 위반이 먼저다 — 깊이만 줄여도 통과하는 것처럼 보이면 안 된다."""
    allowlist = make_allowlist(("a", "b"), max_depth=1)

    with pytest.raises(AaestroError) as exc_info:
        allowlist.check_call("x", "y", trace(depth=9))

    assert exc_info.value.code == "A2A_004"


# --- per-edge token -----------------------------------------------------------


def test_token_is_edge_specific():
    """token 은 방향마다 다르다 — 하나로 다른 edge 를 통과할 수 없다."""
    allowlist = make_allowlist(("a", "b"), ("a", "c"))

    assert allowlist.token_for("a", "b") != allowlist.token_for("a", "c")


def test_valid_token_authorizes_the_callee_side():
    allowlist = make_allowlist(("researcher", "planner"))
    token = allowlist.token_for("researcher", "planner")

    allowlist.verify("researcher", "planner", token)


def test_wrong_token_is_rejected():
    """caller 가 이름을 주장하는 것만으로는 부족하다 — 이중 방어의 두 번째 축."""
    allowlist = make_allowlist(("researcher", "planner"))

    with pytest.raises(AaestroError) as exc_info:
        allowlist.verify("researcher", "planner", "forged")

    assert exc_info.value.code == "A2A_004"


def test_token_from_another_edge_does_not_transfer():
    allowlist = make_allowlist(("a", "b"), ("c", "b"))
    other = allowlist.token_for("c", "b")

    with pytest.raises(AaestroError) as exc_info:
        allowlist.verify("a", "b", other)

    assert exc_info.value.code == "A2A_004"


def test_undeclared_edge_gets_no_token():
    allowlist = make_allowlist(("a", "b"))

    with pytest.raises(AaestroError) as exc_info:
        allowlist.token_for("x", "y")

    assert exc_info.value.code == "A2A_004"


def test_token_depends_on_the_secret():
    """runtime 이 secret 을 갱신하면 이전 token 이 무효가 되어야 한다."""
    edge = Edge(caller="a", callee="b")

    assert issue_token(b"one", edge) != issue_token(b"two", edge)


# --- 그래프 선언에서 생성 ------------------------------------------------------


def test_edges_are_built_from_graph_connections():
    """allowlist 의 출처는 그래프 배선 선언이다."""
    connections = (
        ConnectionSpec(caller="researcher", callee="planner"),
        ConnectionSpec(caller="writer", callee="planner"),
    )

    edges = edges_from(connections)

    assert edges == frozenset(
        {Edge(caller="researcher", callee="planner"), Edge(caller="writer", callee="planner")}
    )


# --- secret 필수 ---------------------------------------------------------------


def test_empty_secret_is_rejected():
    """빈 secret 이면 token 을 누구나 계산할 수 있어 callee 측 방어가 무력화된다."""
    with pytest.raises(AaestroError) as exc_info:
        Allowlist(edges=frozenset({Edge(caller="a", callee="b")}), secret=b"")

    assert exc_info.value.code == "CFG_002"


def test_forged_token_from_an_empty_key_is_rejected():
    """공개 키(빈 값)로 계산한 token 이 통과하면 인가가 성립하지 않는다."""
    import hashlib
    import hmac

    allowlist = make_allowlist(("researcher", "planner"))
    forged = hmac.new(b"", b"researcher->planner", hashlib.sha256).hexdigest()

    with pytest.raises(AaestroError) as exc_info:
        allowlist.verify("researcher", "planner", forged)

    assert exc_info.value.code == "A2A_004"


def test_verify_attributes_the_error_to_the_callee():
    """수신 측 판정이므로 callee 에 귀속한다 — caller 이름으로 우리 로그가 오염되면 안 된다."""
    allowlist = make_allowlist(("researcher", "planner"))

    with pytest.raises(AaestroError) as exc_info:
        allowlist.verify("researcher", "planner", "forged")

    assert exc_info.value.agent == "planner"


def test_caller_side_error_stays_with_the_caller():
    allowlist = make_allowlist(("researcher", "planner"))

    with pytest.raises(AaestroError) as exc_info:
        allowlist.check_call("writer", "planner", trace())

    assert exc_info.value.agent == "writer"


def test_details_cannot_overwrite_the_standard_fields():
    """details 가 호출 방향을 덮어쓰면 로그·메트릭이 조용히 틀어진다."""
    from aaestro.protocols.a2a.errors import a2a_error

    error = a2a_error(
        "A2A_004",
        "test",
        caller="researcher",
        callee="planner",
        a2a_caller="spoofed",
        a2a_callee="spoofed",
    )

    assert error.details["a2a_caller"] == "researcher"
    assert error.details["a2a_callee"] == "planner"
