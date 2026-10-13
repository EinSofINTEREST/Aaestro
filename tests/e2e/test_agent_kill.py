"""Killing an agent mid-run.

#163 이 덮은 것은 **오케스트레이터** 재시작 경계다. 에이전트가 죽는 것은 다른
실패 모드다 — 02 lifecycle 의 재시작 정책과 `max_failure_streak` 가 함께
걸린다 (#199).

여기서 확인할 것은 "복구되는가" 가 아니라 **무엇이 어떻게 기록되는가** 다:
운영자가 로그와 저장소만 보고 원인을 짚을 수 있어야 한다.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from aaestro.core.errors import ErrorCode
from aaestro.orchestrator.checkpoint import build_checkpointer, close_checkpointer
from aaestro.orchestrator.run import RunManager, RunStatus
from aaestro.orchestrator.runstore import SqliteRunStore
from aaestro.orchestrator.submit import RunSubmitter
from aaestro.orchestrator.topology import GraphTopology
from aaestro.runtime.control import ControlClient
from aaestro.runtime.nodes import ControlNodeRuntime
from tests.e2e.test_stack import (
    AGENT_TOKEN,
    COMPOSE_FILE,
    compose_up,
    docker,
    requires_docker,
    wait_healthy,
)

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]
# **에이전트 이름**이 키다 — 노드 id(watcher/classifier/notifier)가 아니라.
# 런타임은 `agent_name(node.agent)` 로 클라이언트를 찾으므로, 노드 id 로 키를
# 만들면 모든 노드가 GRAPH_002 로 실패한다 (그래도 iteration 은 올라가서
# 회차만 세는 검증은 통과한다 — 그래서 조용히 지나갔다)
AGENT_PORTS = {"planner": 18082, "researcher": 18083, "writer": 18084}
# feed-monitor 의 watcher 는 researcher 에이전트로 돈다 — 그 컨테이너를 죽인다
WATCHER_SERVICE = "agent-researcher"
CHECKPOINT_URL = "postgresql://aaestro:aaestro@127.0.0.1:15433/aaestro"

INITIAL_STATE = {"feeds": ["https://example.test/feed.xml"]}
"""빈 state 로 시작하면 promptset 의 필수 변수가 비어 `MOD_004` 로 실패한다 —
그래도 iteration 은 올라가므로 회차만 세는 검증은 조용히 통과한다."""


def topology(**service_overrides: Any) -> GraphTopology:
    """레퍼런스 service 그래프 — 임계를 좁혀 정지 경로를 빨리 본다."""
    raw = yaml.safe_load((REPO_ROOT / "graphs" / "feed-monitor.yaml").read_text("utf-8"))
    raw["spec"]["service"].update(service_overrides)
    return GraphTopology.model_validate(raw)


@pytest.fixture(scope="module")
def stack() -> Iterator[dict[str, int]]:
    """세 에이전트 + 외부 checkpointer — finalizer 가 반드시 정리한다."""
    compose_up()
    try:
        for port in AGENT_PORTS.values():
            assert wait_healthy(f"http://127.0.0.1:{port}"), f"agent on {port} never became healthy"
        yield AGENT_PORTS
    finally:
        docker("compose", "-f", str(COMPOSE_FILE), "down", "-v", check=False)


@pytest.fixture
async def runs(stack: dict[str, int], tmp_path):
    """오케스트레이터 하나 — 에이전트를 죽여도 이쪽은 살아 있다."""
    clients = {
        name: ControlClient(f"http://127.0.0.1:{port}", agent=name, token=AGENT_TOKEN)
        for name, port in stack.items()
    }
    checkpointer = build_checkpointer("postgres", url=CHECKPOINT_URL)
    submitter = RunSubmitter(
        runtime=ControlNodeRuntime(clients=clients),
        checkpointer=checkpointer,
        manager=RunManager(store=SqliteRunStore(path=str(tmp_path / "runs.db"))),
    )
    try:
        yield submitter
    finally:
        for client in clients.values():
            await client.aclose()
        await close_checkpointer(checkpointer)


@pytest.fixture
def revive():
    """죽인 에이전트를 반드시 되살린다 — 다음 테스트가 스택을 그대로 쓴다."""
    yield
    docker("compose", "-f", str(COMPOSE_FILE), "start", WATCHER_SERVICE, check=False)
    assert wait_healthy(f"http://127.0.0.1:{AGENT_PORTS['researcher']}"), "agent never came back"


async def no_sleep(_delay: float) -> None:
    """idle backoff 를 즉시 통과시킨다 — 06 은 실제 sleep 을 금지한다."""


@requires_docker
async def test_a_dead_agent_halts_the_run_with_graph_005(runs, revive):
    """에이전트가 죽으면 iteration 이 연속 실패하고, 임계에서 정지한다.

    조용히 도는 것보다 **정지가 낫다**: 05 의 ServiceRunHalted 알림이 그것을
    본다.
    """
    docker("compose", "-f", str(COMPOSE_FILE), "stop", WATCHER_SERVICE)

    handle = await runs.start_service(
        topology(max_failure_streak=2), {}, run_id="kill-halt", max_iterations=20, sleep=no_sleep
    )
    await runs.services[handle.run_id]

    assert handle.status is RunStatus.HALTED
    assert handle.error is not None
    assert handle.error.code == ErrorCode.GRAPH_005


@requires_docker
async def test_the_halt_is_visible_to_another_process(runs, revive):
    """운영자가 저장소만 보고 원인을 짚을 수 있어야 한다 (05 Incident Response)."""
    docker("compose", "-f", str(COMPOSE_FILE), "stop", WATCHER_SERVICE)

    handle = await runs.start_service(
        topology(max_failure_streak=2), {}, run_id="kill-visible", max_iterations=20, sleep=no_sleep
    )
    await runs.services[handle.run_id]

    record = runs.manager.store.get("kill-visible")  # type: ignore[union-attr]
    assert record is not None
    assert record.status == str(RunStatus.HALTED)


@requires_docker
async def test_a_revived_agent_lets_the_run_resume(runs, revive):
    """원인이 해소되면 재개된다 — 죽음이 영구 고장으로 남으면 안 된다."""
    docker("compose", "-f", str(COMPOSE_FILE), "stop", WATCHER_SERVICE)
    halted = await runs.start_service(
        topology(max_failure_streak=2), {}, run_id="kill-resume", max_iterations=20, sleep=no_sleep
    )
    await runs.services[halted.run_id]
    assert halted.status is RunStatus.HALTED

    # 에이전트가 돌아온다 — 운영자가 원인을 해소한 상황
    docker("compose", "-f", str(COMPOSE_FILE), "start", WATCHER_SERVICE)
    assert wait_healthy(f"http://127.0.0.1:{AGENT_PORTS['researcher']}")

    # `max_iterations` 는 **절대 회차 상한**이다 (재개 지점부터 세는 것이
    # 아니다) — 정지 지점보다 크게 줘야 한 회차라도 더 돈다
    resumed = await runs.resume_service(
        topology(max_failure_streak=2),
        "kill-resume",
        max_iterations=halted.iteration + 1,
        sleep=no_sleep,
    )
    await runs.services[resumed.run_id]

    assert resumed.status is RunStatus.STOPPED
    assert resumed.failure_streak == 0
    # 실패한 회차를 다시 돌리면 부수효과가 겹친다 (01 Mode Rules 2)
    assert resumed.iteration > halted.iteration


@requires_docker
async def test_a_healthy_run_is_not_halted_by_the_threshold(runs):
    """임계가 멀쩡한 배포를 정지시키면 안 된다 — 실패가 없으면 streak 도 없다."""
    handle = await runs.start_service(
        topology(max_failure_streak=2), {}, run_id="kill-healthy", max_iterations=3, sleep=no_sleep
    )
    await runs.services[handle.run_id]

    assert handle.status is RunStatus.STOPPED
    assert handle.iteration == 3
