"""Scoped artifact access.

01 은 artifact 를 **global / group / local** 3계층 리소스로 규정한다. 이 모듈이
그 경계를 강제한다 — 없으면 artifact 가 graph state 를 우회하는 사이드채널이
된다 (01 은 그런 경로를 금지하며 예외는 선언된 memory space 뿐이다).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from aaestro.artifacts.store import parse_ref
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aaestro.artifacts.store import FilesystemArtifactStore

log = structlog.get_logger(__name__)


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
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    """quota 검사부터 쓰기 완료까지를 묶는 락 — 이 객체가 소유한다.

    쓰기가 스레드로 넘어가면 동시에 온 두 저장이 둘 다 같은 사용량을 보고 검사를 통과해
    상한을 넘는다. 보호 대상은 스코프별 사용량 (디스크) 이고, 이 객체가 발급한 쓰기만
    직렬화된다 — 같은 스코프 디렉토리를 쓰는 다른 객체 (다른 에이전트 프로세스) 의 쓰기는
    이 락으로 막지 못한다 (#679). 취소된 저장도 쓰기가 끝날 때 놓는다.
    """
    """쓰기가 허용된 스코프.

    09 의 memory ACL 과 같은 규약이다: **group 은 멤버 rw, global 은 기본
    ro** (전역 write 는 명시 허가). local 만 쓰게 막으면 01 이 규정한
    "그룹 산출물"에 아무도 쓸 수 없어 노드 간 전달이 성립하지 않는다.
    """
    _writes: set[asyncio.Future[str]] = field(default_factory=set, init=False, repr=False)
    """이 객체가 소유한 진행 중 쓰기 태스크 — 호출자가 취소된 뒤에도 락 해제를 위해 쥔다."""

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

        await self._write_lock.acquire()
        try:
            await self._check_quota(scope, store, len(data))
            write = asyncio.ensure_future(store.put(key, data))
        except BaseException:
            self._write_lock.release()
            raise
        # 호출자가 취소돼도 쓰기 스레드는 계속 돈다 — 락은 쓰기가 끝날 때 놓아야
        # 다음 저장이 반영 전 사용량으로 quota 를 통과하지 못한다
        self._writes.add(write)
        write.add_done_callback(self._finish_write)
        return await asyncio.shield(write)

    def _finish_write(self, write: asyncio.Future[str]) -> None:
        """쓰기 태스크의 소유자 — 락을 놓고, 호출자가 떠난 뒤의 실패도 남긴다."""
        self._writes.discard(write)
        self._write_lock.release()
        if not write.cancelled() and (err := write.exception()) is not None:
            log.warning("artifact write failed", error_type=type(err).__name__)

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

    async def _check_quota(
        self, scope: ArtifactScope, store: FilesystemArtifactStore, size: int
    ) -> None:
        """이 저장이 상한을 넘는지 — 넘으면 쓰기 전에 막는다."""
        limit = self.quotas.get(scope)
        if limit is None:
            return
        used = await asyncio.to_thread(store.used_bytes)
        if used + size > limit:
            raise over_quota(str(scope), used=used, limit=limit, incoming=size)


__all__ = [
    "RESOLUTION_ORDER",
    "ArtifactScope",
    "ScopedArtifacts",
    "denied",
    "over_quota",
]
