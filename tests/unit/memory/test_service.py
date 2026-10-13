"""Unit tests for memory space ACL and alias resolution.

접근 권한은 **선언 위치**가 정한다 — 그룹 소속이 곧 경계이고, 별칭은
가까운 스코프가 이긴다.
"""

from __future__ import annotations

import pytest

from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.core.manifest import MemoryMode
from aaestro.memory.entry import MemoryEntry, MemorySource
from aaestro.memory.service import AccessToken, MemoryService, MemorySpace, build_token
from aaestro.memory.store import SqliteMemoryStore
from aaestro.modules.memoryset import MemoryKind, MemoryScope


@pytest.fixture
def service():
    store = SqliteMemoryStore()
    try:
        yield MemoryService(store=store)
    finally:
        store.close()


def entry(**overrides) -> MemoryEntry:
    base = {
        "space": "placeholder",
        "kind": MemoryKind.FACT,
        "content": "사실 하나",
        "source": MemorySource(agent="researcher", run_id="run-1"),
    }
    base.update(overrides)
    return MemoryEntry(**base)


def token(**overrides) -> AccessToken:
    base = {
        "agent": "researcher",
        "group": "research",
        "local": [("longterm", "researcher")],
    }
    base.update(overrides)
    return build_token(**base)


# --- 미선언 space --------------------------------------------------------------


def test_undeclared_space_is_denied(service):
    """선언되지 않은 space 는 존재조차 알려주지 않는다."""
    with pytest.raises(AaestroError) as exc_info:
        service.append(token(), "secret", entry())

    assert exc_info.value.code == "MEM_001"
    assert exc_info.value.category is ErrorCategory.MEMORY


def test_undeclared_space_read_is_denied(service):
    with pytest.raises(AaestroError) as exc_info:
        service.read(token(), "secret")

    assert exc_info.value.code == "MEM_001"


# --- group scope --------------------------------------------------------------


def test_member_can_write_a_group_space(service):
    member = token(group_spaces=[("knowledge", MemoryMode.RW)])

    stored = service.append(member, "knowledge", entry())

    assert stored.space == "group:research:knowledge"


def test_non_member_cannot_see_a_group_space(service):
    """그룹 소속이 곧 접근 경계다 — 비멤버는 읽기도 불가하다."""
    outsider = build_token(agent="writer", group=None, local=[("longterm", "writer")])

    with pytest.raises(AaestroError) as exc_info:
        service.read(outsider, "knowledge")

    assert exc_info.value.code == "MEM_001"


def test_read_only_group_space_rejects_append(service):
    member = token(group_spaces=[("knowledge", MemoryMode.RO)])

    with pytest.raises(AaestroError) as exc_info:
        service.append(member, "knowledge", entry())

    assert exc_info.value.code == "MEM_001"


def test_read_only_group_space_still_reads(service):
    member = token(group_spaces=[("knowledge", MemoryMode.RO)])

    assert service.read(member, "knowledge") == ()


# --- global scope --------------------------------------------------------------


def test_global_space_is_read_only_by_default(service):
    """전역은 기본 read-only — 아무나 쓰면 전사 지식이 오염된다."""
    reader = token(global_spaces=[("org", ())])

    with pytest.raises(AaestroError) as exc_info:
        service.append(reader, "org", entry())

    assert exc_info.value.code == "MEM_001"


def test_declared_writer_can_write_global(service):
    librarian = build_token(agent="librarian", group=None, global_spaces=[("org", ("librarian",))])

    stored = service.append(librarian, "org", entry(source=MemorySource(agent="librarian")))

    assert stored.space == "global:global:org"


def test_agent_outside_writers_cannot_write_global(service):
    other = token(global_spaces=[("org", ("librarian",))])

    with pytest.raises(AaestroError) as exc_info:
        service.append(other, "org", entry())

    assert exc_info.value.code == "MEM_001"


def test_everyone_may_read_global(service):
    reader = token(global_spaces=[("org", ("librarian",))])

    assert service.read(reader, "org") == ()


# --- 별칭 해석 순서 -------------------------------------------------------------


def test_local_alias_wins_over_group_and_global(service):
    """가까운 스코프가 이긴다 — local > group > global."""
    conflicted = build_token(
        agent="researcher",
        group="research",
        local=[("notes", "researcher")],
        group_spaces=[("notes", MemoryMode.RW)],
        global_spaces=[("notes", ("researcher",))],
    )

    stored = service.append(conflicted, "notes", entry())

    assert stored.space == "local:researcher:notes"


def test_group_alias_wins_over_global(service):
    conflicted = build_token(
        agent="researcher",
        group="research",
        group_spaces=[("notes", MemoryMode.RW)],
        global_spaces=[("notes", ("researcher",))],
    )

    stored = service.append(conflicted, "notes", entry())

    assert stored.space == "group:research:notes"


def test_global_alias_is_used_when_alone(service):
    only_global = build_token(
        agent="researcher", group=None, global_spaces=[("notes", ("researcher",))]
    )

    stored = service.append(only_global, "notes", entry())

    assert stored.space == "global:global:notes"


def test_resolve_returns_none_for_unknown_alias():
    assert token().resolve("absent") is None


# --- run scope / direct 태스크 -------------------------------------------------


def test_graph_task_reaches_the_run_space(service):
    graph_task = token(run_spaces=[("scratch", "run-1")])

    stored = service.append(graph_task, "scratch", entry())

    assert stored.space == "run:run-1:scratch"


def test_direct_task_has_no_run_space(service):
    """direct 태스크는 어떤 graph run 의 state 도 기억도 건드리지 않는다."""
    direct = token()  # run_spaces 미부여

    with pytest.raises(AaestroError) as exc_info:
        service.append(direct, "scratch", entry())

    assert exc_info.value.code == "MEM_001"


def test_group_move_revokes_the_previous_group_space(service):
    """그룹을 옮기면 이전 그룹 space 접근을 즉시 잃는다 — 토큰 재발급."""
    moved = build_token(
        agent="researcher",
        group="ops",
        local=[("longterm", "researcher")],
        group_spaces=[("ops-notes", MemoryMode.RW)],
    )

    with pytest.raises(AaestroError):
        service.read(moved, "knowledge")


def test_agent_without_a_group_gets_no_group_spaces():
    """group=None 이면 group 선언을 넘겨도 반영되지 않는다."""
    solo = build_token(agent="writer", group=None, group_spaces=[("knowledge", MemoryMode.RW)])

    assert solo.resolve("knowledge") is None


# --- 감사 로그 -----------------------------------------------------------------


def test_successful_access_is_audited(service):
    service.append(token(), "longterm", entry())

    record = service.audit_log[-1]
    assert record["agent"] == "researcher"
    assert record["group"] == "research"
    assert record["op"] == "append"
    assert record["status"] == "ok"


def test_denied_access_is_audited(service):
    """거부도 기록되어야 침해 시도를 추적할 수 있다."""
    with pytest.raises(AaestroError):
        service.append(token(), "secret", entry())

    record = service.audit_log[-1]
    assert record["status"] == "denied"
    assert record["memory_space"] == "secret"


# --- 격리 --------------------------------------------------------------------


def test_two_agents_local_spaces_do_not_collide(service):
    """같은 별칭이라도 소유자가 다르면 서로의 기억을 덮어쓰지 않는다."""
    a = build_token(agent="researcher", group=None, local=[("longterm", "researcher")])
    b = build_token(agent="writer", group=None, local=[("longterm", "writer")])

    service.append(a, "longterm", entry(content="mine"))
    service.append(b, "longterm", entry(content="theirs", source=MemorySource(agent="writer")))

    assert [e.content for e in service.read(a, "longterm")] == ["mine"]
    assert [e.content for e in service.read(b, "longterm")] == ["theirs"]


def test_correction_chain_is_reachable_through_the_service(service):
    original = service.append(token(), "longterm", entry(content="v1"))
    service.append(token(), "longterm", entry(content="v2", supersedes=original.entry_id))

    latest = service.latest(token(), "longterm", original.entry_id)

    assert latest is not None
    assert latest.content == "v2"


def test_space_id_encodes_scope_and_owner():
    space = MemorySpace(alias="notes", scope=MemoryScope.GROUP, owner="research")

    assert space.space_id == "group:research:notes"


def test_every_scope_has_a_precedence():
    """우선순위에 빠진 스코프의 별칭은 해석 단계에서 터진다."""
    from aaestro.memory.service import SCOPE_PRECEDENCE

    assert set(MemoryScope) == set(SCOPE_PRECEDENCE)


def test_run_alias_outranks_group_and_global(service):
    """run 한정 기억이 공용 지식보다 이 태스크에 가깝다."""
    conflicted = build_token(
        agent="researcher",
        group="research",
        run_spaces=[("notes", "run-1")],
        group_spaces=[("notes", MemoryMode.RW)],
        global_spaces=[("notes", ("researcher",))],
    )

    stored = service.append(conflicted, "notes", entry())

    assert stored.space == "run:run-1:notes"


def test_entry_cannot_claim_a_space_it_lacks_access_to(service):
    """항목이 space 를 스스로 주장해도 해석된 id 로 덮어쓴다 — space 위조 차단."""
    tok = token()
    forged = entry(space="global:global:org")

    stored = service.append(tok, "longterm", forged)

    assert stored.space == "local:researcher:longterm"


def test_global_space_defaults_to_read_only():
    """기본값이 과한 권한을 주면 선언을 빠뜨렸을 때 조용히 열린다."""
    space = MemorySpace(alias="org", scope=MemoryScope.GLOBAL, owner="global")

    assert space.may_write("anyone") is False


# --- latest() 의 space 경계 --------------------------------------------------


def test_latest_does_not_cross_space_boundaries(service):
    """entry_id 만으로 체인을 따라가면 id 를 아는 것만으로 남의 기억을 읽는다."""
    victim = build_token(agent="writer", group=None, local=[("longterm", "writer")])
    secret = service.append(
        victim, "longterm", entry(content="기밀", source=MemorySource(agent="writer"))
    )
    attacker = build_token(agent="researcher", group=None, local=[("longterm", "researcher")])

    assert service.latest(attacker, "longterm", secret.entry_id) is None


def test_cross_space_latest_is_audited_as_denied(service):
    victim = build_token(agent="writer", group=None, local=[("longterm", "writer")])
    secret = service.append(
        victim, "longterm", entry(content="기밀", source=MemorySource(agent="writer"))
    )
    attacker = build_token(agent="researcher", group=None, local=[("longterm", "researcher")])

    service.latest(attacker, "longterm", secret.entry_id)

    assert service.audit_log[-1] == {
        "agent": "researcher",
        "group": "",
        "memory_space": "longterm",
        "op": "latest",
        "status": "denied",
    }


def test_latest_within_the_space_is_audited_as_ok(service):
    original = service.append(token(), "longterm", entry(content="v1"))

    service.latest(token(), "longterm", original.entry_id)

    assert service.audit_log[-1]["op"] == "latest"
    assert service.audit_log[-1]["status"] == "ok"


def test_latest_on_an_undeclared_space_is_denied(service):
    with pytest.raises(AaestroError) as exc_info:
        service.latest(token(), "secret", "any")

    assert exc_info.value.code == "MEM_001"
    assert service.audit_log[-1]["status"] == "denied"


# --- 메트릭 배선 ---------------------------------------------------------------


def metered_service(store):
    """계측을 물린 서비스와 그 registry."""
    from prometheus_client import CollectorRegistry

    from aaestro.observability.metrics import Metrics

    metrics = Metrics(registry=CollectorRegistry())
    return MemoryService(store=store, metrics=metrics), metrics


def metric_value(metrics, **labels) -> float:
    return metrics.registry.get_sample_value("aaestro_memory_operations_total", labels) or 0.0


def test_successful_append_is_counted():
    store = SqliteMemoryStore()
    try:
        service, metrics = metered_service(store)

        service.append(token(), "longterm", entry())

        assert metric_value(metrics, space="longterm", op="append", status="ok") == 1.0
    finally:
        store.close()


def test_denied_access_is_counted_too():
    """거부가 카운터에서 빠지면 ACL 위반 시도가 관측되지 않는다."""
    store = SqliteMemoryStore()
    try:
        service, metrics = metered_service(store)

        with pytest.raises(AaestroError):
            service.append(token(), "secret", entry())

        assert metric_value(metrics, space="secret", op="append", status="denied") == 1.0
    finally:
        store.close()


def test_memory_service_works_without_metrics(service):
    stored = service.append(token(), "longterm", entry())

    assert stored.entry_id


# --- 정정은 같은 space 안에서만 (#324) ----------------------------------------------


def _org_and_attacker():
    """global org 를 읽기만 하는 에이전트와, org 에 쓸 수 있는 사서."""
    store = SqliteMemoryStore()
    service = MemoryService(store=store)
    librarian = build_token(agent="librarian", group=None, global_spaces=[("org", ("librarian",))])
    attacker = build_token(
        agent="attacker", group=None, local=[("longterm", "attacker")], global_spaces=[("org", ())]
    )
    return store, service, librarian, attacker


def test_append_refuses_a_correction_that_points_into_another_space():
    store, service, librarian, attacker = _org_and_attacker()
    org_entry = service.append(
        librarian,
        "org",
        MemoryEntry(
            space="x", kind=MemoryKind.FACT, content="org policy", source=MemorySource(agent="l")
        ),
    )

    with pytest.raises(AaestroError) as exc_info:
        service.append(
            attacker,
            "longterm",
            MemoryEntry(
                space="x",
                kind=MemoryKind.FACT,
                content="hide it",
                source=MemorySource(agent="attacker"),
                supersedes=org_entry.entry_id,
            ),
        )

    err = exc_info.value
    assert err.code == "VAL_002"
    assert err.message == "supersedes must reference an entry in the same space"
    assert err.details == {"space": "longterm"}
    assert org_entry.entry_id not in str(err.details), "대상 id 를 흘리지 않는다"
    assert store.list_space("local:attacker:longterm") == ()
    store.close()


def test_append_refuses_a_correction_whose_target_is_gone_with_the_same_error():
    """없는 id 와 다른 space 의 id 를 다르게 답하면 다른 space 의 id 존재 여부가 샌다."""
    store, service, librarian, attacker = _org_and_attacker()
    org_entry = service.append(
        librarian,
        "org",
        MemoryEntry(
            space="x", kind=MemoryKind.FACT, content="org policy", source=MemorySource(agent="l")
        ),
    )

    def attempt(target: str) -> AaestroError:
        with pytest.raises(AaestroError) as exc_info:
            service.append(
                attacker,
                "longterm",
                MemoryEntry(
                    space="x",
                    kind=MemoryKind.FACT,
                    content="correction",
                    source=MemorySource(agent="attacker"),
                    supersedes=target,
                ),
            )
        return exc_info.value

    missing, foreign = attempt("no-such-entry"), attempt(org_entry.entry_id)

    assert (missing.code, missing.message, missing.details) == (
        foreign.code,
        foreign.message,
        foreign.details,
    )
    assert missing.code == "VAL_002"
    # 검증 실패는 권한 거부가 아니다 — denied 는 memory ACL 전용 어휘
    assert {r["status"] for r in service.audit_log if r["op"] == "append"} >= {"failed"}
    assert not [r for r in service.audit_log if r["op"] == "append" and r["status"] == "denied"]
    store.close()


def test_append_allows_a_correction_inside_the_same_space():
    store, service, _librarian, attacker = _org_and_attacker()
    first = service.append(
        attacker,
        "longterm",
        MemoryEntry(
            space="x", kind=MemoryKind.FACT, content="old", source=MemorySource(agent="attacker")
        ),
    )

    stored = service.append(
        attacker,
        "longterm",
        MemoryEntry(
            space="x",
            kind=MemoryKind.FACT,
            content="new",
            source=MemorySource(agent="attacker"),
            supersedes=first.entry_id,
        ),
    )

    assert stored.supersedes == first.entry_id
    store.close()
