"""Agent-execution metric wiring tests.

메트릭은 대시보드·알림이 의존하는 운영 계약이다 — 라벨과 증가 조건을 실제
실행 경로로 검증한다. registry 는 테스트마다 격리한다 (프로세스 전역 오염 금지).
"""

from __future__ import annotations

import pytest
from prometheus_client import CollectorRegistry

from malkuth.agentd.executor import Executor, ExecutorConfig, ExecutorServices
from malkuth.agentd.telemetry import ExecutorTelemetry, tool_source
from malkuth.core.agent import TaskStatus, TraceContext
from malkuth.core.errors import ErrorCategory, ErrorCode, MalkuthError
from malkuth.observability.metrics import Metrics
from tests.fixtures.builders import make_task
from tests.fixtures.fake_model import FakeModel, FakeTools, calls, text


def rate_limited() -> MalkuthError:
    """provider 가 429 를 낸 상황 — anthropic 어댑터가 내는 것과 같은 모양."""
    return MalkuthError(
        category=ErrorCategory.RATE_LIMIT,
        code=ErrorCode.LLM_001,
        message="provider rate limited",
        agent="researcher",
        retryable=True,
    )


MCP_TOOL = "mcp__filesystem__read_file"
SKILL_TOOL = "search"


def make_metrics() -> Metrics:
    """이 테스트만의 registry — 다른 테스트의 카운터와 섞이지 않는다."""
    return Metrics(registry=CollectorRegistry())


def make_executor(
    responses,
    *,
    metrics: Metrics | None = None,
    tools: FakeTools | None = None,
    **config,
) -> tuple[Executor, FakeTools]:
    """계측을 물린(또는 물리지 않은) executor."""
    registry = tools or FakeTools()
    telemetry = (
        None
        if metrics is None
        else ExecutorTelemetry(
            metrics,
            agent="researcher",
            group="research",
            provider="anthropic",
            model="claude-sonnet-5",
        )
    )
    executor = Executor(
        agent="researcher",
        model=FakeModel(responses),
        tools=registry,
        render=lambda task: f"prompt:{task.template_name}",
        config=ExecutorConfig(**config) if config else None,
        services=ExecutorServices(telemetry=telemetry),
    )
    return executor, registry


GRAPH_TRACE = TraceContext(trace_id="trace-0001", graph="research-pipeline")
"""그래프 run 이 만든 태스크 — 오케스트레이터가 graph 를 실어 보낸다 (#113)."""


def value(metrics: Metrics, name: str, **labels: str) -> float:
    """해당 라벨 조합의 현재 값 — 미기록이면 0.0."""
    return metrics.registry.get_sample_value(name, labels) or 0.0


# --- 태스크 카운터와 latency -------------------------------------------------


async def test_completed_task_is_counted_with_its_labels():
    metrics = make_metrics()
    executor, _ = make_executor([text("done")], metrics=metrics)

    result = await executor.execute(make_task(trace=GRAPH_TRACE))

    assert result.status is TaskStatus.COMPLETED
    assert (
        value(
            metrics,
            "malkuth_agent_tasks_total",
            agent="researcher",
            group="research",
            graph="research-pipeline",
            status="completed",
        )
        == 1.0
    )


async def test_failed_task_is_counted_under_the_failed_status():
    """AgentHighFailureRate 알림이 이 라벨 조합에 의존한다."""
    metrics = make_metrics()
    tools = FakeTools()
    tools.fail(SKILL_TOOL, RuntimeError("boom"))
    executor, _ = make_executor([calls(SKILL_TOOL)], metrics=metrics, tools=tools)

    result = await executor.execute(make_task(trace=GRAPH_TRACE))

    assert result.status is TaskStatus.FAILED
    assert (
        value(
            metrics,
            "malkuth_agent_tasks_total",
            agent="researcher",
            group="research",
            graph="research-pipeline",
            status="failed",
        )
        == 1.0
    )


async def test_task_duration_is_observed_once_per_task():
    metrics = make_metrics()
    executor, _ = make_executor([text("done")], metrics=metrics)

    await executor.execute(make_task(trace=GRAPH_TRACE))

    assert (
        value(
            metrics,
            "malkuth_agent_task_duration_seconds_count",
            agent="researcher",
            group="research",
            graph="research-pipeline",
        )
        == 1.0
    )


# --- 모델 호출과 토큰 --------------------------------------------------------


async def test_model_tokens_are_split_by_direction():
    metrics = make_metrics()
    executor, _ = make_executor([text("done", input_tokens=120, output_tokens=45)], metrics=metrics)

    await executor.execute(make_task(trace=GRAPH_TRACE))

    assert (
        value(
            metrics,
            "malkuth_model_tokens_total",
            agent="researcher",
            model="claude-sonnet-5",
            direction="input",
        )
        == 120.0
    )
    assert (
        value(
            metrics,
            "malkuth_model_tokens_total",
            agent="researcher",
            model="claude-sonnet-5",
            direction="output",
        )
        == 45.0
    )


async def test_each_model_turn_is_counted():
    """tool 을 한 번 부르면 모델은 두 턴 돈다."""
    metrics = make_metrics()
    executor, _ = make_executor([calls(SKILL_TOOL), text("done")], metrics=metrics)

    await executor.execute(make_task(trace=GRAPH_TRACE))

    assert (
        value(
            metrics,
            "malkuth_model_requests_total",
            agent="researcher",
            provider="anthropic",
            model="claude-sonnet-5",
            status="completed",
            fallback_index="0",
        )
        == 2.0
    )


async def test_model_failure_is_counted_before_it_propagates():
    """provider 장애가 요청 카운터에 남지 않으면 ModelRateLimited 류 알림이 침묵한다."""
    metrics = make_metrics()
    executor, _ = make_executor([RuntimeError("provider down")], metrics=metrics)

    await executor.execute(make_task(trace=GRAPH_TRACE))

    assert (
        value(
            metrics,
            "malkuth_model_requests_total",
            agent="researcher",
            provider="anthropic",
            model="claude-sonnet-5",
            status="failed",
            fallback_index="0",
        )
        == 1.0
    )


async def test_rate_limit_is_counted_under_its_own_status():
    """`ModelRateLimited` 알림은 **이 값으로** 필터한다.

    failed 로 뭉개면 알림이 영원히 침묵한다 — 05 status 표가 명시적으로
    경고하는 상황이다.
    """
    metrics = make_metrics()
    executor, _ = make_executor([rate_limited()], metrics=metrics)

    await executor.execute(make_task(trace=GRAPH_TRACE))

    assert (
        value(
            metrics,
            "malkuth_model_requests_total",
            agent="researcher",
            provider="anthropic",
            model="claude-sonnet-5",
            status="rate_limited",
            fallback_index="0",
        )
        == 1.0
    )


async def test_rate_limit_does_not_also_count_as_failed():
    """두 번 세면 실패율 알림이 rate limit 때마다 함께 울린다."""
    metrics = make_metrics()
    executor, _ = make_executor([rate_limited()], metrics=metrics)

    await executor.execute(make_task(trace=GRAPH_TRACE))

    assert (
        value(
            metrics,
            "malkuth_model_requests_total",
            agent="researcher",
            provider="anthropic",
            model="claude-sonnet-5",
            status="failed",
            fallback_index="0",
        )
        == 0.0
    )


# --- tool 호출 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "expected_source"),
    [(SKILL_TOOL, "skillset"), (MCP_TOOL, "mcp")],
)
def test_tool_source_is_read_from_the_namespace(tool, expected_source):
    assert tool_source(tool) == expected_source


async def test_skillset_and_mcp_tools_are_counted_separately():
    metrics = make_metrics()
    executor, _ = make_executor([calls(SKILL_TOOL, MCP_TOOL), text("done")], metrics=metrics)

    await executor.execute(make_task(trace=GRAPH_TRACE))

    assert (
        value(
            metrics,
            "malkuth_tool_calls_total",
            agent="researcher",
            source="skillset",
            tool=SKILL_TOOL,
            status="completed",
        )
        == 1.0
    )
    assert (
        value(
            metrics,
            "malkuth_tool_calls_total",
            agent="researcher",
            source="mcp",
            tool=MCP_TOOL,
            status="completed",
        )
        == 1.0
    )


async def test_failed_tool_is_counted_as_failed():
    metrics = make_metrics()
    tools = FakeTools()
    tools.fail(MCP_TOOL, RuntimeError("transport lost"))
    executor, _ = make_executor([calls(MCP_TOOL)], metrics=metrics, tools=tools)

    await executor.execute(make_task(trace=GRAPH_TRACE))

    assert (
        value(
            metrics,
            "malkuth_tool_calls_total",
            agent="researcher",
            source="mcp",
            tool=MCP_TOOL,
            status="failed",
        )
        == 1.0
    )


async def test_timed_out_tool_is_counted_as_failed():
    """tool timeout 은 흔한 실패 모드다 — 카운터에서 빠지면 안 된다."""
    metrics = make_metrics()
    tools = FakeTools()
    tools.script(SKILL_TOOL, delay=10.0)
    executor, _ = make_executor(
        [calls(SKILL_TOOL)], metrics=metrics, tools=tools, tool_timeout_s=0.01
    )

    await executor.execute(make_task(trace=GRAPH_TRACE))

    assert (
        value(
            metrics,
            "malkuth_tool_calls_total",
            agent="researcher",
            source="skillset",
            tool=SKILL_TOOL,
            status="failed",
        )
        == 1.0
    )


# --- 계측은 선택적이다 ------------------------------------------------------


async def test_execution_works_without_any_metrics():
    """메트릭을 주입하지 않아도 전 경로가 무오류로 동작해야 한다."""
    tools = FakeTools()
    tools.fail(SKILL_TOOL, RuntimeError("boom"))
    executor, _ = make_executor([calls(SKILL_TOOL), text("done")], tools=tools)

    result = await executor.execute(make_task(trace=GRAPH_TRACE))

    assert result.status is TaskStatus.FAILED


async def test_streaming_without_metrics_still_streams():
    executor, _ = make_executor([text("done")])

    events = [event async for event in executor.stream(make_task())]

    assert events


async def test_sibling_tools_are_counted_when_one_fails():
    """한 tool 이 실패해 나머지가 취소되면 그 시도는 카운터에서 사라진다."""
    metrics = make_metrics()
    tools = FakeTools()
    tools.fail(SKILL_TOOL, RuntimeError("boom"))
    tools.script(MCP_TOOL, delay=0.05)
    executor, _ = make_executor([calls(SKILL_TOOL, MCP_TOOL)], metrics=metrics, tools=tools)

    await executor.execute(make_task(trace=GRAPH_TRACE))

    recorded = value(
        metrics,
        "malkuth_tool_calls_total",
        agent="researcher",
        source="mcp",
        tool=MCP_TOOL,
        status="completed",
    )
    assert recorded == 1.0


# --- graph 라벨 (#113) ---------------------------------------------------------


async def test_direct_requests_are_labelled_direct():
    """빈 문자열이면 '그래프 없음'과 '라벨을 못 채움'이 구분되지 않는다."""
    metrics = make_metrics()
    executor, _ = make_executor([text("done")], metrics=metrics)

    await executor.execute(make_task(node_id=None))

    assert (
        value(
            metrics,
            "malkuth_agent_tasks_total",
            agent="researcher",
            group="research",
            graph="direct",
            status="completed",
        )
        == 1.0
    )


async def test_the_graph_label_is_never_empty():
    """빈 라벨은 대시보드의 그래프별 집계를 무의미하게 만든다 (#113)."""
    metrics = make_metrics()
    executor, _ = make_executor([text("done")], metrics=metrics)

    await executor.execute(make_task())

    assert (
        value(
            metrics,
            "malkuth_agent_tasks_total",
            agent="researcher",
            group="research",
            graph="",
            status="completed",
        )
        == 0.0
    )


async def test_behaviour_does_not_depend_on_the_graph_name():
    """02 Rule 6 — 에이전트는 배선을 가정하지 않는다. 라벨로만 쓴다."""
    one, _ = make_executor([text("done")])
    other, _ = make_executor([text("done")])

    mine = await one.execute(make_task(trace=TraceContext(trace_id="t", graph="research-pipeline")))
    theirs = await other.execute(make_task(trace=TraceContext(trace_id="t", graph="feed-monitor")))

    assert mine.output == theirs.output
    assert mine.status == theirs.status
