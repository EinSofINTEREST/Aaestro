"""Who is asking, and what they may touch right now.

Memory Service 의 입구. 두 가지가 있다:

- ``TokenGate`` — 기동 시 발급한 토큰에 권한을 담는다. 권한이 기동 시점에 고정된다
- ``RegistryGate`` — 에이전트는 **신원**만 내민다. 어떤 space 에 어떤 mode 로 닿는지는 요청마다
  레지스트리에 묻는다 (09 Access Enforcement 2). 회수·강등이 재시작 없이 다음 요청에 반영되고,
  신원은 control plane 이 발급해 이 서비스의 재시작을 넘는다

어느 쪽이든 서비스에 넘기는 것은 ``AccessToken`` 이다 — 레지스트리 입구는 **허용된 space 만** 담은
토큰으로 좁혀서 넘기므로, 서비스의 기존 검사(미선언·쓰기 불가 → ``MEM_001``)와 감사 로그가 그대로
거부를 처리한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

import structlog

from aaestro.access.client import DecisionSource
from aaestro.access.model import Mode, ResourceKind
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import RESERVED_GLOBAL_GROUP, MemoryMode
from aaestro.memory.service import AccessToken, MemorySpace
from aaestro.memory.tokens import issue_token

if TYPE_CHECKING:
    from aaestro.access.client import AccessClient
    from aaestro.catalog import Catalog
    from aaestro.memory.http import TokenRegistry

log = structlog.get_logger(__name__)


def unauthorized(reason: str) -> AaestroError:
    """토큰 없는/알 수 없는 요청 — space 존재 여부조차 알려주지 않는다."""
    return AaestroError(category=ErrorCategory.MEMORY, code=ErrorCode.MEM_001, message=reason)


class Gate(Protocol):
    async def admit(self, presented: str | None) -> AccessToken:
        """The caller and the spaces its declarations name — ``MEM_001`` if unknown."""
        ...

    async def narrow(
        self, presented: str, token: AccessToken, aliases: Sequence[str], *, write: bool
    ) -> AccessToken:
        """The token cut down to what may be touched now, for these aliases."""
        ...


@dataclass(frozen=True)
class TokenGate:
    """기동 시 발급한 토큰 — 권한은 토큰에 있다."""

    tokens: TokenRegistry

    async def admit(self, presented: str | None) -> AccessToken:
        return self.tokens.resolve(presented)

    async def narrow(
        self, presented: str, token: AccessToken, aliases: Sequence[str], *, write: bool
    ) -> AccessToken:
        return token


@dataclass(frozen=True)
class RegistryGate:
    """Identity in, per-request registry decisions out.

    Attributes:
        client: 레지스트리 판정 클라이언트 — 캐시와 장애 시 동작은 거기 있다.
        catalog: 별칭을 space 로 해석하는 선언 — 요청마다 읽어 그룹 이동 등이 바로 반영된다.
    """

    client: AccessClient
    catalog: Catalog

    async def admit(self, presented: str | None) -> AccessToken:
        if not presented:
            raise unauthorized("memory token is required")
        agent, _ = await self.client.identify(presented)
        if agent is None:
            # 모르는 신원, 폐기된 신원, 판정 불가 모두 — 무엇이 없는지 알려주지 않는다
            raise unauthorized("memory token is not recognised")
        return self._declared(agent)

    async def narrow(
        self, presented: str, token: AccessToken, aliases: Sequence[str], *, write: bool
    ) -> AccessToken:
        mode = Mode.RW if write else Mode.RO
        allowed: list[MemorySpace] = []
        for alias in dict.fromkeys(aliases):
            space = token.resolve(alias) or _addressed_by_id(alias)
            if space is None:
                continue  # 미선언 — 서비스가 MEM_001 로 거부하고 감사 로그를 남긴다
            verdict = await self.client.decide(presented, ResourceKind.MEMORY, space.space_id, mode)
            _log_decision(token.agent, space, verdict.allowed, verdict.source, mode)
            if verdict.allowed:
                # 판정이 곧 권한이다 — 선언의 mode/writers 대신 판정을 담는다
                allowed.append(
                    replace(
                        space,
                        mode=MemoryMode.RW if write else MemoryMode.RO,
                        writers=(token.agent,) if write else (),
                    )
                )
        return replace(token, spaces=tuple(allowed))

    def _declared(self, agent: str) -> AccessToken:
        try:
            manifest = self.catalog.agent(agent)
        except AaestroError as err:
            raise unauthorized("memory token is not recognised") from err
        groups = self.catalog.groups().items
        global_group = groups.get(RESERVED_GLOBAL_GROUP)
        return issue_token(
            manifest,
            group=groups.get(manifest.metadata.group or ""),
            global_spaces=global_group.spec.memory if global_group else None,
        )


def _addressed_by_id(alias: str) -> MemorySpace | None:
    """선언하지 않은 space 를 space id 로 부른 경우 — 부여받은 space 에 닿는 길 (#279).

    해석만 넓힌다: 허용 여부는 여전히 요청마다 레지스트리가 판정한다. 선언도 부여도 없으면 거부.
    """
    from aaestro.access.baselines import SPACE_ID
    from aaestro.modules.memoryset import MemoryScope

    matched = SPACE_ID.fullmatch(alias)
    if matched is None:
        return None
    return MemorySpace(
        alias=alias,
        scope=MemoryScope(matched["scope"]),
        owner=matched["owner"],
        name=matched["alias"],
    )


def _log_decision(
    agent: str, space: MemorySpace, allowed: bool, source: DecisionSource, mode: Mode
) -> None:
    fields = {
        "agent": agent,
        "resource": ResourceKind.MEMORY.value,
        "target": space.space_id,
        "memory_space": space.alias,
        "access_mode": mode.value,
        "decision": "allow" if allowed else "deny",
        "decision_source": source.value,
    }
    if allowed:
        log.debug("memory access decided", **fields)
    else:
        log.info("memory access denied by registry", **fields)


__all__ = ["Gate", "RegistryGate", "TokenGate", "unauthorized"]
