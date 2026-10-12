"""Memory access wiring for an agent.

선언된 memory space 로부터 접근 토큰을 조립하고, 에이전트가 쓰는
``MemoryAccess`` 어댑터를 만든다.

토큰은 **runtime 이 발급한다** — 에이전트 컨테이너에 저장소 자격증명을 주지
않기 위해서다 (09 Access Enforcement). 별칭 해석은 토큰이 담당하므로
어댑터는 그것을 다시 구현하지 않는다 — 순서는 ``SCOPE_PRECEDENCE``
(local > run > group > global) 가 정본이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.memory.recall import AutoRecall, render_context, superseded_ids
from aaestro.memory.service import access_denied, build_token

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aaestro.core.manifest import AgentManifest, GroupManifest, MemorySpec
    from aaestro.memory.entry import MemoryEntry
    from aaestro.memory.recall import Recall
    from aaestro.memory.service import AccessToken, MemoryService
    from aaestro.modules.memoryset import MemoryKind, RecallSpec


DEFAULT_SCAN_LIMIT = 500
"""검색 대상 후보를 space 당 몇 개까지 읽을지 — 인덱스가 아니라 저장소 상한이다."""


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


@dataclass
class ServiceMemoryAccess:
    """The ``MemoryAccess`` implementation backed by the Memory Service.

    ``AgentContext.memory`` 에 주입되는 어댑터. 토큰을 함께 실어 호출하므로
    에이전트는 자신이 무엇에 접근할 수 있는지 따로 알 필요가 없습니다.
    """

    service: MemoryService
    token: AccessToken
    recall: Recall
    """검색은 인덱스가 담당한다 — 서비스는 저장소 경유 연산만 갖는다."""

    async def search(self, query: str, **kwargs: Any) -> list[Any]:
        """Search the spaces this agent may read.

        접근 가능한 space 로 한정해 검색합니다 — space 를 지정하지 않으면
        토큰이 허용하는 전부를 봅니다.

        선언되지 않은 space 를 요청하면 ``MEM_001`` 로 거부합니다 — 인덱스는
        서비스를 거치지 않으므로 여기서 경계를 지켜야 합니다.

        Args:
            query: The search text.
            **kwargs: ``spaces`` / ``k`` / ``kinds`` / ``tags``.

        Returns:
            Scored entries, best-first.
        """
        requested: Sequence[str] | None = kwargs.pop("spaces", None)
        # comprehension 안에서 pop 하면 첫 회차만 값을 쓰고 나머지는 기본값이 된다
        scan = kwargs.pop("scan", DEFAULT_SCAN_LIMIT)
        if requested is None:
            requested = [space.alias for space in self.token.spaces]
        # 같은 별칭을 두 번 넘겨도 한 번만 본다 — 중복 검색은 비용만 늘린다
        requested = list(dict.fromkeys(requested))

        # 별칭 해석이 곧 경계 검사다 — 선언되지 않은 별칭은 여기서 MEM_001
        resolved = [self._space_id(alias) for alias in requested]
        entries = {
            entry.entry_id: entry
            for alias in requested
            for entry in self.service.read(self.token, alias, limit=scan)
        }
        return list(self.recall.search(query, spaces=resolved, entries=entries, **kwargs))

    def _space_id(self, alias: str) -> str:
        """별칭을 실제 space id 로 해석한다 — 인덱스는 id 로 색인된다.

        해석 실패가 곧 접근 거부다: 인덱스는 서비스를 거치지 않으므로 어댑터가
        경계를 지켜야 한다.
        """
        space = self.token.resolve(alias)
        if space is None:
            raise access_denied(
                "memory space is not declared for this agent",
                agent=self.token.agent,
                space=alias,
            )
        return space.space_id

    async def recall_for_task(self, query: str, *, policy: RecallSpec) -> str:
        """Recall once at task entry and render it for the prompt.

        태스크 진입 시 1회 회상해 프롬프트에 넣을 형태로 렌더링합니다 —
        루프마다 재검색하지 않습니다 (09 Rule 7).

        Args:
            query: The task input to recall against.
            policy: The memoryset recall policy (k / min_score / budget_tokens).

        Returns:
            The rendered context, or an empty string when nothing survives the
            threshold and budget.
        """
        if not policy.auto:
            # 정책이 껐는데 500건씩 훑으면 아무 것도 쓰지 않을 데이터를 읽는다
            return ""

        # 같은 별칭이 여러 스코프에 있을 수 있다 — dedup 하지 않으면 중복 읽기와
        # 중복 히트로 예산이 낭비된다 (해석은 토큰이 하므로 결과는 같다)
        aliases = list(dict.fromkeys(space.alias for space in self.token.spaces))
        if not aliases:
            return ""

        entries = {
            entry.entry_id: entry
            for alias in aliases
            for entry in self.service.read(self.token, alias, limit=DEFAULT_SCAN_LIMIT)
        }
        auto = AutoRecall(recall=self.recall, policy=policy)
        selected = auto.for_task(
            query,
            spaces=[self._space_id(alias) for alias in aliases],
            entries=entries,
            superseded=superseded_ids(entries.values()),
        )
        return render_context(selected)

    async def append(self, space: str, **kwargs: Any) -> Any:
        """Append one entry to a writable space.

        rw 권한이 있는 space 에만 추가합니다 — 아니면 ``MEM_001``.
        """
        return self.service.append(self.token, space, kwargs["entry"])

    async def latest(self, space: str, entry_id: str) -> MemoryEntry | None:
        """정정 체인의 최신 항목 — 대체된 기억을 다시 믿지 않게 한다."""
        return self.service.latest(self.token, space, entry_id)

    async def read(
        self, space: str, *, kinds: Sequence[MemoryKind] | None = None, limit: int = 100
    ) -> tuple[MemoryEntry, ...]:
        """space 의 항목을 최신순으로 읽는다."""
        return self.service.read(self.token, space, kinds=kinds, limit=limit)


__all__ = ["ServiceMemoryAccess", "issue_token"]
