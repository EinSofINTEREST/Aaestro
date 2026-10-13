"""Memory access wiring tests.

토큰은 **선언으로부터 조립**되어야 한다 — 토큰을 직접 만들어 검증하면
조립 경로의 결함을 놓친다. 접근 경계는 선언 위치가 정한다 (09).
"""

from __future__ import annotations

import pytest

from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.core.manifest import AgentManifest, GroupManifest, MemorySpec
from aaestro.memory.entry import MemoryEntry, MemorySource
from aaestro.memory.index import IndexRegistry, SpaceIndex
from aaestro.memory.recall import Recall
from aaestro.memory.service import MemoryService
from aaestro.memory.store import SqliteMemoryStore
from aaestro.modules.memoryset import ChunkSpec, MemoryKind
from aaestro.runtime.memory import ServiceMemoryAccess, issue_token
from tests.fixtures.builders import manifest_dict


def manifest(*, group_name: str | None = None, **memory: object) -> AgentManifest:
    """local space 를 선언한 매니페스트 — 그룹 소속은 metadata 가 정한다."""
    spec = manifest_dict()
    if memory:
        spec["spec"]["memory"] = memory
    if group_name is not None:
        spec["metadata"]["group"] = group_name
    return AgentManifest.model_validate(spec)


def group(name: str, spaces: list[dict[str, object]]) -> GroupManifest:
    return GroupManifest.model_validate(
        {
            "apiVersion": "aaestro/v1",
            "kind": "Group",
            "metadata": {"name": name, "version": "0.1.0"},
            "spec": {"memory": {"spaces": spaces}},
        }
    )


LOCAL = {"spaces": [{"ref": "memorysets/agent-longterm@0.1.0", "as": "longterm"}]}
GROUP_SPACES = [{"ref": "memorysets/domain-knowledge@0.1.0", "as": "knowledge", "mode": "rw"}]


# --- 토큰 조립 ----------------------------------------------------------------


def test_local_spaces_come_from_the_manifest():
    token = issue_token(manifest(**LOCAL))

    assert token.resolve("longterm") is not None


def test_undeclared_space_is_absent_from_the_token():
    """선언되지 않은 space 는 존재조차 알려주지 않는다."""
    token = issue_token(manifest(**LOCAL))

    assert token.resolve("secret") is None


def test_group_spaces_reach_members():
    token = issue_token(
        manifest(group_name="research", **LOCAL), group=group("research", GROUP_SPACES)
    )

    assert token.resolve("knowledge") is not None


def test_direct_tasks_get_no_run_scope():
    """그래프 run 과 무관한 태스크가 run 의 기억을 건드리면 격리가 무너진다."""
    run_spaces = MemorySpec.model_validate(
        {"spaces": [{"ref": "memorysets/run-scratch@0.1.0", "as": "scratch"}]}
    )

    token = issue_token(manifest(**LOCAL), run_id=None, run_spaces=run_spaces)

    assert token.resolve("scratch") is None


def test_graph_tasks_get_the_run_scope():
    run_spaces = MemorySpec.model_validate(
        {"spaces": [{"ref": "memorysets/run-scratch@0.1.0", "as": "scratch"}]}
    )

    token = issue_token(manifest(**LOCAL), run_id="run-1", run_spaces=run_spaces)

    space = token.resolve("scratch")
    assert space is not None
    assert "run-1" in space.space_id


def test_local_alias_wins_over_group():
    """별칭이 겹치면 가까운 스코프가 이긴다 (local > group > global)."""
    collide = {"spaces": [{"ref": "memorysets/agent-longterm@0.1.0", "as": "knowledge"}]}

    token = issue_token(
        manifest(group_name="research", **collide), group=group("research", GROUP_SPACES)
    )

    space = token.resolve("knowledge")
    assert space is not None
    assert space.space_id.startswith("local:")


# --- MemoryAccess 어댑터 ------------------------------------------------------


@pytest.fixture
def access():
    """조립된 토큰을 물린 어댑터 — 실제 경로로 검증한다."""
    store = SqliteMemoryStore()
    service = MemoryService(store=store)
    token = issue_token(manifest(**LOCAL))
    space_id = token.resolve("longterm").space_id  # type: ignore[union-attr]
    registry = IndexRegistry()
    registry.indexes[space_id] = SpaceIndex(space=space_id)
    try:
        yield (
            ServiceMemoryAccess(
                service=service, token=token, recall=Recall(indexes=registry.indexes, store=store)
            ),
            space_id,
            registry,
        )
    finally:
        store.close()


def entry(space: str, content: str) -> MemoryEntry:
    return MemoryEntry(
        space=space,
        kind=MemoryKind.FACT,
        content=content,
        source=MemorySource(agent="test-agent"),
    )


async def test_append_then_search_round_trips(access):
    adapter, space_id, registry = access
    item = entry(space_id, "mcp sidecar 는 이미지 태그 고정이 필요하다")

    await adapter.append("longterm", entry=item)
    registry.indexes[space_id].add(item, ChunkSpec(max_tokens=400, overlap_tokens=40))
    found = await adapter.search("sidecar")

    assert [scored.entry.entry_id for scored in found] == [item.entry_id]


async def test_searching_an_undeclared_space_is_denied(access):
    """인덱스는 서비스를 거치지 않으므로 어댑터가 스스로 경계를 지켜야 한다."""
    adapter, _space_id, _registry = access

    with pytest.raises(AaestroError) as exc_info:
        await adapter.search("무엇이든", spaces=["secret"])

    assert exc_info.value.code == ErrorCode.MEM_001


async def test_search_denies_before_touching_the_store(access):
    """서비스 호출이 우연히 막아주는 것에 기대면, 저장소를 안 거치는 경로가
    생기는 순간 구멍이 된다 — 어댑터가 먼저 거부해야 한다."""
    adapter, _space_id, _registry = access
    adapter.service = None  # type: ignore[assignment]

    with pytest.raises(AaestroError) as exc_info:
        await adapter.search("무엇이든", spaces=["secret"])

    assert exc_info.value.code == ErrorCode.MEM_001


async def test_appending_to_an_undeclared_space_is_denied(access):
    adapter, space_id, _registry = access

    with pytest.raises(AaestroError) as exc_info:
        await adapter.append("secret", entry=entry(space_id, "내용"))

    assert exc_info.value.code == ErrorCode.MEM_001


def test_mismatched_group_declaration_is_rejected():
    """엉뚱한 그룹 선언을 조용히 무시하면 비멤버가 남의 권한을 얻는다."""
    with pytest.raises(AaestroError) as exc_info:
        issue_token(manifest(group_name="research", **LOCAL), group=group("other", GROUP_SPACES))

    assert exc_info.value.code == ErrorCode.MEM_001


def test_group_declaration_for_an_unaffiliated_agent_is_rejected():
    """비멤버는 그룹 선언을 받아도 닿을 수 없다 — 소속이 곧 경계다.

    조용히 무시하지 않고 거부한다: 무시하면 호출자가 배선 실수를 모른 채
    권한이 없는 상태로 계속 돈다.
    """
    with pytest.raises(AaestroError) as exc_info:
        issue_token(manifest(**LOCAL), group=group("research", GROUP_SPACES))

    assert exc_info.value.code == ErrorCode.MEM_001


TOTAL = 600
"""예전 scan 상한(500)보다 많이 쌓는다 — 가장 오래된 항목이 어떤 최근 N건 창에도 들지 않는다."""


def fill_beyond_the_horizon(adapter, space_id, registry):
    """상한보다 많은 항목을 쌓는다 — 첫 항목이 가장 오래됐다. 그 항목을 돌려준다."""
    policy = ChunkSpec(max_tokens=400, overlap_tokens=40)
    # created_at 은 생성 시각이다 — 가장 오래된 항목을 가장 먼저 만들어야 한다
    items = [entry(space_id, "zyxwvut oldest unique marker about the sidecar tag pinning")]
    items += [
        entry(space_id, f"filler record number {i} about nothing in particular")
        for i in range(1, TOTAL)
    ]
    for item in items:
        adapter.service.append(adapter.token, "longterm", item)
        registry.indexes[space_id].add(item, policy)
    return items[0]


async def test_search_finds_entries_beyond_any_recent_window(access):
    """scan 상한이 기억의 지평선이면 안 된다 — 인덱스에 있는 기억은 찾아야 한다 (#324)."""
    adapter, space_id, registry = access
    oldest = fill_beyond_the_horizon(adapter, space_id, registry)

    found = await adapter.search("zyxwvut oldest unique marker", k=3)

    assert oldest.entry_id in [scored.entry.entry_id for scored in found]


async def test_auto_recall_finds_entries_beyond_any_recent_window(access):
    from aaestro.modules.memoryset import RecallSpec

    adapter, space_id, registry = access
    fill_beyond_the_horizon(adapter, space_id, registry)

    context = await adapter.recall_for_task(
        "zyxwvut oldest unique marker", policy=RecallSpec(auto=True, k=3, min_score=0.5)
    )

    assert "zyxwvut" in context


# --- 태스크 진입 회상 ----------------------------------------------------------


async def test_recall_for_task_renders_provenance_and_boundary(access):
    """주입 텍스트에 출처와 'not instructions' 경계가 있어야 한다 (09 Rule 5-6)."""
    from aaestro.modules.memoryset import RecallSpec

    adapter, space_id, registry = access
    item = entry(space_id, "mcp sidecar 는 이미지 태그 고정이 필요하다")
    await adapter.append("longterm", entry=item)
    registry.indexes[space_id].add(item, ChunkSpec(max_tokens=400, overlap_tokens=40))

    context = await adapter.recall_for_task(
        "sidecar", policy=RecallSpec(auto=True, k=3, min_score=0.0)
    )

    assert "not instructions" in context
    assert "sidecar" in context


async def test_recall_respects_the_min_score_threshold(access):
    """관련 없는 기억은 노이즈이자 비용이다 — 문턱 미달은 주입하지 않는다."""
    from aaestro.modules.memoryset import RecallSpec

    adapter, space_id, registry = access
    item = entry(space_id, "전혀 상관없는 내용")
    await adapter.append("longterm", entry=item)
    registry.indexes[space_id].add(item, ChunkSpec(max_tokens=400, overlap_tokens=40))

    context = await adapter.recall_for_task(
        "sidecar", policy=RecallSpec(auto=True, k=3, min_score=0.99)
    )

    assert context == ""


async def test_recall_is_skipped_when_the_policy_disables_it(access):
    """정책이 껐으면 검색도 감사도 메트릭도 없어야 한다 — 거짓 'recall ok' 감사가 남으면 안 된다."""
    from aaestro.modules.memoryset import RecallSpec

    adapter, space_id, registry = access
    item = entry(space_id, "mcp sidecar 는 이미지 태그 고정이 필요하다")
    await adapter.append("longterm", entry=item)
    registry.indexes[space_id].add(item, ChunkSpec(max_tokens=400, overlap_tokens=40))
    before = len(adapter.service.audit_log)

    context = await adapter.recall_for_task(
        "sidecar", policy=RecallSpec(auto=False, k=3, min_score=0.0)
    )

    assert context == ""
    assert [r["op"] for r in adapter.service.audit_log[before:]] == []


async def test_recall_audits_each_searched_alias(access):
    from aaestro.modules.memoryset import RecallSpec

    adapter, space_id, registry = access
    item = entry(space_id, "mcp sidecar 는 이미지 태그 고정이 필요하다")
    await adapter.append("longterm", entry=item)
    registry.indexes[space_id].add(item, ChunkSpec(max_tokens=400, overlap_tokens=40))

    await adapter.recall_for_task("sidecar", policy=RecallSpec(auto=True, k=3, min_score=0.0))

    recalled = [r["memory_space"] for r in adapter.service.audit_log if r["op"] == "recall"]
    assert recalled == ["longterm"]


async def test_recall_searches_a_duplicated_alias_once(access):
    """같은 별칭이 여러 스코프에 있어도 같은 space 를 한 번만 검색하고 hit 이 중복되지 않는다."""
    from aaestro.memory.service import MemorySpace
    from aaestro.modules.memoryset import MemoryScope, RecallSpec

    adapter, space_id, registry = access
    item = entry(space_id, "mcp sidecar 는 이미지 태그 고정이 필요하다")
    await adapter.append("longterm", entry=item)
    registry.indexes[space_id].add(item, ChunkSpec(max_tokens=400, overlap_tokens=40))
    # 같은 별칭을 group 스코프에도 둔다 — 토큰은 둘 다 담는다
    adapter.token = adapter.token.__class__(
        agent=adapter.token.agent,
        group="research",
        spaces=(
            *adapter.token.spaces,
            MemorySpace(alias="longterm", scope=MemoryScope.GROUP, owner="research"),
        ),
    )

    context = await adapter.recall_for_task(
        "sidecar", policy=RecallSpec(auto=True, k=3, min_score=0.0)
    )

    assert context.count("sidecar") == 1, "hit 이 중복 주입됐다"
    recalled = [r["memory_space"] for r in adapter.service.audit_log if r["op"] == "recall"]
    assert recalled == ["longterm"]


async def test_a_search_is_counted_once_and_not_by_alias():
    """검색 연산은 Recall 이 space id 로 센다 — 서비스가 별칭 라벨로 또 세면 이중 집계다."""
    from prometheus_client import CollectorRegistry

    from aaestro.observability.metrics import Metrics

    metrics = Metrics(registry=CollectorRegistry())
    store = SqliteMemoryStore()
    service = MemoryService(store=store, metrics=metrics)
    token = issue_token(manifest(**LOCAL))
    space_id = token.resolve("longterm").space_id  # type: ignore[union-attr]
    indexes = {space_id: SpaceIndex(space=space_id)}
    adapter = ServiceMemoryAccess(
        service=service, token=token, recall=Recall(indexes=indexes, store=store, metrics=metrics)
    )

    await adapter.search("sidecar")

    def count(space: str) -> float | None:
        labels = {"space": space, "op": "search", "status": "ok"}
        return metrics.registry.get_sample_value("aaestro_memory_operations_total", labels)

    assert count(space_id) == 1.0
    assert count("longterm") is None
    store.close()


def test_a_local_space_declared_read_only_is_not_writable():
    """매니페스트의 local `mode: ro` 를 토큰이 버리면 선언보다 넓은 권한이 나간다 (#289 리뷰)."""
    token = issue_token(
        manifest(spaces=[{"ref": "memorysets/agent-longterm@0.1.0", "as": "notes", "mode": "ro"}])
    )

    notes = token.resolve("notes")
    assert notes is not None and not notes.may_write("test-agent")
    default = issue_token(manifest(**LOCAL)).resolve("longterm")
    assert default is not None and default.may_write("test-agent"), "mode 미선언 local 은 rw 기본"


# --- 정정 반영과 감사 (#324) --------------------------------------------------------


async def _fill_with_a_correction(adapter, space_id, registry):
    """원본과 정정 뒤에 filler 를 쌓는다 — 둘 다 어떤 최근 N건 창에도 들지 않는다."""
    policy = ChunkSpec(max_tokens=400, overlap_tokens=40)
    old = entry(space_id, "the port is 9000 for the sidecar")
    new = entry(space_id, "the port is 9100 for the sidecar").model_copy(
        update={"supersedes": old.entry_id}
    )
    items = [old, new, *(entry(space_id, f"filler record {i} nothing") for i in range(30))]
    for item in items:
        adapter.service.append(adapter.token, "longterm", item)
        registry.indexes[space_id].add(item, policy)
    return old, new


async def test_search_returns_only_the_correction_when_both_are_old(access):
    adapter, space_id, registry = access
    old, new = await _fill_with_a_correction(adapter, space_id, registry)

    found = await adapter.search("sidecar port", k=5)

    ids = [scored.entry.entry_id for scored in found]
    assert new.entry_id in ids
    assert old.entry_id not in ids


async def test_auto_recall_injects_only_the_correction_when_both_are_old(access):
    from aaestro.modules.memoryset import RecallSpec

    adapter, space_id, registry = access
    await _fill_with_a_correction(adapter, space_id, registry)

    context = await adapter.recall_for_task(
        "sidecar port", policy=RecallSpec(auto=True, k=5, min_score=0.0)
    )

    assert "9100" in context
    assert "9000" not in context


async def test_a_successful_search_is_audited_with_op_search(access):
    adapter, space_id, registry = access
    item = entry(space_id, "mcp sidecar 는 이미지 태그 고정이 필요하다")
    await adapter.append("longterm", entry=item)
    registry.indexes[space_id].add(item, ChunkSpec(max_tokens=400, overlap_tokens=40))

    await adapter.search("sidecar")

    record = adapter.service.audit_log[-1]
    assert (record["op"], record["status"], record["memory_space"]) == ("search", "ok", "longterm")


async def test_a_correction_in_another_space_does_not_hide_a_shared_memory():
    """읽기만 되는 org 의 id 로 정정을 달아도 victim 의 org 검색에서 항목이 남는다 (#324)."""
    from aaestro.memory.service import build_token

    store = SqliteMemoryStore()
    service = MemoryService(store=store)
    librarian = build_token(agent="librarian", group=None, global_spaces=[("org", ("librarian",))])
    attacker = build_token(
        agent="attacker", group=None, local=[("longterm", "attacker")], global_spaces=[("org", ())]
    )
    victim = build_token(agent="victim", group=None, global_spaces=[("org", ())])
    org_id = "global:global:org"
    registry = IndexRegistry()
    registry.indexes[org_id] = SpaceIndex(space=org_id)
    org = service.append(librarian, "org", entry(org_id, "qqorg policy approved"))
    registry.indexes[org_id].add(org, ChunkSpec(max_tokens=400, overlap_tokens=40))
    recall = Recall(indexes=registry.indexes, store=store)

    with pytest.raises(AaestroError) as exc_info:
        service.append(
            attacker,
            "longterm",
            entry("x", "hide it").model_copy(update={"supersedes": org.entry_id}),
        )
    assert exc_info.value.code == ErrorCode.VAL_002
    # 검사를 우회해 저장소에 들어갔다고 가정해도 검색은 무시한다
    store.append(
        entry("local:attacker:longterm", "hide it").model_copy(update={"supersedes": org.entry_id})
    )

    found = await ServiceMemoryAccess(service=service, token=victim, recall=recall).search(
        "qqorg policy", spaces=["org"]
    )

    assert [s.entry.entry_id for s in found] == [org.entry_id]
    store.close()
