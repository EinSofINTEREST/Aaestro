"""The enforcement point's side of the registry — cache, invalidation, and outages.

강제 지점(Memory Service, 이그레스 프록시, A2A 피호출자)이 레지스트리에 판정을 묻는 클라이언트.
01 Access Control 의 캐시 규칙을 여기 한 곳에 둔다 — 강제 지점마다 따로 짜면 장애 시 동작이 갈린다:

1. **캐시 + 변경 알림**: 판정은 캐시하고, 변경 알림(긴 폴링)이 새 버전을 알리면 전부 버린다.
   알림이 끊겼지만 레지스트리에 닿는 동안에는 ``recheck_s`` 가 지난 판정을 다시 묻는다
2. **레지스트리에 닿지 않으면**: 캐시에 없는 판정은 거부. 캐시된 판정은 알림이 끊겨도 그대로 쓴다.
   "닿지 않음" 은 전송 실패·시간 초과·5xx 뿐이다 — 레지스트리가 **거절**하면(4xx, 잘못된 응답)
   캐시도 버리고 거부한다. 강제 지점 토큰이 틀린 설정을 장애로 보고 옛 허용을 계속 쓰면 안 된다
3. **만료는 자기 시계로**: 판정의 ``valid_until`` 이 지나면 캐시를 쓰지 않는다 — 부여의 만료는
   레지스트리 없이도 알 수 있으므로 장애 중에도 지킨다
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from aaestro.access.model import Mode, ResourceKind

if TYPE_CHECKING:
    from aaestro.observability.metrics import Metrics

log = structlog.get_logger(__name__)

ACCESS_URL_ENV = "AAESTRO_ACCESS_URL"
ENFORCER_TOKEN_ENV = "AAESTRO_ACCESS_ENFORCER_TOKEN"  # noqa: S105 — 키 이름이지 값이 아니다

RECHECK_S = 2.0
"""알림이 끊겼을 때 캐시한 판정을 다시 묻는 주기 (01 Access Control — 결정 D5)."""
REQUEST_TIMEOUT_S = 2.0
FEED_WAIT_S = 25.0
"""변경 알림 긴 폴링 한 번의 대기 — 레지스트리 상한(30초)보다 짧게."""


class DecisionSource(StrEnum):
    FRESH = "fresh"
    CACHE = "cache"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class Verdict:
    """What the enforcement point acts on."""

    agent: str | None
    allowed: bool
    decided_by: str
    source: DecisionSource


@dataclass(frozen=True)
class _Cached:
    value: Any
    version: int
    fetched_at: float
    valid_until: float | None


class _UnreachableError(Exception):
    """레지스트리가 답하지 않았다 — 원인은 로그로, 판정은 캐시 규칙으로."""


class _RejectedError(Exception):
    """레지스트리가 답했지만 판정을 주지 않았다 — 설정·인증 문제다. 캐시로 버티지 않는다."""


def _classify(err: Exception) -> type[Exception]:
    if isinstance(err, httpx.TransportError):
        return _UnreachableError
    if isinstance(err, httpx.HTTPStatusError) and err.response.status_code >= 500:
        return _UnreachableError
    return _RejectedError


@dataclass
class AccessClient:
    """Ask the registry, cache the answers, and drop them when the registry says so.

    Attributes:
        base_url: control plane 주소.
        enforcer_token: 강제 지점 자격 — 운영자 토큰과 다르다. 피호출자 에이전트의 A2A 서버에서는
            그 에이전트 **자신의 신원**이다 (강제 지점 토큰을 에이전트 컨테이너에 넣지 않는다).
        component: 계측 라벨 (``memory`` / ``egress`` / ``a2a``).
        clock: epoch 초 — ``valid_until`` 과 같은 시계. 테스트는 주입한다.
    """

    base_url: str
    enforcer_token: str
    component: str
    http: httpx.AsyncClient | None = None
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    metrics: Metrics | None = None
    recheck_s: float = RECHECK_S
    feed_wait_s: float = FEED_WAIT_S
    _version: int = field(default=-1, init=False)
    _feed_alive: bool = field(default=False, init=False)
    _identities: dict[str, _Cached] = field(default_factory=dict, init=False)
    _decisions: dict[tuple[str, str, str, str], _Cached] = field(default_factory=dict, init=False)

    # --- 판정 ---------------------------------------------------------------

    async def identify(self, credential: str) -> tuple[str | None, DecisionSource]:
        """The agent behind a credential — ``None`` if unknown, revoked, or undecidable."""
        key = _hash(credential)

        async def fetch() -> tuple[Any, int, float | None]:
            body = await self._post("/v1/access/identities", {"credential": credential})
            return body["agent"], int(body["version"]), None

        agent, source = await self._cached(self._identities, key, fetch)
        return agent, source

    async def decide(
        self, credential: str, kind: ResourceKind, target: str, mode: Mode | None = None
    ) -> Verdict:
        key = (_hash(credential), kind.value, target, mode.value if mode else "")

        async def fetch() -> tuple[Any, int, float | None]:
            body = await self._post(
                "/v1/access/decisions",
                {
                    "credential": credential,
                    "kind": kind.value,
                    "target": target,
                    **({"mode": mode.value} if mode else {}),
                },
            )
            answer = (body["agent"], body["decision"] == "allow", body["decided_by"])
            return answer, int(body["version"]), body.get("valid_until")

        answer, source = await self._cached(self._decisions, key, fetch)
        if answer is None:
            verdict = Verdict(None, False, "unreachable", source)
        else:
            verdict = Verdict(answer[0], answer[1], answer[2], source)
        self._count(kind, verdict)
        if source is DecisionSource.UNREACHABLE:
            log.warning(
                "access decision unavailable",
                resource=kind.value,
                target=target,
                decision="deny",
                decision_source=source.value,
            )
        return verdict

    async def verify_ticket(self, ticket: str) -> Verdict:
        """Decide an inbound A2A call — the bearer is the callee's own identity (#281)."""

        async def fetch() -> tuple[Any, int, float | None]:
            body = await self._post("/v1/access/a2a/verify", {"ticket": ticket})
            answer = (body["agent"], body["decision"] == "allow", body["decided_by"])
            return answer, int(body["version"]), body.get("valid_until")

        answer, source = await self._cached(
            self._decisions, ("ticket", _hash(ticket), "", ""), fetch
        )
        if answer is None:
            verdict = Verdict(None, False, "unreachable", source)
        else:
            verdict = Verdict(answer[0], answer[1], answer[2], source)
        self._count(ResourceKind.A2A, verdict)
        return verdict

    # --- 변경 알림 ------------------------------------------------------------

    async def watch(self) -> None:
        """Follow the registry's change feed until cancelled — the owner holds the task."""
        while True:
            try:
                response = await self._client().get(
                    "/v1/access/changes",
                    params={"after": max(self._version, 0), "wait_s": self.feed_wait_s},
                    headers=self._headers(),
                    timeout=self.feed_wait_s + REQUEST_TIMEOUT_S,
                )
                response.raise_for_status()
                version = int(response.json()["version"])
            except (httpx.HTTPError, ValueError, KeyError) as err:
                if _classify(err) is _RejectedError:
                    # 거절당한 강제 지점이 쥔 캐시는 믿을 근거가 없다
                    log.error("access change feed rejected", component=self.component, exc_info=err)
                    self._drop_cache()
                elif self._feed_alive:
                    log.warning("access change feed lost", component=self.component, exc_info=err)
                self._feed_alive = False
                self._reachable(False)
                await self.sleep(self.recheck_s)
                continue
            if not self._feed_alive:
                log.info("access change feed connected", component=self.component)
            self._feed_alive = True
            self._reachable(True)
            if version != self._version:
                # 낮아진 버전도 버린다 — 레지스트리 저장소가 바뀌었다는 뜻이다
                self._invalidate(version)

    async def aclose(self) -> None:
        await self._client().aclose()

    # --- 내부 ---------------------------------------------------------------

    async def _cached(
        self,
        cache: dict[Any, _Cached],
        key: Any,
        fetch: Callable[[], Awaitable[tuple[Any, int, float | None]]],
    ) -> tuple[Any, DecisionSource]:
        now = self.clock()
        entry = cache.get(key)
        if entry is not None and self._usable(entry, now):
            return entry.value, DecisionSource.CACHE
        try:
            value, version, valid_until = await fetch()
        except _UnreachableError:
            self._reachable(False)
            if entry is not None and not _expired(entry, now):
                # 캐시에 있던 판정은 알림이 끊겨도 그대로 쓴다 (01 — 결정 D4)
                return entry.value, DecisionSource.CACHE
            return None, DecisionSource.UNREACHABLE
        except _RejectedError:
            self._reachable(False)
            cache.pop(key, None)
            return None, DecisionSource.UNREACHABLE
        self._reachable(True)
        if version >= self._version:
            # 알림이 이미 더 새 버전을 알렸으면 이 답은 그 전의 것이다 — 캐시하지 않는다
            cache[key] = _Cached(value, version, now, valid_until)
        return value, DecisionSource.FRESH

    def _usable(self, entry: _Cached, now: float) -> bool:
        if _expired(entry, now):
            return False
        return self._feed_alive or now - entry.fetched_at < self.recheck_s

    def _invalidate(self, version: int) -> None:
        self._version = version
        self._drop_cache()

    def _drop_cache(self) -> None:
        if not (self._identities or self._decisions):
            return
        self._identities.clear()
        self._decisions.clear()
        if self.metrics is not None:
            self.metrics.counter("aaestro_access_cache_invalidations_total").labels(
                component=self.component
            ).inc()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client().post(path, json=body, headers=self._headers())
            response.raise_for_status()
            found: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as err:
            kind = _classify(err)
            if kind is _RejectedError:
                log.error(
                    "access registry rejected the request", component=self.component, exc_info=err
                )
            else:
                log.warning("access registry unreachable", component=self.component, exc_info=err)
            raise kind from err
        return found

    def _client(self) -> httpx.AsyncClient:
        if self.http is None:
            self.http = httpx.AsyncClient(base_url=self.base_url, timeout=REQUEST_TIMEOUT_S)
        return self.http

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.enforcer_token}"}

    def _reachable(self, reachable: bool) -> None:
        if self.metrics is not None:
            self.metrics.gauge("aaestro_access_registry_reachable").labels(
                component=self.component
            ).set(1 if reachable else 0)

    def _count(self, kind: ResourceKind, verdict: Verdict) -> None:
        # fresh 는 레지스트리가 센다 — 여기서도 세면 두 번이다
        if self.metrics is None or verdict.source is DecisionSource.FRESH:
            return
        self.metrics.counter("aaestro_access_decisions_total").labels(
            resource=kind.value,
            decision="allow" if verdict.allowed else "deny",
            source=verdict.source.value,
        ).inc()


def _expired(entry: _Cached, now: float) -> bool:
    return entry.valid_until is not None and now >= entry.valid_until


def _hash(credential: str) -> str:
    """캐시 키에도 자격 값을 두지 않는다 — 프로세스 덤프에 남는 것을 줄인다."""
    return hashlib.sha256(credential.encode()).hexdigest()


__all__ = [
    "ACCESS_URL_ENV",
    "ENFORCER_TOKEN_ENV",
    "AccessClient",
    "DecisionSource",
    "Verdict",
]
