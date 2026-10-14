"""Memory Service — space registration and access control.

space 접근 권한은 **선언 위치**가 정한다: manifest=local, 그래프=run,
group.yaml=group, global.yaml=global. 저장소 자격증명은 이 서비스만 보유하며
에이전트 컨테이너에 주입하지 않는다 (09 Access Enforcement).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import structlog

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import MemoryMode
from aaestro.modules.memoryset import MemoryScope

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from aaestro.memory.entry import MemoryDraft, MemoryEntry
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

TOKEN_DECISION_SOURCE: Final = "token"  # noqa: S105 — 판정 출처의 이름이지 값이 아니다
"""판정 출처 — 레지스트리에 묻지 않고, 기동 시 발급한 토큰의 선언으로 정한 접근이다.

05 의 ``decision_source`` 어휘에 있다."""

UNDECLARED_SPACE_LABEL: Final = "undeclared"
"""space 로 해석되지 않은 접근의 메트릭 ``space`` 라벨.

호출자가 고른 별칭을 라벨로 쓰면 시계열 수를 호출자가 정한다 — 해석되지 않은 것은 한 값으로 센다."""

log = structlog.get_logger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def access_denied(message: str, *, agent: str, space: str, **details: str) -> AaestroError:
    """접근 거부를 구조화 에러로 만든다."""
    return AaestroError(
        category=ErrorCategory.MEMORY,
        code=ErrorCode.MEM_001,
        message=message,
        agent=agent,
        details={"memory_space": space, **details},
    )


def _run_of(token: AccessToken, claimed: str | None, alias: str) -> str | None:
    """기억의 ``source.run_id`` — 토큰이 아는 run 이다. 호출자가 실은 값은 그것과 같을 때만 받는다.

    run 을 모르는 토큰 (direct 태스크) 은 run 을 실을 수 없다. 거절은 다른 run 과 없는 run 을
    가르지 않고, 값도 싣지 않는다 — run 은 다른 run 의 space 를 고르는 열쇠다.

    Raises:
        AaestroError: VALIDATION/``VAL_002`` if the claim differs from the token's run.
    """
    if claimed is None or claimed == token.run_id:
        return token.run_id
    raise AaestroError(
        category=ErrorCategory.VALIDATION,
        code=ErrorCode.VAL_002,
        message="source run_id must be the run this token belongs to",
        agent=token.agent,
        details={"space": alias, "field": "source.run_id"},
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
    decision_source: str = TOKEN_DECISION_SOURCE
    """이 space 에 닿아도 된다는 판정이 어디서 왔는지 — 레지스트리 입구가 판정의 출처로 채운다."""

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
    decision_source: str = TOKEN_DECISION_SOURCE
    """space 별 판정이 없는 접근 (미선언 별칭) 의 출처.

    레지스트리 입구는 신원 확인의 출처를 싣는다."""
    refusals: tuple[tuple[str, str], ...] = ()
    """레지스트리가 거부한 ``(별칭, 판정 출처)``.

    거부된 space 는 ``spaces`` 에 없으므로 감사 줄이 출처를 잃지 않게 따로 싣는다."""
    run_id: str | None = None
    """이 토큰이 속한 graph run — direct 태스크 · 에이전트 단위 토큰은 None.

    기억의 출처 ``source.run_id`` 는 호출자가 아니라 이 값이 정한다 (09 Entry Rules 4). space 를
    좁혀도 (``narrow``) 그대로 남는다 — run space 가 빠진 토큰도 run 을 안다."""

    def source_of(self, alias: str) -> str:
        """이 별칭에 대한 접근의 판정 출처 — 감사 줄에 싣는다 (09 Access Enforcement 6)."""
        space = self.resolve(alias)
        if space is not None:
            return space.decision_source
        return dict(self.refusals).get(alias, self.decision_source)

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
    clock: Callable[[], datetime] = _utc_now
    """항목의 ``created_at`` 을 정하는 시계 — 출처의 시각은 호출자가 아니라 서비스가 정한다."""
    _audit: list[dict[str, str]] = field(default_factory=list, init=False)
    _last_issued: datetime | None = field(default=None, init=False, repr=False)

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

    def _record(
        self,
        token: AccessToken,
        alias: str,
        *,
        op: str,
        status: str,
        count_metric: bool = True,
    ) -> None:
        """감사 로그 — 모든 접근이 추적 가능해야 한다.

        로그의 ``memory_space`` 는 에이전트가 부른 별칭이고, 메트릭의 ``space`` 는 space id 다 —
        별칭은 에이전트 관점의 이름이라 서로 다른 space 가 한 시계열로 합쳐진다.

        ``count_metric=False`` 는 연산 카운터를 다른 곳이 이미 세는 경우 (검색은 ``Recall`` 이 센다)
        이중 집계를 피한다.
        """
        entry = {
            "agent": token.agent,
            "group": token.group or "",
            "memory_space": alias,
            "op": op,
            "status": status,
            "decision_source": token.source_of(alias),
        }
        self._audit.append(entry)
        log.info("memory access", **entry)
        if self.metrics is not None and count_metric:
            space = token.resolve(alias)
            self.metrics.counter("aaestro_memory_operations_total").labels(
                space=space.space_id if space is not None else UNDECLARED_SPACE_LABEL,
                op=op,
                status=status,
            ).inc()

    def _admitted(self, token: AccessToken, alias: str, *, op: str, write: bool) -> MemorySpace:
        """접근을 검사한다 — 거부도 감사 줄로 남긴다."""
        try:
            return self._authorize(token, alias, write=write)
        except AaestroError:
            self._record(token, alias, op=op, status="denied")
            raise

    @contextmanager
    def _failing(self, token: AccessToken, alias: str, *, op: str) -> Iterator[None]:
        """허용된 접근이 실패로 끝나면 감사 줄을 남긴다 — 저장소 실패와 검증 실패 모두.

        권한 거부가 아니다 — ``denied`` 는 memory ACL 전용 어휘다 (05).
        """
        try:
            yield
        except AaestroError:
            self._record(token, alias, op=op, status="failed")
            raise

    def searchable(
        self, token: AccessToken, aliases: Sequence[str], *, op: str = "search"
    ) -> list[str]:
        """Resolve the aliases a search names into space ids — the boundary check for search.

        검색은 인덱스를 직접 쓰므로 ``read`` 의 검사를 지나지 않는다. 별칭 해석이 곧 경계다:
        해석되지 않는 별칭 (미선언, 레지스트리가 거부한 space) 은 별칭마다 ``denied`` 한 줄을
        남기고 거부한다 — 형제인 ``read`` 와 같은 흔적이다.

        Args:
            token: The agent's access token, already narrowed by the gate.
            aliases: The aliases the search names.
            op: ``search`` or ``recall``.

        Returns:
            The space ids, in the order of ``aliases``.

        Raises:
            AaestroError: MEMORY/``MEM_001`` if any alias does not resolve.
        """
        spaces = [token.resolve(alias) for alias in aliases]
        refused = [alias for alias, space in zip(aliases, spaces, strict=True) if space is None]
        if refused:
            self.record_search(token, refused, op=op, status="denied")
            raise access_denied(
                "memory space is not declared for this agent", agent=token.agent, space=refused[0]
            )
        return [space.space_id for space in spaces if space is not None]

    def record_search(
        self,
        token: AccessToken,
        aliases: Sequence[str],
        *,
        op: str = "search",
        status: str = "ok",
    ) -> None:
        """검색의 감사 로그를 space 마다 한 줄 남긴다 (09 Access Enforcement 6).

        검색은 저장소 조회가 id 점조회라 ``read`` 를 거치지 않는다 — 여기서 남기지 않으면
        검색이 감사에서 사라진다. 실패한 검색은 호출 측이 ``status="failed"`` 로 남긴다.

        연산 카운터는 인덱스에 닿은 검색이면 ``Recall`` 이 센다. 거부는 인덱스에 닿지 않으므로
        여기서 센다 — 아니면 검색으로 경계를 두드리는 시도가 메트릭에서 빠진다.
        """
        for alias in aliases:
            self._record(token, alias, op=op, status=status, count_metric=status == "denied")

    @property
    def audit_log(self) -> tuple[dict[str, str], ...]:
        """기록된 접근 감사 로그."""
        return tuple(self._audit)

    def append(
        self,
        token: AccessToken,
        alias: str,
        draft: MemoryDraft,
    ) -> MemoryEntry:
        """Append one entry to a declared space — the service issues its id and provenance.

        선언된 space 에 항목을 추가합니다. ``entry_id`` · ``created_at`` · 출처의 ``agent`` 는
        서비스가 정합니다 (#456) — 호출자는 내용과 자기 문맥 (run · task · node) 만 싣습니다.
        id 를 호출자가 고르면 전역 기본 키의 충돌로 다른 space 의 항목 존재가 드러납니다.

        Args:
            token: The agent's access token.
            alias: The space alias to write to.
            draft: What the caller says about the entry.

        Returns:
            The stored entry — 정정 (``supersedes``) 은 여기 담긴 id 로 합니다.

        Raises:
            AaestroError: MEMORY/``MEM_001`` if the space is undeclared or
                read-only for this agent, ``MEM_002`` on storage failure,
                ``MEM_004`` if the correction target could not be looked up,
                VALIDATION/``VAL_002`` if ``supersedes`` points outside the space.
        """
        space = self._admitted(token, alias, op="append", write=True)
        with self._failing(token, alias, op="append"):
            run_id = _run_of(token, draft.source.run_id, alias)
            self._require_same_space_correction(token, alias, draft.supersedes, space.space_id)
            entry = draft.issue(
                space=space.space_id, agent=token.agent, run_id=run_id, created_at=self._now()
            )
            stored = self.store.append(entry)
        self._record(token, alias, op="append", status="ok")
        return stored

    def record_failure(self, token: AccessToken, alias: str, *, op: str) -> None:
        """Audit an operation refused before it reached the store — e.g. a malformed draft.

        저장소에 닿기 전에 거절된 연산의 감사 줄 (``failed``) — 신원이 있는 경로 (in-process
        어댑터) 의 형식 거절이 감사에서 빠지지 않게 한다.
        """
        self._record(token, alias, op=op, status="failed")

    def _now(self) -> datetime:
        """발급할 ``created_at`` — UTC 로 정규화하고, 직전 발급보다 뒤로 가지 않는다.

        저장소는 ``created_at`` 의 ISO 문자열로 정렬한다: 오프셋이 섞이면 문자열 순서가 시각 순서와
        어긋나고, 시계가 역행하면 나중에 쓴 정정이 ``latest`` 에서 밀린다. 같은 순간은 저장 순서
        (rowid) 가 가른다.

        Raises:
            AaestroError: MEMORY/``MEM_002`` if the clock returns a naive timestamp.
        """
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise AaestroError(
                category=ErrorCategory.MEMORY,
                code=ErrorCode.MEM_002,
                message="memory service clock returned a naive timestamp",
            )
        now = now.astimezone(UTC)
        if self._last_issued is not None and now < self._last_issued:
            now = self._last_issued
        self._last_issued = now
        return now

    def _require_same_space_correction(
        self, token: AccessToken, alias: str, supersedes: str | None, space_id: str
    ) -> None:
        """정정은 이 space 에서 지금 읽을 수 있는 항목만 가리킬 수 있다.

        읽기만 되는 space 의 id 를 ``supersedes`` 로 달면 그 space 의 기억을 숨길 수 있다.
        대상이 없는 경우와 다른 space 에 있는 경우는 같은 거절이다 — 다르게 답하면 호출자가
        다른 space 의 id 존재 여부를 알아낼 수 있다. 대상 id 와 다른 space 이름도 싣지 않는다.
        """
        if supersedes is None:
            return
        # 대상의 space 만 본다 — 행을 만들면 남의 space 의 손상된 행이 다른 답을 낸다
        if self.store.space_of(supersedes) == space_id:
            return
        raise AaestroError(
            category=ErrorCategory.VALIDATION,
            code=ErrorCode.VAL_002,
            message="supersedes must reference an entry in the same space",
            agent=token.agent,
            details={"space": alias},
        )

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
            AaestroError: MEMORY/``MEM_001`` if the space is not declared,
                ``MEM_004`` if the store could not be queried.
        """
        space = self._admitted(token, alias, op="read", write=False)
        with self._failing(token, alias, op="read"):
            entries = self.store.list_space(space.space_id, kinds=kinds, limit=limit)
        self._record(token, alias, op="read", status="ok")
        return entries

    def latest(self, token: AccessToken, alias: str, entry_id: str) -> MemoryEntry | None:
        """Follow a correction chain to its newest entry.

        정정 체인의 최신 항목을 찾습니다 — 대체된 기억은 주입 대상이 아닙니다.

        체인은 요청한 space 안에서만 따라갑니다 (저장소 계약). entry_id 만으로 따라가면
        id 를 아는 것만으로 다른 space 의 기억을 읽거나, 다른 space 에서 낸 정정으로 이 space 의
        기억을 가릴 수 있습니다.

        Args:
            token: The agent's access token.
            alias: The space alias the entry must belong to.
            entry_id: Any entry id in the chain.

        Returns:
            The newest entry in the chain, or None if the id is unknown or
            belongs to another space — 두 경우의 답은 같습니다.

        Raises:
            AaestroError: MEMORY/``MEM_001`` if the space is not declared,
                ``MEM_004`` if the store could not be queried.
        """
        space = self._admitted(token, alias, op="latest", write=False)
        with self._failing(token, alias, op="latest"):
            # 시작 항목의 space 만 묻는다 — 남의 행을 만들면 그 내용 (손상 여부) 이 답을 가른다
            origin = self.store.space_of(entry_id)
            foreign = origin is not None and origin != space.space_id
            entry = None if foreign else self.store.latest_of_chain(space.space_id, entry_id)
        # 다른 space 의 id 로 물은 것은 경계를 두드린 것이다 — 응답은 없는 id 와 같고
        # 감사에만 남는다
        self._record(token, alias, op="latest", status="denied" if foreign else "ok")
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
    run_id: str | None = None,
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
        run_id: 이 토큰이 속한 run — 없으면 ``run_spaces`` 의 run 이 하나일 때 그것.
            run space 가 둘 이상의 run 에 걸치면 ValueError.

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
    runs = {owner for _alias, owner in run_spaces}
    if run_id is None and len(runs) == 1:
        run_id = next(iter(runs))
    # 한 토큰은 한 run 에 속한다 — run 이 둘 이상이면 run_id 가 없어도 출처의 run 이 모호해진다
    if len(runs) > 1 or (run_id is not None and runs - {run_id}):
        raise ValueError("run spaces must belong to the token's run")
    return AccessToken(agent=agent, group=group, spaces=tuple(spaces), run_id=run_id)


__all__ = [
    "SCOPE_PRECEDENCE",
    "TOKEN_DECISION_SOURCE",
    "UNDECLARED_SPACE_LABEL",
    "AccessToken",
    "MemoryService",
    "MemorySpace",
    "access_denied",
    "build_token",
]
