"""Shared test builders.

테스트용 객체 빌더 — 기본값은 유효한 최소 구성이고, 필요한 필드만 override 한다.
"""

from __future__ import annotations

from typing import Any

from aaestro.core.agent import TaskConfig, TaskRequest, TraceContext
from aaestro.core.manifest import AgentManifest


def _merge_mapping(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """중첩 매핑을 재귀 병합한다 — 호출자가 필요한 필드만 override 하도록."""
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_mapping(merged[key], value)
        else:
            merged[key] = value
    return merged


def manifest_dict(**overrides: Any) -> dict[str, Any]:
    """Build a raw manifest mapping (as loaded from YAML).

    YAML 로부터 로드된 형태의 manifest 매핑을 만듭니다 — 스키마 검증
    테스트가 원시 입력을 다루기 위해 사용합니다.
    """
    base: dict[str, Any] = {
        "apiVersion": "aaestro/v1",
        "kind": "Agent",
        "metadata": {"name": "test-agent", "version": "0.1.0"},
        "spec": {
            "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
            "promptset": {"ref": "promptsets/test@0.1.0"},
        },
    }
    return _merge_mapping(base, overrides)


def make_manifest(**overrides: Any) -> AgentManifest:
    """Build a valid agent manifest.

    유효한 에이전트 manifest 를 만듭니다.
    """
    return AgentManifest.model_validate(manifest_dict(**overrides))


def make_task(**overrides: Any) -> TaskRequest:
    """Build a task request.

    태스크 요청을 만듭니다.
    """
    base: dict[str, Any] = {
        "task_id": "task-0001",
        "run_id": "run-0001",
        "node_id": "planner",
        "input": {"query": "test"},
        "config": TaskConfig(),
        "trace": TraceContext(trace_id="trace-0001"),
    }
    base.update(overrides)
    return TaskRequest.model_validate(base)
