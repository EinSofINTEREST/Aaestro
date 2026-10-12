"""Artifact handoff between graph nodes.

02 Output Discipline 은 대용량 산출물을 **참조로** 전달하라고 규정한다.
참조가 state 를 타고 다음 노드로 건너가지 못하면 그 규정은 죽은 글자다 (#155).

artifact 가 graph state 를 **우회**하면 01 이 금지하는 사이드채널이 된다 —
그래서 참조는 반드시 state 를 거쳐야 한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aaestro.artifacts.scope import ArtifactScope, ScopedArtifacts
from aaestro.artifacts.store import FilesystemArtifactStore
from aaestro.core.errors import AaestroError, ErrorCode

PAYLOAD = b"# a report too large for state\n" + b"x" * 4096


def load_manifest(name: str):
    """manifest 를 읽는다 — 파일 I/O 는 동기 헬퍼에 둔다.

    async 테스트 안에서 열면 린터가 막고, 실제로도 이벤트 루프에서 blocking
    I/O 를 하는 셈이다.
    """
    import yaml

    from aaestro.core.manifest import AgentManifest

    declared = Path(f"agents/{name}/manifest.yaml").read_text(encoding="utf-8")
    return AgentManifest.model_validate(yaml.safe_load(declared))


def access_for(tmp_path, agent: str, *, group: str | None = None, quotas=None):
    """한 에이전트가 보는 스코프들."""
    stores = {
        ArtifactScope.LOCAL: FilesystemArtifactStore(root=tmp_path, scope=agent),
        ArtifactScope.GLOBAL: FilesystemArtifactStore(root=tmp_path, scope="global"),
    }
    if group:
        stores[ArtifactScope.GROUP] = FilesystemArtifactStore(root=tmp_path, scope=group)
    return ScopedArtifacts(stores=stores, quotas=quotas or {})


# --- 노드 간 전달 ---------------------------------------------------------------


async def test_a_reference_survives_the_handoff_between_nodes(tmp_path):
    """#155 의 핵심 — 만든 노드와 읽는 노드가 다르다."""
    producer = access_for(tmp_path, "researcher", group="research")
    consumer = access_for(tmp_path, "writer", group="research")

    # 노드 1: **그룹** 스코프에 남기고 참조만 state 로 반환한다.
    # local 에 쓰면 다른 에이전트가 읽을 수 없다 — 01 이 "그룹 산출물"을
    # 규정하는 이유가 이것이다
    ref = await producer.put("findings.md", PAYLOAD, scope=ArtifactScope.GROUP)
    state = {"findings_ref": ref}

    # 노드 2: state 의 참조로 읽는다
    assert await consumer.get(state["findings_ref"]) == PAYLOAD


async def test_the_state_carries_a_reference_not_the_payload(tmp_path):
    """산출물을 state 에 통째로 실으면 02 Rule 5 가 깨진다."""
    producer = access_for(tmp_path, "researcher")

    ref = await producer.put("findings.md", PAYLOAD)

    assert ref.startswith("artifact://")
    assert len(ref) < 100
    assert PAYLOAD.decode(errors="replace")[:20] not in ref


async def test_a_consumer_outside_the_group_cannot_read_it(tmp_path):
    """비멤버가 그룹 산출물을 읽으면 artifact 가 사이드채널이 된다."""
    producer = access_for(tmp_path, "researcher", group="research")
    outsider = access_for(tmp_path, "stranger")  # 그룹 없음

    ref = await FilesystemArtifactStore(root=tmp_path, scope="research").put("shared", PAYLOAD)
    assert await producer.get(ref) == PAYLOAD

    with pytest.raises(AaestroError) as exc_info:
        await outsider.get(ref)

    assert exc_info.value.code == ErrorCode.ART_001


async def test_a_local_artifact_does_not_cross_to_another_agent(tmp_path):
    """local 에 쓴 것은 넘어가지 않는다 — 전달하려면 group 을 써야 한다."""
    producer = access_for(tmp_path, "researcher", group="research")
    consumer = access_for(tmp_path, "writer", group="research")

    ref = await producer.put("scratch.md", PAYLOAD)

    with pytest.raises(AaestroError) as exc_info:
        await consumer.get(ref)

    assert exc_info.value.code == ErrorCode.ART_001


async def test_the_global_scope_is_read_only(tmp_path):
    """전역 산출물에 아무나 쓰면 전사 공용이 오염된다 (09 와 같은 규약)."""
    access = access_for(tmp_path, "researcher", group="research")

    with pytest.raises(AaestroError) as exc_info:
        await access.put("org.md", PAYLOAD, scope=ArtifactScope.GLOBAL)

    assert exc_info.value.code == ErrorCode.ART_001


async def test_a_granted_agent_can_write_globally(tmp_path):
    """09 의 writers 처럼 명시 허가는 열어 둔다."""
    access = access_for(tmp_path, "librarian")
    access.writable = frozenset({ArtifactScope.LOCAL, ArtifactScope.GLOBAL})

    ref = await access.put("org.md", PAYLOAD, scope=ArtifactScope.GLOBAL)

    assert await access.get(ref) == PAYLOAD


async def test_a_consumer_cannot_read_another_agents_local_scope(tmp_path):
    """local 은 소유 에이전트만 본다 (01 Resource Scoping)."""
    producer = access_for(tmp_path, "researcher")
    other = access_for(tmp_path, "writer")

    ref = await producer.put("private.md", PAYLOAD)

    with pytest.raises(AaestroError) as exc_info:
        await other.get(ref)

    assert exc_info.value.code == ErrorCode.ART_001


# --- 조립부 배선 ----------------------------------------------------------------


def test_the_assembly_fills_scopes_from_membership(tmp_path, monkeypatch):
    """소속이 스코프를 정한다 — 조립부가 잘못 채우면 ACL 이 무의미해진다."""

    from aaestro.agentd.__main__ import ARTIFACT_ROOT_ENV, _artifact_store

    monkeypatch.setenv(ARTIFACT_ROOT_ENV, str(tmp_path))
    built = _artifact_store(load_manifest("researcher"))

    scopes = {scope: store.scope for scope, store in built.stores.items()}
    assert scopes[ArtifactScope.LOCAL] == "researcher"
    assert scopes[ArtifactScope.GROUP] == "research"
    assert scopes[ArtifactScope.GLOBAL] == "global"


def test_an_agent_without_a_group_gets_no_group_scope(tmp_path, monkeypatch):
    """소속이 없는데 그룹 스코프를 주면 남의 산출물이 보인다."""

    from aaestro.agentd.__main__ import ARTIFACT_ROOT_ENV, _artifact_store

    monkeypatch.setenv(ARTIFACT_ROOT_ENV, str(tmp_path))
    built = _artifact_store(load_manifest("echo"))

    assert ArtifactScope.GROUP not in built.stores


def test_quotas_are_read_from_the_injected_declaration(tmp_path, monkeypatch):
    """컨테이너는 groups/*.yaml 을 볼 수 없다 — 값만 넘겨받는다."""

    from aaestro.agentd.__main__ import (
        ARTIFACT_QUOTA_ENV,
        ARTIFACT_ROOT_ENV,
        _artifact_store,
    )

    monkeypatch.setenv(ARTIFACT_ROOT_ENV, str(tmp_path))
    monkeypatch.setenv(ARTIFACT_QUOTA_ENV, "local=1024,group=2048")
    built = _artifact_store(load_manifest("researcher"))

    assert built.quotas[ArtifactScope.LOCAL] == 1024
    assert built.quotas[ArtifactScope.GROUP] == 2048


async def test_an_injected_quota_is_enforced_on_the_real_path(tmp_path, monkeypatch):
    """선언만 읽고 강제하지 않으면 quota 는 장식이다."""

    from aaestro.agentd.__main__ import (
        ARTIFACT_QUOTA_ENV,
        ARTIFACT_ROOT_ENV,
        _artifact_store,
    )

    monkeypatch.setenv(ARTIFACT_ROOT_ENV, str(tmp_path))
    monkeypatch.setenv(ARTIFACT_QUOTA_ENV, "local=100")
    built = _artifact_store(load_manifest("researcher"))

    with pytest.raises(AaestroError) as exc_info:
        await built.put("too-big", b"x" * 200)

    assert exc_info.value.code == ErrorCode.ART_002
