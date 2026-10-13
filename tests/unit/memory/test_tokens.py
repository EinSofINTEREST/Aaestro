"""Access token assembly tests.

토큰은 **선언으로부터 조립**되어야 한다 — 토큰을 직접 만들어 검증하면
조립 경로의 결함을 놓친다. 접근 경계는 선언 위치가 정한다 (09).
"""

from __future__ import annotations

import pytest

from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.core.manifest import AgentManifest, GroupManifest, MemorySpec
from aaestro.memory.tokens import issue_token
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


def test_a_local_space_declared_read_only_is_not_writable():
    """매니페스트의 local `mode: ro` 를 토큰이 버리면 선언보다 넓은 권한이 나간다 (#289 리뷰)."""
    token = issue_token(
        manifest(spaces=[{"ref": "memorysets/agent-longterm@0.1.0", "as": "notes", "mode": "ro"}])
    )

    notes = token.resolve("notes")
    assert notes is not None and not notes.may_write("test-agent")
    default = issue_token(manifest(**LOCAL)).resolve("longterm")
    assert default is not None and default.may_write("test-agent"), "mode 미선언 local 은 rw 기본"
