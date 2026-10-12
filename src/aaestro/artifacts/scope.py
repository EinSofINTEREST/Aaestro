"""Scoped artifact access.

01 은 artifact 를 **global / group / local** 3계층 리소스로 규정한다. 이 모듈이
그 경계를 강제한다 — 없으면 artifact 가 graph state 를 우회하는 사이드채널이
된다 (01 은 그런 경로를 금지하며 예외는 선언된 memory space 뿐이다).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from aaestro.artifacts.store import parse_ref
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aaestro.artifacts.store import FilesystemArtifactStore


class ArtifactScope(StrEnum):
    """Artifact 가 놓인 스코프 — secrets/memory 와 같은 어휘."""

    LOCAL = "local"
    GROUP = "group"
    GLOBAL = "global"


RESOLUTION_ORDER = (ArtifactScope.LOCAL, ArtifactScope.GROUP, ArtifactScope.GLOBAL)
"""가까운 스코프가 이긴다 — 01 Resource Scoping 의 해석 순서."""


def denied(scope: str, reason: str, **details: str) -> AaestroError:
    """스코프 밖 접근 — 조용히 허용하면 경계가 무의미해진다."""
    return AaestroError(
        category=ErrorCategory.FORBIDDEN,
        code=ErrorCode.ART_001,
        message=f"artifact scope access denied: {reason}",
        details={"scope": scope, **details},
    )


def over_quota(scope: str, *, used: int, limit: int, incoming: int) -> AaestroError:
    """quota 초과 — 저장을 거부한다."""
    return AaestroError(
        category=ErrorCategory.STORAGE,
        code=ErrorCode.ART_002,
        message="artifact quota exceeded",
        details={
            "scope": scope,
            "used_bytes": str(used),
            "limit_bytes": str(limit),
            "incoming_bytes": str(incoming),
        },
    )


@dataclass
class ScopedArtifacts:
    """Routes artifact access across local / group / global scopes.

    스코프별 저장소를 묶어 하나의 ``ArtifactStore`` 처럼 보이게 합니다.
    쓰기는 **local 로만** 갑니다 — 어느 스코프에 쓸지 호출자가 고르게 하면
    에이전트가 group/global 을 임의로 오염시킬 수 있습니다.

    Attributes:
        stores: 스코프별 저장소. 선언되지 않은 스코프는 없는 것으로 취급합니다.
        quotas: 스코프별 바이트 상한. 미선언 스코프는 상한 없음입니다.
    """

    stores: Mapping[ArtifactScope, FilesystemArtifactStore]
    quotas: Mapping[ArtifactScope, int] = field(default_factory=dict)
    writable: frozenset[ArtifactScope] = frozenset({ArtifactScope.LOCAL, ArtifactScope.GROUP})
    """쓰기가 허용된 스코프.

    09 의 memory ACL 과 같은 규약이다: **group 은 멤버 rw, global 은 기본
    ro** (전역 write 는 명시 허가). local 만 쓰게 막으면 01 이 규정한
    "그룹 산출물"에 아무도 쓸 수 없어 노드 간 전달이 성립하지 않는다.
    """

    async def put(
        self, key: str, data: bytes, *, scope: ArtifactScope = ArtifactScope.LOCAL
    ) -> str:
        """Store into a writable scope.

        쓰기가 허용된 스코프에 저장합니다. 기본은 ``local`` — 노드 간에
        넘길 산출물만 ``group`` 을 명시합니다.

        Args:
            key: Name within the scope.
            data: The bytes to store.
            scope: Target scope; defaults to the agent's own.

        Returns:
            The ``artifact://`` reference.

        Raises:
            AaestroError: FORBIDDEN/``ART_001`` if the scope is undeclared or
                read-only, STORAGE/``ART_002`` if the quota would be exceeded.
        """
        store = self.stores.get(scope)
        if store is None:
            raise denied(str(scope), "scope was not declared for this agent")
        if scope not in self.writable:
            # global 에 아무나 쓰면 전사 공용 산출물이 오염된다 (09 와 같은 규약)
            raise denied(str(scope), "scope is read-only for this agent")

        self._check_quota(scope, store, len(data))
        return await store.put(key, data)

    async def get(self, ref: str) -> bytes:
        """Read from whichever declared scope owns the reference.

        참조가 가리키는 스코프에서 읽습니다 — **선언된 스코프만** 봅니다.

        Args:
            ref: The ``artifact://`` reference.

        Returns:
            The stored bytes.

        Raises:
            AaestroError: FORBIDDEN/``ART_001`` if the reference names a scope
                this agent did not declare — 비멤버가 그룹 산출물을 읽으면
                artifact 가 사이드채널이 된다.
        """
        parsed = parse_ref(ref)
        for scope in RESOLUTION_ORDER:
            store = self.stores.get(scope)
            if store is not None and store.scope == parsed.scope:
                return await store.get(ref)

        raise denied(parsed.scope, "scope was not declared for this agent", ref=ref)

    def _check_quota(self, scope: ArtifactScope, store: FilesystemArtifactStore, size: int) -> None:
        """이 저장이 상한을 넘는지 — 넘으면 쓰기 전에 막는다."""
        limit = self.quotas.get(scope)
        if limit is None:
            return
        used = store.used_bytes()
        if used + size > limit:
            raise over_quota(str(scope), used=used, limit=limit, incoming=size)


__all__ = [
    "RESOLUTION_ORDER",
    "ArtifactScope",
    "ScopedArtifacts",
    "denied",
    "over_quota",
]
