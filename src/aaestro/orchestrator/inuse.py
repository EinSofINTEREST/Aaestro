"""Which declarations a running run depends on.

authoring 은 배포 중인 선언을 지우거나 덮어쓰지 못하게 `in_use` 를 묻는다 (#242).
프로덕션 `Author` 에 그 술어가 비어 있었다 — 실행 중 run 의 그래프와 에이전트가
발밑에서 바뀔 수 있었다. 여기서는 **run 저장소**를 본다. 배포(#243)는 또 다른
원천이고, 그 둘을 OR 로 묶는 것이 최종 형태다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from aaestro.authoring import InUse
from aaestro.core.errors import AaestroError
from aaestro.core.manifest import agent_name
from aaestro.orchestrator.run import RunStatus

if TYPE_CHECKING:
    from aaestro.catalog import Catalog
    from aaestro.orchestrator.runstore import RunStore

log = structlog.get_logger(__name__)

ACTIVE = frozenset({str(RunStatus.RUNNING), str(RunStatus.DRAINING)})
"""드레인 중도 아직 그 그래프로 돈다 — 끝날 때까지 선언은 고정이다."""


def run_backed(store: RunStore, catalog: Catalog) -> InUse:
    """Build an ``in_use`` predicate from live runs.

    실행 중 run 이 참조하는 그래프와, 그 그래프의 노드가 쓰는 에이전트를 사용 중으로 본다.
    """

    def in_use(kind: str, name: str) -> bool:
        active_graphs = {r.graph for r in store.list() if r.status in ACTIVE}
        if kind == "graph":
            return name in active_graphs
        for graph_name in active_graphs:
            try:
                graph = catalog.graph(graph_name)
            except AaestroError as err:
                # 실행 중 run 의 그래프가 안 읽힌다 — 그 자체가 운영자가 알아야 할 일이다.
                # 여기서는 보호를 포기하지 않고 그 그래프만 건너뛴다
                log.warning(
                    "running graph cannot be read for in-use check",
                    graph=graph_name,
                    error_code=err.code,
                )
                continue
            if any(n.agent is not None and agent_name(n.agent) == name for n in graph.spec.nodes):
                return True
        return False

    return in_use


def any_of(*predicates: InUse) -> InUse:
    """실행 중 run 과 배포 — 어느 쪽이든 쓰고 있으면 사용 중이다."""

    def in_use(kind: str, name: str) -> bool:
        return any(p(kind, name) for p in predicates)

    return in_use


__all__ = ["ACTIVE", "any_of", "run_backed"]
