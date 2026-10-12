"""Memory Service — space registration and access control.

space 접근 권한은 **선언 위치**가 정한다: manifest=local, 그래프=run,
group.yaml=group, global.yaml=global. 저장소 자격증명은 이 서비스만 보유하며
에이전트 컨테이너에 주입하지 않는다 (09 Access Enforcement).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import MemoryMode
from aaestro.memory.entry import MemoryEntry
from aaestro.modules.memoryset import MemoryScope

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from aaestro.memory.store import MemoryStore
    from aaestro.modules.memoryset import MemoryKind
    from aaestro.observability.metrics import Metrics

SCOPE_PRECEDENCE = (
    MemoryScope.LOCAL,
    MemoryScope.RUN,
    MemoryScope.GROUP,
    MemoryScope.GLOBAL,
)
"""별칭 해석 순서 — 가까운 스코프가 이긴다 (09 Attachment 3).

``run`` 은 local 다음이다: 해당 run 에 한정된 기억이 그룹/전역 공용 지식보다
이 태스크에 가깝다. 모든 스코프가 여기 있어야 한다 — 빠진 스코프의 별칭은
해석 단계에서 터진다."""

log = structlog.get_logger(__name__)


def access_denied(message: str, *, agent: str, space: str, **details: str) -> AaestroError:
    """접근 거부를 구조화 에러로 만든다."""
    return AaestroError(
        category=ErrorCategory.MEMORY,
        code=ErrorCode.MEM_001,
        message=message,
        agent=agent,
        details={"memory_space": space, **details},
    )


@dataclass(frozen=True)
class MemorySpace:
    """One declared memory space.

    선언된 space 하나. ``scope`` 는 선언 위치에서 나오며, ``owner`` 는
    scope 에 따라 에이전트 이름(local) / 그룹 이름(group) / run id(run) 다.
    """

    alias: str
    scope: MemoryScope
    owner: str
    mode: MemoryMode = MemoryMode.RW
    writers: tuple[str, ...] = ()
    """global scope 에서 write 가 허용된 에이전트 — 미지정 시 read-only."""
    name: str | None = None
    """space 의 선언 이름 — 요청한 이름(``alias``)이 space id 그대로일 때만 다르다 (#279).

    선언하지 않고 **부여받은** space 는 에이전트에게 별칭이 없어 space id 로 부른다. 그때
    ``alias`` 는 요청한 문자열(``group:research:knowledge``), ``name`` 은 선언 이름이다."""

    @property
    def space_id(self) -> str:
        """저장소가 쓰는 실제 식별자 — ``(scope, owner, name)`` 으로 유일하다.

        같은 별칭이 스코프마다 달라야 서로의 기억을 덮어쓰지 않는다.
        """
        return f"{self.scope}:{self.owner}:{self.name or self.alias}"

    def may_write(self, agent: str) -> bool:
        """이 에이전트가 쓸 수 있는지."""
        if self.scope is MemoryScope.GLOBAL:
            # 전역은 기본 read-only — writers 에 명시된 에이전트만 쓴다
            return agent in self.writers
        return self.mode is MemoryMode.RW


@dataclass(frozen=True)
class AccessToken:
    """What one agent may touch.

    runtime 이 발급하는 per-agent 접근 토큰. 접근 가능 space 와 mode 를 담으며,
    그룹 이동이나 mode/writers 변경 시 재발급된다.
    """

    agent: str
    group: str | None
    spaces: tuple[MemorySpace, ...]

    def resolve(self, alias: str) -> MemorySpace | None:
        """Resolve an alias by scope precedence.

        별칭을 스코프 우선순위로 해석합니다 —
        **local > run > group > global** (``SCOPE_PRECEDENCE`` 순).
        같은 별칭이 여러 스코프에 있으면 가까운 쪽이 이깁니다.

        Args:
            alias: The agent-facing space alias.

        Returns:
            The winning space, or None if the alias is not declared.
        """
        candidates = [s for s in self.spaces if s.alias == alias]
        if not candidates:
            return None
        return min(candidates, key=lambda s: SCOPE_PRECEDENCE.index(s.scope))


@dataclass
class MemoryService:
    """The framework-side memory gateway.

    프레임워크 측 메모리 게이트웨이. 에이전트는 이 서비스를 통해서만 저장소에
    닿는다 — DB 자격증명은 컨테이너로 나가지 않는다.
    """

    store: MemoryStore
    metrics: Metrics | None = None
    _audit: list[dict[str, str]] = field(default_factory=list, init=False)

    def _authorize(self, token: AccessToken, alias: str, *, write: bool) -> MemorySpace:
        """접근을 검사하고 대상 space 를 돌려준다."""
        space = token.resolve(alias)
        if space is None:
            # 선언되지 않은 space 는 존재조차 알려주지 않는다
            raise access_denied(
                "memory space is not declared for this agent",
                agent=token.agent,
                space=alias,
            )
        if write and not space.may_write(token.agent):
            raise access_denied(
                "memory space is not writable by this agent",
                agent=token.agent,
                space=alias,
                scope=str(space.scope),
            )
        return space

    def _record(self, *, agent: str, group: str | None, space: str, op: str, status: str) -> None:
        """감사 로그 — 모든 접근이 추적 가능해야 한다."""
        entry = {
            "agent": agent,
            "group": group or "",
            "memory_space": space,
            "op": op,
            "status": status,
        }
        self._audit.append(entry)
        log.info("memory access", **entry)
        if self.metrics is not None:
            self.metrics.counter("aaestro_memory_operations_total").labels(
                space=space, op=op, status=status
            ).inc()

    @property
    def audit_log(self) -> tuple[dict[str, str], ...]:
        """기록된 접근 감사 로그."""
        return tuple(self._audit)

    def append(
        self,
        token: AccessToken,
        alias: str,
        entry: MemoryEntry,
    ) -> MemoryEntry:
        """Append one entry to a declared space.

        선언된 space 에 항목을 추가합니다.

        Args:
            token: The agent's access token.
            alias: The space alias to write to.
            entry: The entry — its ``space`` is rewritten to the resolved id.

        Returns:
            The stored entry.

        Raises:
            AaestroError: MEMORY/``MEM_001`` if the space is undeclared or
                read-only for this agent, ``MEM_002`` on storage failure.
        """
        try:
            space = self._authorize(token, alias, write=True)
        except AaestroError:
            self._record(
                agent=token.agent, group=token.group, space=alias, op="append", status="denied"
            )
            raise

        stored = self.store.append(entry.model_copy(update={"space": space.space_id}))
        self._record(agent=token.agent, group=token.group, space=alias, op="append", status="ok")
        return stored

    def read(
        self,
        token: AccessToken,
        alias: str,
        *,
        kinds: Sequence[MemoryKind] | None = None,
        limit: int = 100,
    ) -> tuple[MemoryEntry, ...]:
        """Read entries from a declared space.

        선언된 space 의 항목을 읽습니다 — 검색은 space 경계를 넘지 않습니다.

        Args:
            token: The agent's access token.
            alias: The space alias to read.
            kinds: Optional kind filter.
            limit: Maximum entries.

        Returns:
            Entries, newest first.

        Raises:
            AaestroError: MEMORY/``MEM_001`` if the space is not declared.
        """
        try:
            space = self._authorize(token, alias, write=False)
        except AaestroError:
            self._record(
                agent=token.agent, group=token.group, space=alias, op="read", status="denied"
            )
            raise

        entries = self.store.list_space(space.space_id, kinds=kinds, limit=limit)
        self._record(agent=token.agent, group=token.group, space=alias, op="read", status="ok")
        return entries

    def latest(self, token: AccessToken, alias: str, entry_id: str) -> MemoryEntry | None:
        """Follow a correction chain to its newest entry.

        정정 체인의 최신 항목을 찾습니다 — 대체된 기억은 주입 대상이 아닙니다.

        조회 결과가 요청한 space 에 속하는지 반드시 확인합니다. entry_id 만으로
        체인을 따라가면, id 를 아는 것만으로 다른 space 의 기억을 읽을 수 있어
        ACL 이 무의미해집니다.

        Args:
            token: The agent's access token.
            alias: The space alias the entry must belong to.
            entry_id: Any entry id in the chain.

        Returns:
            The newest entry in the chain, or None if the id is unknown or
            belongs to another space.

        Raises:
            AaestroError: MEMORY/``MEM_001`` if the space is not declared.
        """
        try:
            space = self._authorize(token, alias, write=False)
        except AaestroError:
            self._record(
                agent=token.agent, group=token.group, space=alias, op="latest", status="denied"
            )
            raise

        entry = self.store.latest_of_chain(entry_id)
        if entry is not None and entry.space != space.space_id:
            # 다른 space 의 항목은 존재조차 알리지 않는다
            self._record(
                agent=token.agent, group=token.group, space=alias, op="latest", status="denied"
            )
            return None

        self._record(agent=token.agent, group=token.group, space=alias, op="latest", status="ok")
        return entry


def build_token(
    *,
    agent: str,
    group: str | None,
    local: Sequence[tuple[str, str]] = (),
    group_spaces: Sequence[tuple[str, MemoryMode]] = (),
    global_spaces: Sequence[tuple[str, tuple[str, ...]]] = (),
    run_spaces: Sequence[tuple[str, str]] = (),
    local_modes: Mapping[str, MemoryMode] | None = None,
) -> AccessToken:
    """Issue an access token from the declarations that apply to an agent.

    에이전트에게 적용되는 선언들로부터 접근 토큰을 발급합니다.
    **run scope 는 direct 태스크에 부여하지 않습니다** — 그래프 run 과 무관한
    태스크가 run 의 기억을 건드리면 격리가 무너집니다.

    Args:
        agent: The owning agent.
        group: The agent's group, or None if global-only.
        local: ``(alias, owner)`` pairs from the agent manifest.
        group_spaces: ``(alias, mode)`` pairs from ``group.yaml``.
        global_spaces: ``(alias, writers)`` pairs from ``groups/global.yaml``.
        run_spaces: ``(alias, run_id)`` pairs — omit for direct tasks.
        local_modes: 매니페스트가 선언한 local space 의 mode — 없으면 rw.

    Returns:
        The access token.
    """
    modes = local_modes or {}
    spaces: list[MemorySpace] = [
        MemorySpace(
            alias=alias,
            scope=MemoryScope.LOCAL,
            owner=owner,
            mode=modes.get(alias, MemoryMode.RW),
        )
        for alias, owner in local
    ]
    if group is not None:
        spaces.extend(
            MemorySpace(alias=alias, scope=MemoryScope.GROUP, owner=group, mode=mode)
            for alias, mode in group_spaces
        )
    spaces.extend(
        MemorySpace(alias=alias, scope=MemoryScope.GLOBAL, owner="global", writers=tuple(writers))
        for alias, writers in global_spaces
    )
    spaces.extend(
        MemorySpace(alias=alias, scope=MemoryScope.RUN, owner=run_id)
        for alias, run_id in run_spaces
    )
    return AccessToken(agent=agent, group=group, spaces=tuple(spaces))


__all__ = [
    "SCOPE_PRECEDENCE",
    "AccessToken",
    "MemoryService",
    "MemorySpace",
    "access_denied",
    "build_token",
]
