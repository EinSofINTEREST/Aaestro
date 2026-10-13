"""Tests for ref resolution and subgraph cycle detection in topology validation."""

from __future__ import annotations

import pytest

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.topology import GraphTopology, validate_topology
from aaestro.modules.registry import ModuleRegistry
from tests.fixtures.topologies import make_mission


class FakeRegistry:
    """해석 가능한 ref 집합만 아는 최소 레지스트리 대역."""

    def __init__(self, known: set[str]) -> None:
        self._known = known
        self.resolved: list[str] = []

    def resolve(self, ref: str) -> object:
        self.resolved.append(ref)
        if ref not in self._known:
            raise AaestroError(
                category=ErrorCategory.MODULE,
                code=ErrorCode.MOD_001,
                message=f"cannot resolve: {ref}",
            )
        return object()


def test_resolvable_agent_refs_pass():
    registry = FakeRegistry({"agents/planner@0.1.0", "agents/researcher@0.1.0"})

    validate_topology(make_mission(), registry=registry)  # type: ignore[arg-type]

    assert registry.resolved == ["agents/planner@0.1.0", "agents/researcher@0.1.0"]


def test_unresolvable_agent_ref_is_rejected():
    registry = FakeRegistry({"agents/planner@0.1.0"})

    with pytest.raises(AaestroError) as exc_info:
        validate_topology(make_mission(), registry=registry)  # type: ignore[arg-type]

    assert exc_info.value.code == "GRAPH_001"
    assert "cannot resolve node ref" in exc_info.value.message
    # 원인 체인이 모듈 레이어 에러로 이어진다
    assert isinstance(exc_info.value.__cause__, AaestroError)


def test_registry_is_optional():
    """레지스트리를 주지 않으면 ref 해석 검사를 건너뛴다 (스키마 단독 검증)."""
    validate_topology(make_mission())


def _graph_with_subgraph(name: str, child_ref: str | None) -> GraphTopology:
    """서브그래프 노드를 가진 그래프를 만든다."""
    nodes: list[dict[str, object]] = [{"id": "work", "agent": "agents/worker@0.1.0"}]
    edges: list[dict[str, object]] = [
        {"from": "START", "to": "work"},
        {"from": "work", "to": "END"},
    ]
    if child_ref is not None:
        nodes.append({"id": "child", "graph": child_ref})
        edges = [
            {"from": "START", "to": "work"},
            {"from": "work", "to": "child"},
            {"from": "child", "to": "END"},
        ]

    return GraphTopology.model_validate(
        {
            "apiVersion": "aaestro/v1",
            "kind": "Graph",
            "metadata": {"name": name, "version": "1.0.0"},
            "spec": {
                "mode": "mission",
                "goal": "테스트",
                "state": {"schema": "aaestro.graphs.schemas:ResearchState"},
                "nodes": nodes,
                "edges": edges,
            },
        }
    )


def test_acyclic_subgraph_passes():
    leaf = _graph_with_subgraph("leaf", None)
    parent = _graph_with_subgraph("parent", "graphs/leaf@1.0.0")

    validate_topology(parent, load_subgraph=lambda ref: leaf)


def test_direct_subgraph_cycle_is_rejected():
    """자기 자신을 서브그래프로 참조하는 그래프는 차단된다."""
    parent = _graph_with_subgraph("parent", "graphs/parent@1.0.0")

    with pytest.raises(AaestroError) as exc_info:
        validate_topology(parent, load_subgraph=lambda ref: parent)

    assert exc_info.value.code == "GRAPH_001"
    assert "subgraph cycle detected" in exc_info.value.message


def test_indirect_subgraph_cycle_is_rejected():
    """A -> B -> A 형태의 간접 순환도 차단된다."""
    a = _graph_with_subgraph("a", "graphs/b@1.0.0")
    b = _graph_with_subgraph("b", "graphs/a@1.0.0")
    loaded = {"graphs/a@1.0.0": a, "graphs/b@1.0.0": b}

    with pytest.raises(AaestroError) as exc_info:
        validate_topology(a, load_subgraph=lambda ref: loaded[ref])

    assert "subgraph cycle detected" in exc_info.value.message


def test_registry_under_repo_root_resolves_graph_refs(tmp_path):
    """실제 레지스트리로 그래프 ref 해석 경로가 이어지는지 확인한다."""
    graphs = tmp_path / "graphs"
    graphs.mkdir()
    (graphs / "sub-review.yaml").write_text("apiVersion: aaestro/v1\nkind: Graph\n")

    registry = ModuleRegistry.under(tmp_path)
    resolved = registry.resolve("graphs/sub-review@1.0.0")

    assert resolved.name == "sub-review"
