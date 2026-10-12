"""The access registry — identities, rules, ceilings, and decisions.

01 Access Control 의 판정 지점. 판정 순서:

1. 운영자의 **회수**가 해당하면 거부 — 선언도 부여도 이기지 못한다
2. **선언**(기본 권한)이 허용하면 허용
3. 권한 에이전트의 유효한 **부여**가 해당하면 허용
4. 그 밖에는 거부

넓히는 것은 권한 에이전트만, 그리고 **확장 상한** 안에서만 한다. 상한은 여기서 결정적으로
강제한다 — 확장 요청은 신뢰할 수 없는 입력이다 (03/09).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import secrets
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from aaestro.access.model import (
    DECLARATION,
    OPERATOR,
    Decision,
    Effect,
    Mode,
    Outcome,
    ResourceKind,
    Rule,
    mode_problem,
)
from aaestro.access.store import Identity, Ticket
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import RESERVED_GLOBAL_GROUP, AccessCeiling

if TYPE_CHECKING:
    from aaestro.access.store import AccessStore
    from aaestro.catalog import Catalog
    from aaestro.observability.metrics import Metrics

log = structlog.get_logger(__name__)

ACCESS_CREDENTIAL_ENV = "AAESTRO_ACCESS_CREDENTIAL"  # noqa: S105 — 키 이름이지 값이 아니다
"""에이전트 컨테이너에 주입되는 신원 자격의 env 이름."""

DEFAULT_OUTCOME_SOURCE = "default"
"""어느 규칙에도 해당하지 않아 거부된 판정의 출처."""


@runtime_checkable
class Baseline(Protocol):
    """What declarations allow for one resource kind.

    선언된 기본 권한. 종류마다 선언이 사는 곳이 다르다 — 메모리는 매니페스트·그룹, A2A 는 그래프의
    ``connections`` — 그래서 종류별로 꽂는다.
    """

    def allows(
        self, agent: str, target: str, mode: Mode | None, identity: Identity | None = None
    ) -> bool:
        """``identity`` 는 요청을 보낸 **그 신원** — 배포마다 다른 선언(A2A 의 그래프)을 고른다."""
        ...


DECLARATIONS_POLL_S = 2.0
RECENT_DENIALS = 50
"""에이전트마다 기억하는 최근 거부 수 — 화면이 원인을 보는 데 충분하고 메모리가 늘지 않는다."""


@dataclass(frozen=True)
class Denial:
    """One refused decision, as the operator sees it."""

    kind: ResourceKind
    target: str
    mode: Mode | None
    decided_by: str
    at: float


TICKET_TTL_S = 300.0
"""A2A 호출 표의 수명 — 호출자는 만료 전까지 같은 피호출자에게 재사용한다."""

INVALID_TICKET = "invalid-ticket"
"""표가 없거나, 만료됐거나, 다른 피호출자의 것이거나, 발급받은 신원이 폐기된 판정의 출처."""


def declaration_fingerprint(catalog: Catalog) -> tuple[tuple[str, int, int, int], ...]:
    """선언 판정과 상한이 읽는 파일의 (경로, inode, 크기, mtime) — 원자적 교체는 inode 가 바뀐다."""
    paths = sorted(
        [*catalog.roots.agents.glob("*/manifest.yaml"), *catalog.roots.groups.glob("*.yaml")]
    )
    found = []
    for path in paths:
        with contextlib.suppress(FileNotFoundError):
            stat = path.stat()
            found.append((str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns))
    return tuple(found)


def credential_hash(credential: str) -> str:
    """저장하고 대조하는 것은 해시다 — 저장 파일이 새도 사칭할 수 없다."""
    return hashlib.sha256(credential.encode()).hexdigest()


def _refused(message: str, **details: Any) -> AaestroError:
    return AaestroError(
        category=ErrorCategory.FORBIDDEN,
        code=ErrorCode.ACC_003,
        message=message,
        details=details,
    )


def _check_mode(kind: ResourceKind, mode: Mode | None, *, memory_needs_mode: bool) -> None:
    problem = mode_problem(kind, mode, memory_needs_mode=memory_needs_mode)
    if problem is not None:
        raise AaestroError(
            category=ErrorCategory.VALIDATION,
            code=ErrorCode.VAL_002,
            message=problem,
            details={"resource": kind.value, "mode": mode.value if mode else None},
        )


def _unknown_identity() -> AaestroError:
    return AaestroError(
        category=ErrorCategory.FORBIDDEN,
        code=ErrorCode.ACC_001,
        message="not a recognised agent credential",
    )


@dataclass
class AccessRegistry:
    """Decides who may do what, and records every change.

    Attributes:
        store: 신원과 기록 — 재시작을 넘는다.
        catalog: 확장 상한(그룹·global 선언)과 에이전트 소속의 출처.
        stewards: 권한 에이전트로 지정된 에이전트 이름 — **운영자 설정**이다. 에이전트가 스스로
            자신을 지정할 방법은 없다.
        baselines: 종류별 선언 기본 권한. 없는 종류는 선언이 아무것도 허용하지 않는다.
        clock: epoch 초 — 테스트는 주입한다 (06).
    """

    store: AccessStore
    catalog: Catalog
    stewards: frozenset[str] = frozenset()
    baselines: Mapping[ResourceKind, Baseline] = field(default_factory=dict)
    clock: Callable[[], float] = time.time
    metrics: Metrics | None = None
    declarations_poll_s: float = DECLARATIONS_POLL_S
    """선언 파일 변경을 몇 초마다 보는가 — 변경 알림 긴 폴링도 이 간격으로 깨어 확인한다."""
    _changed: asyncio.Event | None = field(default=None, init=False, repr=False)
    _declarations: tuple[tuple[str, int, int, int], ...] | None = field(
        default=None, init=False, repr=False
    )
    _declarations_seen_at: float = field(default=float("-inf"), init=False, repr=False)
    _denials: dict[str, deque[Denial]] = field(default_factory=dict, init=False, repr=False)
    """에이전트별 최근 거부 — 운영자 화면용. 프로세스 메모리에만 두고 재시작하면 비운다."""

    # --- 신원 ---------------------------------------------------------------

    def issue_identity(self, agent: str, deployment_id: str, *, graph: str = "") -> str:
        """Mint a credential for one agent in one deployment and return it once.

        값은 여기서 한 번만 돌려준다 — 레지스트리는 해시만 저장한다. ``graph`` 는 A2A 선언 판정이
        이 에이전트의 ``connections`` 를 찾는 곳이다.
        """
        credential = secrets.token_urlsafe(32)
        self.store.put_identity(
            Identity(
                credential_hash=credential_hash(credential),
                agent=agent,
                deployment_id=deployment_id,
                issued_at=self.clock(),
                graph=graph,
            )
        )
        log.info("agent identity issued", agent=agent, deployment_id=deployment_id)
        return credential

    def revoke_deployment(self, deployment_id: str) -> int:
        """배포를 해체하면 그 신원은 더 이상 아무 강제 지점도 통과하지 못한다."""
        revoked = self.store.revoke_identities(deployment_id, self.clock())
        if revoked:
            self._wake_waiters()
            log.info("agent identities revoked", deployment_id=deployment_id, count=revoked)
        return revoked

    def identify(self, credential: str) -> str:
        """The agent behind a credential.

        Raises:
            AaestroError: FORBIDDEN/``ACC_001`` if the credential is unknown or revoked.
        """
        return self.identity_of(credential).agent

    def identity_of(self, credential: str) -> Identity:
        """The live identity record behind a credential — agent, deployment, graph.

        Raises:
            AaestroError: FORBIDDEN/``ACC_001`` if the credential is unknown or revoked.
        """
        found = self.store.identity(credential_hash(credential)) if credential else None
        if found is None or found.revoked_at is not None:
            raise _unknown_identity()
        return found

    # --- 판정 ---------------------------------------------------------------

    def decide(
        self,
        agent: str,
        kind: ResourceKind,
        target: str,
        mode: Mode | None = None,
        *,
        identity: Identity | None = None,
    ) -> Decision:
        """Decide one request — revocation, then declaration, then grant.

        ``identity`` 가 있으면 선언 판정이 그 신원의 배포를 본다 — 같은 이름의 다른 배포가 선언한
        권한을 이 요청이 빌려 쓰지 않는다.

        Raises:
            AaestroError: VALIDATION/``VAL_002`` if the mode does not fit the kind.
        """
        _check_mode(kind, mode, memory_needs_mode=True)
        self._sync_declarations()
        now = self.clock()
        active = [
            r for r in self.store.rules(agent) if r.active(now) and r.covers(kind, target, mode)
        ]
        version = self.store.version()
        valid_until = min((r.expires_at for r in active if r.expires_at is not None), default=None)

        def answer(outcome: Outcome, decided_by: str) -> Decision:
            self._count_decision(kind, outcome)
            if outcome is Outcome.DENY:
                recent = self._denials.setdefault(agent, deque(maxlen=RECENT_DENIALS))
                recent.appendleft(Denial(kind, target, mode, decided_by, now))
            return Decision(agent, kind, target, mode, outcome, decided_by, version, valid_until)

        denial = next((r for r in active if r.effect is Effect.DENY), None)
        if denial is not None:
            return answer(Outcome.DENY, denial.decided_by)
        baseline = self.baselines.get(kind)
        if baseline is not None and baseline.allows(agent, target, mode, identity):
            return answer(Outcome.ALLOW, DECLARATION)
        grant = next((r for r in active if r.effect is Effect.ALLOW), None)
        if grant is not None:
            return answer(Outcome.ALLOW, grant.decided_by)
        return answer(Outcome.DENY, DEFAULT_OUTCOME_SOURCE)

    # --- A2A 호출 표 ------------------------------------------------------------

    def issue_ticket(self, caller_credential: str, callee: str) -> tuple[str, float]:
        """Mint a ticket the caller presents to one callee.

        표는 **신원 증명**일 뿐 허가가 아니다 — 허가는 피호출자가 표를 들고 물을 때 판정한다.
        그래서 연결이 회수돼도 표 발급은 되고, 피호출자가 거부한다.

        Raises:
            AaestroError: FORBIDDEN/``ACC_001`` unknown caller, NOT_FOUND/``NF_001`` unknown callee.
        """
        caller = self.identify(caller_credential)
        self.catalog.agent(callee)
        ticket = secrets.token_urlsafe(32)
        now = self.clock()
        self.store.put_ticket(
            Ticket(
                ticket_hash=credential_hash(ticket),
                caller_hash=credential_hash(caller_credential),
                caller=caller,
                callee=callee,
                issued_at=now,
                expires_at=now + TICKET_TTL_S,
            )
        )
        return ticket, now + TICKET_TTL_S

    def verify_ticket(self, callee_credential: str, ticket: str) -> Decision:
        """Decide an inbound A2A call for the callee holding ``callee_credential``.

        표가 이 피호출자의 것이 아니면 거부한다 — 표를 받은 피호출자가 그것을 들고 다른 에이전트를
        호출자 행세로 부르지 못하게 한다.

        Raises:
            AaestroError: FORBIDDEN/``ACC_001`` if the callee credential is unknown.
        """
        callee = self.identify(callee_credential)
        found = self.store.ticket(credential_hash(ticket)) if ticket else None
        now = self.clock()
        caller_identity = self.store.identity(found.caller_hash) if found else None
        if (
            found is None
            or found.callee != callee
            or now >= found.expires_at
            or caller_identity is None
            or caller_identity.revoked_at is not None
        ):
            self._count_decision(ResourceKind.A2A, Outcome.DENY)
            return Decision(
                agent="",
                kind=ResourceKind.A2A,
                target=callee,
                mode=None,
                outcome=Outcome.DENY,
                decided_by=INVALID_TICKET,
                version=self.store.version(),
            )
        # 판정은 표를 발급받은 **그 신원**의 배포 선언으로 한다
        decision = self.decide(found.caller, ResourceKind.A2A, callee, identity=caller_identity)
        until = found.expires_at
        if decision.valid_until is not None:
            until = min(until, decision.valid_until)
        return replace(decision, valid_until=until)

    # --- 운영자: 좁히기 --------------------------------------------------------

    def revoke(
        self,
        agent: str,
        kind: ResourceKind,
        target: str,
        *,
        reason: str,
        mode: Mode | None = None,
        expires_in_s: float | None = None,
    ) -> Rule:
        """Narrow an agent's permission — declared ones included (operator only).

        ``mode=rw`` 로 memory 를 회수하면 쓰기만 막고 읽기는 남긴다 (rw → ro 강등).
        """
        _check_mode(kind, mode, memory_needs_mode=False)
        self.catalog.agent(agent)  # 없는 에이전트에 대한 기록은 만들지 않는다
        now = self.clock()
        rule = Rule(
            rule_id=f"rule-{uuid.uuid4().hex[:16]}",
            agent=agent,
            kind=kind,
            target=target,
            mode=mode,
            effect=Effect.DENY,
            decided_by=OPERATOR,
            reason=reason,
            created_at=now,
            expires_at=None if expires_in_s is None else now + expires_in_s,
        )
        return self._record(rule, op="revoke")

    def lift(self, rule_id: str) -> Rule:
        """End a rule early — restores a revocation, or cuts a grant short (operator only).

        Raises:
            AaestroError: NOT_FOUND/``NF_001`` if no such rule.
        """
        found = self.store.rule(rule_id)
        if found is None:
            raise AaestroError(
                category=ErrorCategory.NOT_FOUND,
                code=ErrorCode.NF_001,
                message="unknown access rule",
                details={"rule_id": rule_id},
            )
        if found.lifted_at is not None:
            return found
        return self._record(replace(found, lifted_at=self.clock()), op="lift")

    # --- 권한 에이전트: 넓히기 ---------------------------------------------------

    def grant(
        self,
        steward_credential: str,
        agent: str,
        kind: ResourceKind,
        target: str,
        *,
        ttl_s: float,
        reason: str,
        requested_by: str,
        mode: Mode | None = None,
    ) -> Rule:
        """Widen an agent's permission within its expansion ceiling.

        Raises:
            AaestroError: FORBIDDEN/``ACC_001`` unknown credential,
                FORBIDDEN/``ACC_003`` the caller is not a permission agent, grants to itself,
                exceeds the ceiling or its TTL, or the permission is revoked by an operator,
                NOT_FOUND/``NF_001`` unknown agent.
        """
        steward = self.identify(steward_credential)
        _check_mode(kind, mode, memory_needs_mode=True)
        try:
            self._check_grant(steward, agent, kind, target, mode, ttl_s)
        except AaestroError as err:
            if err.code == ErrorCode.ACC_003:
                self._count_grant(kind, "refuse", steward)
                log.warning(
                    "access grant refused",
                    agent=agent,
                    resource=kind.value,
                    target=target,
                    decided_by=steward,
                    error_code=str(err.code),
                    reason=err.message,
                )
            raise
        now = self.clock()
        rule = Rule(
            rule_id=f"rule-{uuid.uuid4().hex[:16]}",
            agent=agent,
            kind=kind,
            target=target,
            mode=mode,
            effect=Effect.ALLOW,
            decided_by=steward,
            requested_by=requested_by,
            reason=reason,
            created_at=now,
            expires_at=now + ttl_s,
        )
        return self._record(rule, op="grant")

    def _check_grant(
        self,
        steward: str,
        agent: str,
        kind: ResourceKind,
        target: str,
        mode: Mode | None,
        ttl_s: float,
    ) -> None:
        if steward not in self.stewards:
            raise _refused("only a permission agent may grant", caller=steward)
        if steward == agent:
            raise _refused("a permission agent may not grant to itself", agent=agent)
        self.catalog.agent(agent)
        ceilings = self._ceilings_for(agent, kind, target, mode)
        if not ceilings:
            raise _refused(
                "grant exceeds the expansion ceiling",
                agent=agent,
                resource=kind.value,
                target=target,
            )
        # 여러 상한이 같은 대상을 덮으면 가장 엄한 만료가 이긴다 — 먼저 찾은 것을 쓰면 global 의
        # 넉넉한 TTL 이 그룹이 좁혀 둔 TTL 을 건너뛴다
        max_ttl_s = min(ceiling.max_ttl_s for ceiling in ceilings)
        if not 0 < ttl_s <= max_ttl_s:
            raise _refused(
                "grant lifetime exceeds the ceiling",
                agent=agent,
                ttl_s=ttl_s,
                max_ttl_s=max_ttl_s,
            )
        now = self.clock()
        if any(
            r.effect is Effect.DENY and r.active(now) and r.covers(kind, target, mode)
            for r in self.store.rules(agent)
        ):
            # 운영자가 회수한 것을 권한 에이전트가 되살리지 못한다 — 되돌리는 것은 운영자다
            raise _refused("permission is revoked by an operator", agent=agent, target=target)

    def _ceilings_for(
        self, agent: str, kind: ResourceKind, target: str, mode: Mode | None
    ) -> list[AccessCeiling]:
        """요청을 덮는 상한 **전부** — 소속 그룹과 global 의 선언에서 찾는다."""
        group = self.catalog.agent(agent).metadata.group
        names = [RESERVED_GLOBAL_GROUP] + ([group] if group else [])
        covering: list[AccessCeiling] = []
        for name in names:
            try:
                ceiling = self.catalog.group(name).spec.access.ceiling
            except AaestroError as err:
                if err.code == ErrorCode.NF_001:
                    continue
                raise
            if ceiling is not None and _ceiling_covers(ceiling, kind, target, mode):
                covering.append(ceiling)
        return covering

    # --- 조회와 변경 알림 --------------------------------------------------------

    def rules(self, agent: str) -> list[Rule]:
        return list(self.store.rules(agent))

    def declared(self, agent: str) -> dict[ResourceKind, list[tuple[str, Mode | None]]]:
        """What declarations give this agent, per kind — the baseline the operator narrows.

        강제 지점이 연결된 종류만 — 선언 판정이 없는 종류는 선언이 아무것도 주지 않는다.
        """
        self._sync_declarations()
        return {
            kind: baseline.declared_for(agent)
            for kind, baseline in self.baselines.items()
            if hasattr(baseline, "declared_for")
        }

    def ceilings(self, agent: str) -> dict[str, AccessCeiling]:
        """확장 상한 — 소속 그룹과 global 에서, 선언한 곳의 이름으로."""
        group = self.catalog.agent(agent).metadata.group
        found: dict[str, AccessCeiling] = {}
        for name in [RESERVED_GLOBAL_GROUP] + ([group] if group else []):
            try:
                ceiling = self.catalog.group(name).spec.access.ceiling
            except AaestroError as err:
                if err.code == ErrorCode.NF_001:
                    continue
                raise
            if ceiling is not None:
                found[name] = ceiling
        return found

    def recent_denials(self, agent: str) -> list[Denial]:
        """이 프로세스가 내린 최근 거부 — 새것부터. 강제 지점의 캐시 적중은 여기 오지 않는다."""
        return list(self._denials.get(agent, ()))

    def version(self) -> int:
        self._sync_declarations()
        return self.store.version()

    async def wait_for_change(self, after: int, timeout_s: float) -> int:
        """Return the registry version once it moves past ``after``, or at the timeout.

        강제 지점의 변경 알림 (01 Access Control — 캐시 + 변경 알림). 긴 폴링이라 알림을 받는 쪽이
        연결을 끊어도 레지스트리에 남는 상태가 없다.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            current = self.version()
            remaining = deadline - loop.time()
            if current > after or remaining <= 0:
                return current
            if self._changed is None:
                self._changed = asyncio.Event()
            event = self._changed
            # 기록 변경은 이벤트가 깨우지만 선언 파일 변경은 아무도 알려주지 않는다 — 짧게 끊어 본다
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    event.wait(), timeout=min(remaining, max(self.declarations_poll_s, 0.05))
                )

    # --- 내부 ---------------------------------------------------------------

    def _sync_declarations(self) -> None:
        """선언 파일이 바뀌었으면 버전을 올린다 — 선언 판정을 캐시한 강제 지점이 버리게.

        처음 볼 때도 올린다: 레지스트리가 멈춘 동안 파일이 바뀌었는지 알 수 없고, 강제 지점은
        재시작 전 버전의 캐시를 들고 있을 수 있다.
        """
        now = self.clock()
        if now - self._declarations_seen_at < self.declarations_poll_s:
            return
        self._declarations_seen_at = now
        current = declaration_fingerprint(self.catalog)
        if current == self._declarations:
            return
        first = self._declarations is None
        self._declarations = current
        self.store.touch()
        self._wake_waiters()
        if not first:
            log.info("access declarations changed", files=len(current))

    def _record(self, rule: Rule, *, op: str) -> Rule:
        self.store.put_rule(rule)
        self._wake_waiters()
        self._count_grant(rule.kind, op, rule.decided_by)
        log.info(
            "access rule recorded",
            agent=rule.agent,
            resource=rule.kind.value,
            target=rule.target,
            grant_id=rule.rule_id,
            decided_by=rule.decided_by,
            op=op,
            effect=rule.effect.value,
        )
        return rule

    def _wake_waiters(self) -> None:
        """버전은 저장소가 기록과 함께 올렸다 — 여기서는 기다리는 쪽만 깨운다."""
        if self._changed is not None:
            # 기다리던 쪽을 모두 깨우고, 다음 대기는 새 이벤트로 — set 된 채 두면 계속 깨어난다
            self._changed.set()
            self._changed = None

    def _count_decision(self, kind: ResourceKind, outcome: Outcome) -> None:
        if self.metrics is not None:
            self.metrics.counter("aaestro_access_decisions_total").labels(
                resource=kind.value, decision=outcome.value, source="fresh"
            ).inc()

    def _count_grant(self, kind: ResourceKind, op: str, decided_by: str) -> None:
        if self.metrics is not None:
            self.metrics.counter("aaestro_access_grants_total").labels(
                resource=kind.value, op=op, decided_by=decided_by
            ).inc()


def _ceiling_covers(
    ceiling: AccessCeiling, kind: ResourceKind, target: str, mode: Mode | None
) -> bool:
    if kind is ResourceKind.MEMORY:
        return any(
            entry.space == target and (entry.mode.value == "rw" or mode is not Mode.RW)
            for entry in ceiling.memory
        )
    listed = {
        ResourceKind.EGRESS: ceiling.egress,
        ResourceKind.MCP_TOOL: ceiling.mcp_tool,
        ResourceKind.A2A: ceiling.a2a,
    }[kind]
    return target in listed


__all__ = [
    "INVALID_TICKET",
    "TICKET_TTL_S",
    "ACCESS_CREDENTIAL_ENV",
    "AccessRegistry",
    "Baseline",
    "credential_hash",
]
