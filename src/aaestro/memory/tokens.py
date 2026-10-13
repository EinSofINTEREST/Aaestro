"""Access token assembly from declarations.

선언된 memory space 로부터 접근 토큰을 조립한다. control plane (runtime) 과
Memory Service 프로세스가 함께 쓰므로, runtime 패키지에 의존하지 않는 이 자리에 둔다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.memory.service import build_token

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aaestro.core.manifest import AgentManifest, GroupManifest, MemorySpec
    from aaestro.memory.service import AccessToken


def issue_token(
    manifest: AgentManifest,
    *,
    group: GroupManifest | None = None,
    global_spaces: MemorySpec | None = None,
    run_id: str | None = None,
    run_spaces: MemorySpec | None = None,
) -> AccessToken:
    """Assemble an access token from the declarations that apply to an agent.

    에이전트에게 적용되는 선언들로 접근 토큰을 조립합니다.

    Args:
        manifest: The agent manifest — local space 선언의 출처.
        group: The agent's group manifest, when it belongs to one.
        global_spaces: Spaces declared in ``groups/global.yaml``.
        run_id: The graph run this task belongs to; None for direct requests.
        run_spaces: Spaces declared by the graph config.

    Returns:
        The access token — 선언되지 않은 space 는 담기지 않습니다.
    """
    group_name = manifest.metadata.group
    # 그룹 space 는 **멤버에게만** 준다 — 소속이 곧 접근 경계다.
    # 엉뚱한 그룹 선언이 들어오면 조용히 무시하는 대신 거부한다: 그대로 통과시키면
    # 비멤버가 남의 그룹 권한(mode 포함)을 얻는다
    if group is not None and group.metadata.name != group_name:
        raise AaestroError(
            category=ErrorCategory.MEMORY,
            code=ErrorCode.MEM_001,
            message="group declaration does not match the agent's membership",
            agent=manifest.name,
            details={"declared": group.metadata.name, "member_of": group_name or ""},
        )
    group_declared = group.spec.memory.spaces if group is not None else ()

    # run scope 는 그래프 run 이 있을 때만 — direct 태스크가 run 의 기억을
    # 건드리면 격리가 무너진다 (09 Scope Rules 5)
    run_declared: Sequence[tuple[str, str]] = ()
    if run_id is not None and run_spaces is not None:
        run_declared = [(space.alias, run_id) for space in run_spaces.spaces]

    return build_token(
        agent=manifest.name,
        group=group_name,
        local=[(space.alias, manifest.name) for space in manifest.spec.memory.spaces],
        # 매니페스트가 ro 로 선언한 local space 를 rw 로 열지 않는다
        local_modes={space.alias: space.mode for space in manifest.spec.memory.spaces},
        group_spaces=[(space.alias, space.mode) for space in group_declared],
        global_spaces=[
            (space.alias, space.writers)
            for space in (global_spaces.spaces if global_spaces else ())
        ],
        run_spaces=run_declared,
    )


__all__ = ["issue_token"]
