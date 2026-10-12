"""Unit tests for the agent contract models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aaestro.core.agent import (
    ComponentHealth,
    HealthState,
    HealthStatus,
    ModelUsage,
    TaskResult,
    TaskStatus,
    TraceContext,
)
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from tests.fixtures.builders import make_task


def test_graph_task_selects_node_template():
    task = make_task(node_id="research")

    assert task.is_direct is False
    assert task.template_name == "research"


def test_direct_task_selects_default_template():
    """direct 요청은 node_id 가 없으므로 default 템플릿 — 02 Direct Request Rules 2."""
    task = make_task(node_id=None, run_id="direct-abc")

    assert task.is_direct is True
    assert task.template_name == "default"


def test_task_request_is_frozen():
    task = make_task()

    with pytest.raises(ValidationError):
        task.task_id = "other"  # type: ignore[misc]


def test_completed_result_carries_task_id_and_output():
    task = make_task()

    result = TaskResult.completed(task, output={"findings": ["a"]})

    assert result.task_id == task.task_id
    assert result.status is TaskStatus.COMPLETED
    assert result.output == {"findings": ["a"]}
    assert result.error is None


def test_failed_result_carries_error_payload():
    task = make_task()
    err = AaestroError(
        category=ErrorCategory.MODEL,
        code=ErrorCode.LLM_005,
        message="max turns exceeded",
        task_id=task.task_id,
    )

    result = TaskResult.failed(task, err)

    assert result.status is TaskStatus.FAILED
    assert result.error is not None
    assert result.error.code == "LLM_005"
    assert result.error.category is ErrorCategory.MODEL


def test_failed_result_accepts_payload_directly():
    task = make_task()
    payload = AaestroError(
        category=ErrorCategory.TIMEOUT, code=ErrorCode.TO_001, message="task timeout"
    ).payload()

    result = TaskResult.failed(task, payload)

    assert result.error == payload


def test_canceled_result():
    task = make_task()

    result = TaskResult.canceled(task)

    assert result.status is TaskStatus.CANCELED


def test_usage_merge_accumulates_tokens():
    first = ModelUsage(input_tokens=10, output_tokens=5, model="claude-sonnet-5")
    second = ModelUsage(input_tokens=3, output_tokens=7)

    merged = first.merge(second)

    assert merged.input_tokens == 13
    assert merged.output_tokens == 12
    assert merged.model == "claude-sonnet-5"


def test_trace_child_increments_depth():
    """A2A 위임 깊이 추적 — 상한 초과 시 A2A_005 판정의 근거."""
    root = TraceContext(trace_id="t-1")

    child = root.child("span-1")
    grandchild = child.child("span-2")

    assert (root.depth, child.depth, grandchild.depth) == (0, 1, 2)
    assert grandchild.trace_id == "t-1"


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([HealthState.HEALTHY, HealthState.HEALTHY], HealthState.HEALTHY),
        ([HealthState.HEALTHY, HealthState.DEGRADED], HealthState.DEGRADED),
        ([HealthState.DEGRADED, HealthState.UNHEALTHY], HealthState.UNHEALTHY),
        ([HealthState.HEALTHY, HealthState.UNHEALTHY], HealthState.UNHEALTHY),
    ],
)
def test_health_aggregation(states, expected):
    components = {f"c{i}": ComponentHealth(state=state) for i, state in enumerate(states)}

    status = HealthStatus.aggregate(components)

    assert status.status is expected


def test_health_aggregate_with_no_components_is_healthy():
    assert HealthStatus.aggregate({}).status is HealthState.HEALTHY
