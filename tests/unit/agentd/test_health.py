"""agentd health reflects MCP sessions, optional servers and the model (#311).

항상 healthy 로 답하면 runtime 의 재시작 정책은 프로세스가 죽을 때만 움직인다 — 재연결을 소진한
MCP 세션을 가진 에이전트가 Ready 로 남아 태스크를 받고 실패한다.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aaestro.agentd import __main__ as agentd
from aaestro.agentd.executor import Executor, ModuleBinding
from aaestro.agentd.health import ModelHealth, agent_health
from aaestro.core.agent import ComponentHealth, HealthState, TaskConfig
from aaestro.core.errors import AaestroError, ErrorCategory
from tests.fixtures.builders import make_manifest, make_task
from tests.fixtures.fake_model import FakeTools


class Session:
    def __init__(self, state: HealthState, *, optional: bool = False) -> None:
        self.spec = SimpleNamespace(optional=optional)
        self._state = state

    def health(self) -> ComponentHealth:
        detail = "reconnect exhausted" if self._state is HealthState.UNHEALTHY else None
        return ComponentHealth(state=self._state, detail=detail)


def executor_with(sessions: dict[str, Session], degraded: tuple[str, ...] = ()) -> SimpleNamespace:
    tools = SimpleNamespace(mcp=SimpleNamespace(sessions=sessions))
    return SimpleNamespace(
        binding=SimpleNamespace(tools=tools, degraded=degraded), model_health=ModelHealth()
    )


def test_a_required_session_that_gave_up_reconnecting_is_unhealthy():
    """unhealthy 여야 runtime 이 재시작해 세션을 다시 세운다."""
    status = agent_health(executor_with({"fs": Session(HealthState.UNHEALTHY)}))

    assert status.status is HealthState.UNHEALTHY
    assert status.components["mcp:fs"].detail == "reconnect exhausted"


def test_an_optional_session_failure_only_degrades():
    """없어도 되는 서버 때문에 에이전트 전부를 재시작하지 않는다."""
    status = agent_health(executor_with({"web": Session(HealthState.UNHEALTHY, optional=True)}))

    assert status.status is HealthState.DEGRADED
    assert "optional" in (status.components["mcp:web"].detail or "")


def test_an_optional_server_that_never_started_is_degraded():
    status = agent_health(executor_with({}, degraded=("web",)))

    assert status.status is HealthState.DEGRADED
    assert status.components["mcp:web"].state is HealthState.DEGRADED


def test_live_sessions_and_modules_are_healthy():
    status = agent_health(executor_with({"fs": Session(HealthState.HEALTHY)}))

    assert status.status is HealthState.HEALTHY
    assert set(status.components) == {"modules", "mcp:fs", "model"}


def test_a_custom_executor_without_modules_is_healthy():
    assert agent_health(object()).status is HealthState.HEALTHY


# --- 모델: 최근 호출 기록 ---------------------------------------------------------


def test_a_failed_model_call_degrades_until_the_next_success():
    """provider 장애는 재시작으로 낫지 않는다 — unhealthy 가 아니라 degraded."""
    model = ModelHealth()

    model.failed("LLM_003")
    assert model.component().state is HealthState.DEGRADED
    assert model.component().detail == "last model call failed: LLM_003"

    model.succeeded()
    assert model.component().state is HealthState.HEALTHY


class FailingModel:
    async def run(self, system, messages, tools):
        raise AaestroError(category=ErrorCategory.MODEL, code="LLM_003", message="provider down")


async def test_the_executor_records_model_failures_for_health():
    executor = Executor(agent="a", model=FailingModel(), tools=FakeTools(), render=lambda t: "p")

    await executor.execute(make_task())

    assert executor.model_health.last_error_code == "LLM_003"
    assert agent_health(executor).components["model"].state is HealthState.DEGRADED


# --- 배선: Control API 의 /v1/health ----------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [(HealthState.HEALTHY, "healthy"), (HealthState.UNHEALTHY, "unhealthy")],
)
def test_the_control_api_serves_the_live_health(state, expected):
    """상수로 답하던 자리 — 앱이 실행기를 보고 답하는지 본다."""
    executor = executor_with({"fs": Session(state)})
    executor.execute = None  # build_app 은 호출하지 않는다

    app = agentd.build_app(make_manifest(), executor)
    with TestClient(app) as client:
        body = client.get("/v1/health").json()

    assert body["status"] == expected


def test_health_follows_the_binding_a_reload_swapped_in():
    """리로드가 옵션 서버를 잃은 묶음으로 바꾸면 health 도 그것을 본다."""
    tools = FakeTools()
    executor = Executor(agent="a", model=FailingModel(), tools=tools, render=lambda t: "p")
    assert agent_health(executor).status is HealthState.HEALTHY

    executor.rebind(ModuleBinding(tools=tools, render=lambda t: "p", degraded=("web",)))

    assert agent_health(executor).components["mcp:web"].state is HealthState.DEGRADED


class HangingModel:
    async def run(self, system, messages, tools):
        await asyncio.sleep(10)


async def test_a_model_call_cut_by_the_task_deadline_degrades_the_model():
    """마감에 잘린 호출은 예외 경로를 타지 않는다 — 그래도 health 가 봐야 한다."""
    executor = Executor(agent="a", model=HangingModel(), tools=FakeTools(), render=lambda t: "p")

    result = await executor.execute(make_task(config=TaskConfig(timeout_s=0.01)))

    assert result.error is not None and result.error.code == "TO_001"
    assert executor.model_health.last_error_code == "TO_001"


async def test_a_cancelled_task_does_not_count_against_the_model():
    """사용자가 취소한 태스크는 모델의 잘못이 아니다."""
    executor = Executor(agent="a", model=HangingModel(), tools=FakeTools(), render=lambda t: "p")
    running = asyncio.create_task(executor.execute(make_task()))
    await asyncio.sleep(0.01)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert executor.model_health.last_error_code is None
