"""Topology validation benchmark.

프레임워크 오버헤드 회귀 감시 — 검증은 배포 경로에 있으므로 노드가 늘어도
선형에 가깝게 유지되어야 한다.
"""

from __future__ import annotations

from typing import Any

from aaestro.core.topology import GraphTopology, validate_topology

NODE_COUNT = 50


def _large_topology(node_count: int = NODE_COUNT) -> GraphTopology:
    """선형 체인 + 조건부 분기를 가진 대형 그래프."""
    nodes: list[dict[str, Any]] = [
        {"id": f"n{i}", "agent": f"agents/worker-{i}@0.1.0"} for i in range(node_count)
    ]
    edges: list[dict[str, Any]] = [{"from": "START", "to": "n0"}]
    edges += [{"from": f"n{i}", "to": f"n{i + 1}"} for i in range(node_count - 1)]
    edges.append({"from": f"n{node_count - 1}", "to": "END"})

    return GraphTopology.model_validate(
        {
            "apiVersion": "aaestro/v1",
            "kind": "Graph",
            "metadata": {"name": "large-graph", "version": "1.0.0"},
            "spec": {
                "mode": "mission",
                "goal": "대형 그래프 검증 벤치마크",
                "state": {"schema": "aaestro.graphs.schemas:ResearchState"},
                "nodes": nodes,
                "edges": edges,
            },
        }
    )


def test_bench_topology_validation(benchmark):
    topology = _large_topology()

    benchmark(validate_topology, topology)


def test_large_topology_validates():
    """벤치마크 대상이 실제로 유효한 그래프인지 확인한다."""
    validate_topology(_large_topology())
