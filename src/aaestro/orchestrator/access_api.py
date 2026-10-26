"""Control plane routes for the access registry (#277).

누가 부르느냐로 인증이 갈린다 — 한 토큰으로 묶으면 강제 지점이 운영자 권한을, 권한 에이전트가
판정 조회 권한을 덤으로 갖는다:

| 호출자 | 자격 | 라우트 |
|---|---|---|
| 운영자 | control plane 토큰 | 조회 · 회수 · 기록 종료 |
| 권한 에이전트 | 자기 에이전트 신원 | 부여 |
| 에이전트 (A2A) | 자기 에이전트 신원 | 호출 표 발급 · 받은 표 확인 · 변경 알림 |
| 강제 지점 | enforcer 토큰 | 판정 · 신원 · 변경 알림 |
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, ClassVar

from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from aaestro.access.model import Mode, ResourceKind, Rule, mode_problem
from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.http_auth import admit_first, presented_token, require_token
from aaestro.orchestrator.bodies import parsed

if TYPE_CHECKING:
    from collections.abc import Callable

    from aaestro.access.registry import AccessRegistry

MAX_WAIT_S = 30.0
"""변경 알림 긴 폴링의 상한 — 프록시·로드밸런서의 유휴 끊김보다 짧게."""

_TARGET = Field(min_length=1, max_length=512, pattern=r"^[^\s\x00-\x1f]+$")


class _Request(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ResourceKind
    mode: Mode | None = None
    memory_needs_mode: ClassVar[bool] = True

    @model_validator(mode="after")
    def _mode_fits_the_kind(self) -> _Request:
        problem = mode_problem(self.kind, self.mode, memory_needs_mode=self.memory_needs_mode)
        if problem is not None:
            raise ValueError(problem)
        return self


class RevocationRequest(_Request):
    memory_needs_mode: ClassVar[bool] = False  # 모드 없는 메모리 회수 = 읽기·쓰기 모두 회수

    agent: str
    target: str = _TARGET
    reason: str = Field(min_length=1, max_length=1000)
    expires_in_s: float | None = Field(default=None, gt=0)


class GrantRequest(_Request):
    agent: str
    target: str = _TARGET
    ttl_s: float = Field(gt=0)
    reason: str = Field(min_length=1, max_length=1000)
    requested_by: str = Field(min_length=1, max_length=200)


class TicketRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    callee: str = Field(min_length=1, max_length=200)


class VerifyRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket: str = Field(min_length=1, max_length=512)


class IdentityRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    credential: str = Field(min_length=1, max_length=512)


class DecisionRequest(_Request):
    credential: str = Field(min_length=1, max_length=512)
    target: str = _TARGET


def rule_view(rule: Rule) -> dict[str, Any]:
    return {
        "rule_id": rule.rule_id,
        "agent": rule.agent,
        "kind": rule.kind.value,
        "target": rule.target,
        "mode": rule.mode.value if rule.mode else None,
        "effect": rule.effect.value,
        "decided_by": rule.decided_by,
        "requested_by": rule.requested_by,
        "reason": rule.reason,
        "created_at": rule.created_at,
        "expires_at": rule.expires_at,
        "lifted_at": rule.lifted_at,
    }


@dataclass(frozen=True)
class AccessAudiences:
    """운영자가 아닌 두 청중이 닿는 경로 — 라우팅 전 검사가 청중을 좁히는 근거 (#462).

    라우터에 등록된 라우트에서 **만들어진다** — 손으로 쓴 목록이면 라우트를 더할 때 한쪽만 바뀐다.
    """

    enforcer: frozenset[str]
    agent: frozenset[str]


def mount_access(
    app: FastAPI, operator: APIRouter, registry: AccessRegistry, *, enforcer_token: str | None
) -> AccessAudiences:
    """Attach the three audiences' routes.

    ``operator`` 는 이미 control plane 토큰이 걸린 라우터다. 권한 에이전트와 강제 지점의 라우트는
    그 토큰을 요구하지 않으므로 앱에 따로 붙인다. 세 라우터 모두 자격을 **본문을 읽기 전에**
    확인한다 (#462) — 자격이 없으면 본문이 무엇이든 같은 거절이다.
    """
    _mount_operator(operator, registry)

    # 에이전트 신원인지만 먼저 본다 — 표의 주인인지는 본문이 필요하므로 레지스트리가 라우트 안에서
    # 다시 판정한다
    agents = APIRouter(route_class=admit_first(_live_agent(registry)))
    # 부여는 권한 에이전트만 — 본문을 읽기 전에 좁힌다. 작업 에이전트는 깨진 본문으로도 ACC_003 과
    # 거절 기록 (WARN · 메트릭) 을 피하지 못한다 (#462)
    stewards_only = APIRouter(route_class=admit_first(_permission_agent(registry)))

    @agents.post("/v1/access/a2a/tickets", status_code=status.HTTP_201_CREATED)
    async def ticket(request: Request, body: Annotated[Any, Body()]) -> dict[str, Any]:
        """호출 표 — 자격은 **호출하려는 에이전트 자신의** 신원이다 (03 Enforcement)."""
        asked = parsed(body, TicketRequest)
        issued, expires_at = registry.issue_ticket(presented_token(request) or "", asked.callee)
        return {"ticket": issued, "callee": asked.callee, "expires_at": expires_at}

    @agents.post("/v1/access/a2a/verify")
    async def verify(request: Request, body: Annotated[Any, Body()]) -> dict[str, Any]:
        """받은 표 확인 — 자격은 **피호출자 자신의** 신원이다. 남의 표는 거부 판정으로 답한다."""
        asked = parsed(body, VerifyRequest)
        decision = registry.verify_ticket(presented_token(request) or "", asked.ticket)
        return {
            "agent": decision.agent or None,
            "decision": decision.outcome.value,
            "decided_by": decision.decided_by,
            "version": decision.version,
            "valid_until": decision.valid_until,
        }

    @stewards_only.post("/v1/access/grants", status_code=status.HTTP_201_CREATED)
    async def grant(request: Request, body: Annotated[Any, Body()]) -> dict[str, Any]:
        """부여 — 자격은 **호출한 권한 에이전트 자신의** 신원이다."""
        asked = parsed(body, GrantRequest)
        rule = registry.grant(
            presented_token(request) or "",
            asked.agent,
            asked.kind,
            asked.target,
            mode=asked.mode,
            ttl_s=asked.ttl_s,
            reason=asked.reason,
            requested_by=asked.requested_by,
        )
        return rule_view(rule)

    enforcer = APIRouter(
        route_class=admit_first(require_token(enforcer_token, realm="access enforcer token"))
    )

    @enforcer.post("/v1/access/decisions")
    async def decide(body: Annotated[Any, Body()]) -> dict[str, Any]:
        """판정 — 모르는 자격도 **거부 판정**으로 답한다. 강제 지점이 캐시할 수 있어야 한다."""
        asked = parsed(body, DecisionRequest)
        try:
            identity = registry.identity_of(asked.credential)
        except AaestroError as err:
            if err.code != ErrorCode.ACC_001:
                raise
            return {
                "agent": None,
                "decision": "deny",
                "decided_by": "unknown-identity",
                "version": registry.version(),
                "valid_until": None,
            }
        decision = registry.decide(
            identity.agent, asked.kind, asked.target, asked.mode, identity=identity
        )
        return {
            "agent": decision.agent,
            "decision": decision.outcome.value,
            "decided_by": decision.decided_by,
            "version": decision.version,
            "valid_until": decision.valid_until,
        }

    @enforcer.post("/v1/access/identities")
    async def identify(body: Annotated[Any, Body()]) -> dict[str, Any]:
        """자격 → 에이전트. 강제 지점이 대상을 해석하려면 먼저 누구인지 알아야 한다 (메모리 별칭).

        모르는 자격도 에러가 아니라 ``agent: null`` 이다 — 판정과 같은 이유로 캐시할 수 있어야 한다.
        """
        asked = parsed(body, IdentityRequest)
        try:
            agent: str | None = registry.identify(asked.credential)
        except AaestroError as err:
            if err.code != ErrorCode.ACC_001:
                raise
            agent = None
        return {"agent": agent, "version": registry.version()}

    feed = APIRouter(route_class=admit_first(_enforcer_or_agent(registry, enforcer_token)))

    @feed.get("/v1/access/changes")
    async def changes(
        after: Annotated[int, Query(ge=0)] = 0,
        wait_s: Annotated[float, Query(ge=0, le=MAX_WAIT_S)] = MAX_WAIT_S,
    ) -> dict[str, int]:
        """변경 알림 긴 폴링 — 버전이 ``after`` 를 넘거나 ``wait_s`` 가 지나면 답한다.

        피호출자 에이전트도 받은 표의 판정을 캐시하므로 자기 신원으로 따라온다 — 알림은 버전뿐이다.
        """
        return {"version": await registry.wait_for_change(after, timeout_s=wait_s)}

    app.include_router(agents)
    app.include_router(stewards_only)
    app.include_router(enforcer)
    app.include_router(feed)
    return AccessAudiences(
        enforcer=_paths(enforcer, feed),
        agent=_paths(agents, stewards_only, feed),
    )


def _paths(*routers: APIRouter) -> frozenset[str]:
    return frozenset(route.path for router in routers for route in router.routes)  # type: ignore[attr-defined]


def _live_agent(registry: AccessRegistry) -> Callable[[Request], None]:
    """살아 있는 에이전트 신원 — 아니면 레지스트리의 ``ACC_001`` (본문이 맞을 때와 같은 거절)."""

    def check(request: Request) -> None:
        registry.identify(presented_token(request) or "")

    return check


def _permission_agent(registry: AccessRegistry) -> Callable[[Request], None]:
    """권한 에이전트 신원 — 아니면 ``ACC_001`` (모르는 자격) / ``ACC_003`` (기록되는 부여 거절)."""

    def check(request: Request) -> None:
        registry.admit_grant_caller(presented_token(request) or "")

    return check


def _enforcer_or_agent(registry: AccessRegistry, enforcer_token: str | None) -> Any:
    """강제 지점 토큰, 또는 살아 있는 에이전트 신원."""

    def check(request: Request) -> None:
        presented = presented_token(request) or ""
        if enforcer_token is None:
            return
        if hmac.compare_digest(presented.encode(), enforcer_token.encode()):
            return
        try:
            registry.identify(presented)
        except AaestroError as err:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid access feed credential",
                headers={"WWW-Authenticate": "Bearer"},
            ) from err

    return check


def _mount_operator(api: APIRouter, registry: AccessRegistry) -> None:
    @api.get("/v1/access/agents/{name}")
    async def agent_rules(name: str) -> dict[str, Any]:
        registry.catalog.agent(name)
        return {
            "agent": name,
            "version": registry.version(),
            "rules": [rule_view(rule) for rule in registry.rules(name)],
            # 운영자 화면이 한 번에 보는 것 — 기본 권한, 확장 상한, 최근 거부 (#283)
            "declared": [
                {"kind": kind.value, "target": target, "mode": mode.value if mode else None}
                for kind, entries in registry.declared(name).items()
                for target, mode in entries
            ],
            "ceilings": [
                {"group": group, **ceiling.model_dump(mode="json")}
                for group, ceiling in registry.ceilings(name).items()
            ],
            "denials": [
                {
                    "kind": denial.kind.value,
                    "target": denial.target,
                    "mode": denial.mode.value if denial.mode else None,
                    "decided_by": denial.decided_by,
                    "at": denial.at,
                }
                for denial in registry.recent_denials(name)
            ],
        }

    @api.post("/v1/access/revocations", status_code=status.HTTP_201_CREATED)
    async def revoke(body: Annotated[Any, Body()]) -> dict[str, Any]:
        asked = parsed(body, RevocationRequest)
        rule = registry.revoke(
            asked.agent,
            asked.kind,
            asked.target,
            mode=asked.mode,
            reason=asked.reason,
            expires_in_s=asked.expires_in_s,
        )
        return rule_view(rule)

    @api.delete("/v1/access/rules/{rule_id}")
    async def lift(rule_id: str) -> dict[str, Any]:
        """회수를 되돌리거나 부여를 일찍 끝낸다 — 기록은 지우지 않고 종료 시각을 남긴다."""
        return rule_view(registry.lift(rule_id))


__all__ = ["mount_access"]
