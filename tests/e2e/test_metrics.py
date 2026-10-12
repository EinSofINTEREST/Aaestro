"""Metrics filled by a real run.

#79 가 메트릭을 각 계층에 배선했지만 **실제 run 이 그 값을 채우는지**는
검증되지 않았다 (#165). 문서·라벨 일치는 가드 테스트로 고정돼 있어도,
카운터가 0 인 채로 두면 아무도 모른다.

05: "코드가 내지 않는 값을 알림이 보면 **영원히 침묵한다**"
"""

from __future__ import annotations

import re
import urllib.request

import pytest

from aaestro.orchestrator.run import RunStatus
from tests.e2e.test_graph_run import (  # noqa: F401 — fixture 재사용
    graph_stack,
    node_runtime,
    submitter,
    topology,
)
from tests.e2e.test_stack import requires_docker

pytestmark = pytest.mark.e2e

METRIC_PORTS = {"planner": 19082, "researcher": 19083, "writer": 19084}


def scraped(port: int) -> str:
    """에이전트가 노출한 Prometheus 텍스트."""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=10) as response:  # noqa: S310
        return response.read().decode()


def samples(text: str, name: str) -> list[tuple[str, float]]:
    """``name{labels} value`` 행을 (라벨, 값) 으로 — HELP/TYPE 주석은 건너뛴다."""
    found = []
    for line in text.splitlines():
        if line.startswith("#") or not line.startswith(name):
            continue
        match = re.match(rf"^{re.escape(name)}(\{{(?P<labels>[^}}]*)\}})?\s+(?P<value>\S+)$", line)
        if match:
            found.append((match.group("labels") or "", float(match.group("value"))))
    return found


@pytest.fixture
async def after_a_mission(node_runtime):  # noqa: F811 — fixture 주입
    """mission run 을 한 번 돌린 뒤의 스택."""
    result = await submitter(node_runtime).submit(
        topology("research-pipeline"), {"query": "왜 하늘은 파란가"}, run_id="e2e-metrics"
    )
    assert result.status is RunStatus.COMPLETED, "메트릭을 보기 전에 run 이 성공해야 한다"
    return result


@requires_docker
async def test_agent_task_counter_is_not_zero(after_a_mission):
    """run 이 돌았는데 카운터가 0 이면 알림이 영원히 침묵한다."""
    completed = [
        value
        for labels, value in samples(scraped(METRIC_PORTS["planner"]), "aaestro_agent_tasks_total")
        if 'status="completed"' in labels
    ]

    assert completed, "aaestro_agent_tasks_total 에 completed 표본이 없다"
    assert sum(completed) > 0


@requires_docker
async def test_the_model_was_actually_called(after_a_mission):
    """모델 카운터가 0 이면 실행 경로가 돌지 않았다는 뜻이다 — echo 대역 회귀 감지."""
    calls = [
        value
        for _labels, value in samples(
            scraped(METRIC_PORTS["planner"]), "aaestro_model_requests_total"
        )
    ]

    assert sum(calls) > 0


@requires_docker
async def test_the_graph_label_carries_the_real_name(after_a_mission):
    """빈 문자열이나 "direct" 면 대시보드의 그래프별 분해가 무의미해진다 (#113)."""
    labelled = [
        labels
        for labels, _value in samples(scraped(METRIC_PORTS["planner"]), "aaestro_agent_tasks_total")
    ]

    assert labelled
    assert any('graph="research-pipeline"' in labels for labels in labelled)
    assert not any('graph=""' in labels for labels in labelled)


@requires_docker
async def test_every_graph_node_reports_its_own_tasks(after_a_mission):
    """한 에이전트만 계측되면 그래프 전체의 실패율이 왜곡된다."""
    for agent, port in METRIC_PORTS.items():
        completed = [
            value
            for labels, value in samples(scraped(port), "aaestro_agent_tasks_total")
            if 'status="completed"' in labels
        ]
        assert sum(completed) > 0, f"{agent} 가 태스크를 계측하지 않았다"
