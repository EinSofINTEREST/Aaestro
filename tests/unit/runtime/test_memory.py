"""Memory access adapter tests.

토큰 조립 자체는 ``tests/unit/memory/test_tokens.py`` 가 본다.
"""

from __future__ import annotations

import pytest

from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.core.manifest import AgentManifest
from aaestro.memory.entry import MemoryEntry, MemorySource
from aaestro.memory.index import IndexRegistry, SpaceIndex
from aaestro.memory.recall import Recall
from aaestro.memory.service import MemoryService
from aaestro.memory.store import SqliteMemoryStore
from aaestro.memory.tokens import issue_token
from aaestro.modules.memoryset import ChunkSpec, MemoryKind
from aaestro.runtime.memory import ServiceMemoryAccess
from tests.fixtures.builders import manifest_dict


def manifest(*, group_name: str | None = None, **memory: object) -> AgentManifest:
    """local space 를 선언한 매니페스트 — 그룹 소속은 metadata 가 정한다."""
    spec = manifest_dict()
    if memory:
        spec["spec"]["memory"] = memory
    if group_name is not None:
        spec["metadata"]["group"] = group_name
    return AgentManifest.model_validate(spec)


LOCAL = {"spaces": [{"ref": "memorysets/agent-longterm@0.1.0", "as": "longterm"}]}
GROUP_SPACES = [{"ref": "memorysets/domain-knowledge@0.1.0", "as": "knowledge", "mode": "rw"}]


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
                service=service, token=token, recall=Recall(indexes=registry.indexes)
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


async def test_scan_limit_applies_to_every_space(access, monkeypatch):
    """comprehension 안에서 pop 하면 첫 space 만 값을 쓰고 나머지는 기본값이 된다."""
    adapter, space_id, _registry = access
    seen: list[int] = []
    original = adapter.service.read

    def spy(token, alias, **kwargs):
        seen.append(kwargs.get("limit"))
        return original(token, alias, **kwargs)

    monkeypatch.setattr(adapter.service, "read", spy)

    await adapter.search("무엇이든", spaces=["longterm", "longterm"], scan=7)

    # 중복 별칭은 한 번만 보고, 그 한 번은 지정한 상한을 쓴다
    assert seen == [7]


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


async def test_recall_is_skipped_when_the_policy_disables_it(access, monkeypatch):
    """정책이 껐는데 500건씩 훑으면 쓰지도 않을 데이터를 읽는다."""
    from aaestro.modules.memoryset import RecallSpec

    adapter, space_id, registry = access
    item = entry(space_id, "mcp sidecar 는 이미지 태그 고정이 필요하다")
    await adapter.append("longterm", entry=item)
    registry.indexes[space_id].add(item, ChunkSpec(max_tokens=400, overlap_tokens=40))

    reads: list[str] = []
    original = adapter.service.read
    monkeypatch.setattr(
        adapter.service,
        "read",
        lambda token, alias, **kw: (reads.append(alias), original(token, alias, **kw))[1],
    )

    context = await adapter.recall_for_task(
        "sidecar", policy=RecallSpec(auto=False, k=3, min_score=0.0)
    )

    assert context == ""
    assert reads == []


async def test_recall_reads_each_alias_once(access, monkeypatch):
    """같은 별칭이 여러 스코프에 있으면 중복 읽기와 중복 히트로 예산이 낭비된다."""
    from aaestro.memory.service import MemorySpace
    from aaestro.modules.memoryset import MemoryScope, RecallSpec

    adapter, space_id, _registry = access
    # 같은 별칭을 group 스코프에도 둔다 — 토큰은 둘 다 담는다
    adapter.token = adapter.token.__class__(
        agent=adapter.token.agent,
        group="research",
        spaces=(
            *adapter.token.spaces,
            MemorySpace(alias="longterm", scope=MemoryScope.GROUP, owner="research"),
        ),
    )

    reads: list[str] = []
    original = adapter.service.read
    monkeypatch.setattr(
        adapter.service,
        "read",
        lambda token, alias, **kw: (reads.append(alias), original(token, alias, **kw))[1],
    )

    await adapter.recall_for_task("sidecar", policy=RecallSpec(auto=True, k=3, min_score=0.0))

    assert reads == ["longterm"]
