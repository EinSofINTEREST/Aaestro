"""Resolving each graph node's model — manifest default, node override, deployment override.

노드마다 해석된 모델을 ``TaskConfig.model`` 로 싣기 위한 해석기. 해석 계층은
manifest ``spec.model`` → 노드 ``model`` → 배포 ``model_overrides`` 이며, 배포 계층은
토폴로지에 먼저 입혀진다 (`GraphTopology.with_model_overrides`) — 여기서는 노드에
남은 override 를 manifest 위에 올리기만 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aaestro.core.manifest import ModelOverride, agent_name

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aaestro.catalog import Catalog
    from aaestro.core.manifest import ModelConfig
    from aaestro.core.topology import GraphTopology, NodeSpec


def resolve_node_model(catalog: Catalog, node: NodeSpec) -> ModelConfig | None:
    """Resolve the model a node's task runs with.

    노드 태스크가 쓸 모델 — 서브그래프 노드는 모델을 고를 에이전트가 없어 None.

    Raises:
        AaestroError: NOT_FOUND if the agent manifest is missing.
        pydantic.ValidationError: If the layered declaration is invalid.
    """
    if node.agent is None:
        return None
    return catalog.agent(agent_name(node.agent)).spec.model.overridden(node.model)


@dataclass(frozen=True)
class CatalogModels:
    """A ``ModelResolver`` that reads manifests from the catalog.

    매 노드 실행마다 catalog 를 읽는다 — `EgressBaseline` 과 같은 선택이다. 캐시하면
    재배포 없이 바뀐 선언을 어느 시점에 반영하는지가 하나 더 생긴다.
    """

    catalog: Catalog

    def __call__(self, node: NodeSpec) -> ModelConfig | None:
        return resolve_node_model(self.catalog, node)


def with_recorded_overrides(
    topology: GraphTopology, recorded: Mapping[str, Mapping[str, Any]]
) -> GraphTopology:
    """Layer overrides kept in a deployment or run record onto a topology.

    기록(배포 · run)에 JSON 형태로 남은 override 를 토폴로지에 입힌다. 기록은
    ``exclude_unset`` 으로 덤프돼 있어, 다시 읽어도 "명시한 키만 덮는다" 가 유지된다.

    Raises:
        AaestroError: VALIDATION/``VAL_002`` if a node id is no longer in the graph.
    """
    if not recorded:
        return topology
    overrides = {node_id: ModelOverride.model_validate(o) for node_id, o in recorded.items()}
    return topology.with_model_overrides(overrides)


__all__ = ["CatalogModels", "resolve_node_model", "with_recorded_overrides"]
