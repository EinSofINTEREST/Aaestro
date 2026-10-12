"""Graph topology builders for tests.

토폴로지 테스트 빌더 — 기본값은 유효한 최소 그래프이고, 필요한 부분만 override 한다.
"""

from __future__ import annotations

from typing import Any

from aaestro.orchestrator.topology import GraphTopology

_STATE_REF = "aaestro.graphs.schemas:ResearchState"
_CONDITION_REF = "aaestro.graphs.conditions:needs_research"


def mission_dict(**spec_overrides: Any) -> dict[str, Any]:
    """Build a raw mission-mode topology mapping.

    유효한 mission 토폴로지 매핑을 만듭니다 (YAML 로드 결과 형태).
    """
    spec: dict[str, Any] = {
        "mode": "mission",
        "goal": "질의를 받아 리서치 보고서를 완성한다",
        "state": {"schema": _STATE_REF},
        "nodes": [
            {
                "id": "planner",
                "agent": "agents/planner@0.1.0",
                "input_map": {"query": "state.query"},
            },
            {"id": "researcher", "agent": "agents/researcher@0.1.0"},
        ],
        "edges": [
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "researcher"},
            {"from": "researcher", "to": "END"},
        ],
    }
    spec.update(spec_overrides)
    return {
        "apiVersion": "aaestro/v1",
        "kind": "Graph",
        "metadata": {"name": "research-pipeline", "version": "1.0.0"},
        "spec": spec,
    }


def service_dict(**spec_overrides: Any) -> dict[str, Any]:
    """Build a raw service-mode topology mapping.

    유효한 service 토폴로지 매핑을 만듭니다 — idle 정책 포함.
    한 iteration 은 START -> END 로 끝난다 (반복은 ServiceRunner 소유).
    """
    spec: dict[str, Any] = {
        "mode": "service",
        "goal": "피드를 상시 감시하고 신규 항목을 알린다",
        "state": {"schema": "aaestro.graphs.schemas:FeedMonitorState"},
        "service": {
            "idle": {"min_delay_s": 30, "max_delay_s": 600},
            "max_failure_streak": 5,
        },
        "nodes": [
            {"id": "watcher", "agent": "agents/feed-watcher@0.1.0"},
            {"id": "notifier", "agent": "agents/notifier@0.1.0"},
        ],
        "edges": [
            {"from": "START", "to": "watcher"},
            {
                "from": "watcher",
                "to": "notifier",
                "condition": "aaestro.graphs.conditions:has_new_items",
            },
            {"from": "watcher", "to": "END", "condition": "aaestro.graphs.conditions:idle"},
            {"from": "notifier", "to": "END"},
        ],
    }
    spec.update(spec_overrides)
    return {
        "apiVersion": "aaestro/v1",
        "kind": "Graph",
        "metadata": {"name": "feed-monitor", "version": "1.0.0"},
        "spec": spec,
    }


def make_mission(**spec_overrides: Any) -> GraphTopology:
    """Build a validated mission topology object."""
    return GraphTopology.model_validate(mission_dict(**spec_overrides))


def make_service(**spec_overrides: Any) -> GraphTopology:
    """Build a validated service topology object."""
    return GraphTopology.model_validate(service_dict(**spec_overrides))


def condition_ref() -> str:
    """조건 함수 ref — 테스트가 참조하는 표준 값."""
    return _CONDITION_REF
