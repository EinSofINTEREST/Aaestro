"""Assembles the Memory Service from repository declarations.

저장소 선언으로 Memory Service 를 조립한다.

`create_app` 은 **이미 조립된** 컴포넌트 넷을 요구한다 — 저장소·인덱스·검색·
토큰. 그것을 설정과 선언으로부터 만드는 곳이 여기다. 이 조립이 없으면 앱은
있어도 프로세스가 되지 못한다 (#181).

09 Access Enforcement 1 을 지키는 경계이기도 하다: **저장소 자격증명은 이
프로세스만 갖는다.** 에이전트는 불투명 토큰으로만 닿는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.memory.backend import create_store
from aaestro.memory.http import TokenRegistry, create_app
from aaestro.memory.providers import build_index_registry
from aaestro.memory.recall import Recall
from aaestro.memory.service import MemoryService
from aaestro.modules.memoryset import ChunkSpec, EmbeddingSpec, MemorysetLoader
from aaestro.modules.registry import ModuleRegistry
from aaestro.runtime.memory import issue_token

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI

    from aaestro.access.client import AccessClient
    from aaestro.config import AaestroConfig
    from aaestro.core.manifest import AgentManifest, GroupManifest
    from aaestro.memory.entry import MemoryEntry
    from aaestro.memory.store import MemoryStore
    from aaestro.observability.metrics import Metrics

log = structlog.get_logger(__name__)

GLOBAL_GROUP = "global"


@dataclass
class MemoryDeployment:
    """The assembled service and the tokens it issued.

    조립된 서비스와 그것이 발급한 토큰들.

    Attributes:
        app: The HTTP surface agents talk to.
        tokens: Opaque token per agent name — runtime 이 이것을 컨테이너에
            주입한다. 자격증명이 아니라 **범위**를 담은 불투명 문자열이다.
    """

    app: FastAPI
    tokens: dict[str, str] = field(default_factory=dict)
    indexer: Any = None
    """색인 큐 — **누군가 비워야** 저장한 기억이 검색된다 (09 Write Path)."""
    access: AccessClient | None = None
    """레지스트리 판정 클라이언트 — 있으면 **누군가 변경 알림을 따라가야** 캐시가 무효화된다."""


def _embedding_source(
    manifests: dict[str, AgentManifest],
    groups: dict[str, GroupManifest],
    loader: MemorysetLoader,
) -> tuple[EmbeddingSpec | None, ChunkSpec]:
    """Pick the embedding and chunk policy the indexes will use.

    인덱스가 쓸 embedding/chunk 정책을 고릅니다.

    **인덱스는 space 단위지만 embedder 는 하나다** — 서로 다른 벡터 공간을
    섞으면 검색이 조용히 망가진다 (09 Embedding Model Pinning). 그래서 선언된
    memoryset 중 첫 번째를 정본으로 삼고, 나머지가 다르면 경고한다.
    """
    refs = [
        *(space.ref for manifest in manifests.values() for space in manifest.spec.memory.spaces),
        *(space.ref for group in groups.values() for space in group.spec.memory.spaces),
    ]
    embedding: EmbeddingSpec | None = None
    chunk = ChunkSpec()
    chosen: str | None = None

    for ref in dict.fromkeys(refs):
        index = loader.load(ref).manifest.spec.index
        if embedding is None:
            embedding, chunk, chosen = index.embedding, index.chunk, ref
        elif index.embedding != embedding:
            # 재인덱싱 없이 섞으면 두 벡터 공간이 한 인덱스에 들어간다
            log.warning(
                "memoryset declares a different embedding",
                module_ref=ref,
                memory_space=chosen or "",
            )
    return embedding, chunk


def build_deployment(
    config: AaestroConfig,
    *,
    root: Path,
    metrics: Metrics | None = None,
    access: AccessClient | None = None,
) -> MemoryDeployment:
    """Assemble the Memory Service for a repository.

    저장소 하나에 대한 Memory Service 를 조립합니다.

    Args:
        config: Validated framework settings — 저장소 백엔드가 여기서 온다.
        root: Repository root holding ``agents/`` and ``groups/``.
        metrics: Collector for the memory metrics, when observability is on.
        access: Registry decision client. 있으면 토큰을 발급하지 않는다 — 에이전트는 control
            plane 이 발급한 신원을 내밀고, 권한은 요청마다 레지스트리가 판정한다 (09 Access
            Enforcement). 정적 토큰을 함께 발급하면 회수가 닿지 않는 뒷문이 된다.

    Returns:
        The app plus one token per declared agent (none in registry mode).
    """
    from aaestro.catalog import Catalog

    catalog = Catalog.under(root)
    agents, groups_found = catalog.agents(), catalog.groups()
    problems = [*agents.problems, *groups_found.problems]
    if problems:
        # 깨진 에이전트는 토큰을 못 받고, 깨진 그룹은 멀쩡한 에이전트의 group
        # space 를 지운다 — 불완전한 카탈로그로 서비스를 띄우지 않는다
        raise AaestroError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message="memory service cannot start with unreadable declarations",
            details={
                "problems": [
                    {"path": p.path, "code": p.code, "message": p.message} for p in problems
                ]
            },
        )
    manifests = agents.items
    groups = groups_found.items
    loader = MemorysetLoader(ModuleRegistry.under(root))

    embedding, chunk = _embedding_source(manifests, groups, loader)
    indexer = build_index_registry(embedding, metrics=metrics)

    service = MemoryService(store=create_store(config.memory), metrics=metrics)
    # 인덱스는 프로세스 메모리다 — 되읽지 않으면 재시작 전의 기억이 검색되지 않는다 (#312)
    indexer.warm(_stored_entries(service.store), chunk)
    recall = Recall(indexes=indexer.indexes, store=service.store, metrics=metrics)
    tokens = TokenRegistry()

    if access is not None:
        from aaestro.memory.gate import RegistryGate

        log.info("memory service assembled", mode="registry")
        return MemoryDeployment(
            app=create_app(
                service,
                recall,
                None,
                indexer=indexer,
                chunk=chunk,
                gate=RegistryGate(client=access, catalog=catalog),
            ),
            indexer=indexer,
            access=access,
        )

    issued: dict[str, str] = {}
    global_group = groups.get(GLOBAL_GROUP)
    for name, manifest in manifests.items():
        # 그룹 space 는 멤버에게만 — issue_token 이 소속을 검증한다
        group = groups.get(manifest.metadata.group or "")
        declared = issue_token(
            manifest,
            group=group,
            global_spaces=global_group.spec.memory if global_group else None,
        )
        issued[name] = tokens.issue(declared)

    log.info("memory service assembled", agent="", **{"agents": len(issued)})
    return MemoryDeployment(
        app=create_app(service, recall, tokens, indexer=indexer, chunk=chunk),
        tokens=issued,
        indexer=indexer,
    )


def _stored_entries(store: MemoryStore) -> Iterator[MemoryEntry]:
    """저장된 전체 항목 — space 별로 오래된 것부터 (저장 순서대로 색인한다)."""
    for space in store.spaces():
        yield from reversed(store.list_space(space, limit=None))


__all__ = ["GLOBAL_GROUP", "MemoryDeployment", "build_deployment"]
