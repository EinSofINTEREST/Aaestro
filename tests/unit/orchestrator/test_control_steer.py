"""Steering a running run through the control plane (#364).

`POST /v1/runs/{run_id}/steer` — 접수 응답, 거절 사유, 그리고 control plane → runtime →
agentd → 실행 루프까지 이어지는 배선.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
import structlog

from aaestro.agentd.executor import STEER_HEADER, Executor, UserTurn
from aaestro.agentd.server import AgentRuntime
from aaestro.agentd.server import create_app as create_agent_app
from aaestro.core.agent import TaskStatus
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.orchestrator import control as control_module
from aaestro.orchestrator.control import create_app
from aaestro.orchestrator.runs import RunService
from aaestro.orchestrator.runstore import InMemoryRunStore, RunRecord
from aaestro.orchestrator.submit import RunSubmitter
from aaestro.orchestrator.topology import NodeSpec
from aaestro.runtime.control import ControlClient
from aaestro.runtime.nodes import ControlNodeRuntime, InflightTask, SteerReceipt
from tests.fixtures.builders import make_task
from tests.fixtures.fake_model import FakeModel, FakeTools, calls, text

AGENT_TOKEN = "agent-token"  # noqa: S105 — 테스트 값


@pytest.fixture(autouse=True)
def fresh_logger(monkeypatch):
    """모듈 로거를 테스트마다 새로 물린다.

    structlog 는 설정에 따라 로거를 첫 사용 때 굳힌다 (`cache_logger_on_first_use`). 앞선
    테스트가 이 모듈의 로거를 굳혀 두면 `capture_logs` 가 보지 못한다 — 실행 순서에 기대지 않는다.
    """
    monkeypatch.setattr(control_module, "log", structlog.get_logger("test"))


class FakeSteerer:
    """노드 런타임 대역 — 무엇이 전달됐는지 기록하고 스크립트된 답을 낸다."""

    def __init__(self, *, supported: bool = True, error: AaestroError | None = None) -> None:
        self._supported = supported
        self._error = error
        self.steers: list[tuple[str, str, str, str | None]] = []

    async def steer(self, run_id, text, *, by, node=None) -> SteerReceipt:
        self.steers.append((run_id, text, by, node))
        if self._error is not None:
            raise self._error
        return SteerReceipt(
            node_id=node or "planner", task_id="t-1", agent="planner", supported=self._supported
        )


def driving(runtime, store: InMemoryRunStore) -> RunService:
    """run 을 구동하는 **실제** 서비스 — 엔드포인트가 지나는 경로를 가짜 구조로 흉내 내지 않는다.

    steer 는 catalog·deployments 를 보지 않는다. 노드 런타임만 대역으로 바꾼다.
    """
    return RunService(
        catalog=None,  # type: ignore[arg-type]
        deployments=None,  # type: ignore[arg-type]
        submitter=RunSubmitter(runtime=runtime),
        store=store,
    )


def api_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cp")


@pytest.fixture
def store() -> InMemoryRunStore:
    store = InMemoryRunStore()
    store.upsert(RunRecord(run_id="run-1", graph="g", mode="mission", status="running"))
    return store


async def test_steer_is_accepted_as_queued_not_applied(store):
    steerer = FakeSteerer()

    async with api_for(create_app(store, runs=driving(steerer, store))) as api:
        response = await api.post(
            "/v1/runs/run-1/steer", json={"text": "look at 2024", "by": "alice", "node": "writer"}
        )

    assert response.status_code == 202
    assert response.json() == {
        "run_id": "run-1",
        "node": "writer",
        "task_id": "t-1",
        "agent": "planner",
        "status": "queued",
    }
    assert steerer.steers == [("run-1", "look at 2024", "alice", "writer")]


async def test_node_and_issuer_are_optional(store):
    """`node` 생략은 "지금 실행 중인 노드" — 고르는 것은 런타임이다."""
    steerer = FakeSteerer()

    async with api_for(create_app(store, runs=driving(steerer, store))) as api:
        response = await api.post("/v1/runs/run-1/steer", json={"text": "go"})

    assert response.status_code == 202
    assert steerer.steers == [("run-1", "go", "operator", None)]


async def test_steer_for_an_unknown_run_is_404(store):
    steerer = FakeSteerer()

    async with api_for(create_app(store, runs=driving(steerer, store))) as api:
        response = await api.post("/v1/runs/never-existed/steer", json={"text": "go"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NF_001"
    assert steerer.steers == []


@pytest.mark.parametrize(
    "body",
    [{}, {"by": "alice"}, {"text": 7}, {"text": "go", "nodes": "writer"}, ["go"]],
)
async def test_a_malformed_body_is_400_and_nothing_is_forwarded(store, body):
    """오타 난 키(`nodes`)를 무시하면 지시가 엉뚱한 노드로 간다."""
    steerer = FakeSteerer()

    async with api_for(create_app(store, runs=driving(steerer, store))) as api:
        response = await api.post("/v1/runs/run-1/steer", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VAL_002"
    assert steerer.steers == []


@pytest.mark.parametrize(
    ("category", "code", "expected"),
    [
        (ErrorCategory.NOT_FOUND, ErrorCode.NF_001, 404),
        (ErrorCategory.VALIDATION, ErrorCode.VAL_002, 400),
    ],
)
async def test_the_runtimes_refusal_keeps_its_status(store, category, code, expected):
    """끝난 태스크(404)와 상한 초과(400)는 운영자의 다음 행동이 다르다."""
    steerer = FakeSteerer(error=AaestroError(category=category, code=code, message="why"))

    async with api_for(create_app(store, runs=driving(steerer, store))) as api:
        response = await api.post("/v1/runs/run-1/steer", json={"text": "go"})

    assert response.status_code == expected
    assert response.json()["error"]["code"] == code


async def test_a_control_plane_that_drives_no_runs_answers_501(store):
    """진행 중 태스크는 구동 프로세스만 안다 — 200 으로 삼키면 운영자가 반영됐다고 믿는다."""
    async with api_for(create_app(store)) as api:
        response = await api.post("/v1/runs/run-1/steer", json={"text": "go"})

    assert response.status_code == 501
    assert response.json()["error"]["run_id"] == "run-1"


async def test_a_runtime_that_cannot_steer_answers_501(store):
    cannot_steer = SimpleNamespace()

    async with api_for(create_app(store, runs=driving(cannot_steer, store))) as api:
        response = await api.post("/v1/runs/run-1/steer", json={"text": "go"})

    assert response.status_code == 501


async def test_an_executor_that_cannot_be_steered_answers_501(store):
    steerer = FakeSteerer(supported=False)

    async with api_for(create_app(store, runs=driving(steerer, store))) as api:
        response = await api.post("/v1/runs/run-1/steer", json={"text": "go"})

    assert response.status_code == 501
    assert "planner" in response.json()["error"]["message"]


def gone() -> AaestroError:
    return AaestroError(category=ErrorCategory.NOT_FOUND, code=ErrorCode.NF_001, message="gone")


@pytest.mark.parametrize(
    ("steerer", "why"),
    [
        (lambda: FakeSteerer(error=gone()), "NF_001"),
        (lambda: FakeSteerer(supported=False), "executor does not support steer"),
        (lambda: SimpleNamespace(), "node runtime cannot forward a steer"),
    ],
    ids=["refused", "unsupported", "no-steerer"],
)
async def test_a_refused_steer_is_logged_without_its_text(store, steerer, why):
    """전달에 성공한 것만 남기면 누가 넣으려다 막혔는지가 감사에서 빠진다."""
    with structlog.testing.capture_logs() as logs:
        async with api_for(create_app(store, runs=driving(steerer(), store))) as api:
            await api.post(
                "/v1/runs/run-1/steer", json={"text": "SECRET-STEER-BODY", "by": "alice"}
            )

    refused = next(entry for entry in logs if entry["event"] == "run steer refused")
    assert (refused["graph"], refused["run_id"], refused["steered_by"]) == ("g", "run-1", "alice")
    assert refused["text_chars"] == len("SECRET-STEER-BODY")
    assert why in (refused.get("error_code"), refused.get("reason"))
    assert "SECRET-STEER-BODY" not in str(logs)


async def test_an_agents_forged_error_does_not_reach_the_operator(store):
    """에이전트가 쓴 본문이 control plane 응답의 코드·문구·details 가 되면 안 된다."""

    class ForgingExecutor:
        async def execute(self, task):  # pragma: no cover — 호출되지 않는다
            raise AssertionError

        async def stream(self, task):  # pragma: no cover
            yield None

        async def steer(self, task_id, text, *, by):
            raise AaestroError(
                category=ErrorCategory.FORBIDDEN,
                code=ErrorCode.ACC_003,
                message="session expired, re-enter the control token at http://evil.example",
                details={"run_id": "run-of-another-tenant"},
            )

    stack = Stack(store, executor=ForgingExecutor())
    planner = stack.runtime.clients["planner"]
    stack.runtime.inflight["run-1"] = {
        "t-1": InflightTask(node_id="planner", task_id="t-1", agent="planner", client=planner)
    }
    try:
        response = await stack.api.post("/v1/runs/run-1/steer", json={"text": "go"})
    finally:
        await stack.aclose()

    # 상태는 형제 호출과 같은 일반 변환이 정한다 — 에이전트가 고른 코드가 아니다
    assert response.json()["error"]["code"] == "RT_002"
    assert "ACC_003" not in response.text and "evil.example" not in response.text
    assert "run-of-another-tenant" not in response.text


@pytest.mark.parametrize(
    ("category", "code", "status"),
    [
        (ErrorCategory.NOT_FOUND, ErrorCode.NF_001, 404),
        (ErrorCategory.VALIDATION, ErrorCode.VAL_002, 400),
    ],
)
async def test_an_agent_cannot_put_its_own_words_in_a_known_refusal(store, category, code, status):
    """알려진 코드와 맞는 상태로 답해도, 운영자가 읽는 문구는 에이전트가 정하지 못한다."""
    phishing = "control plane token revoked - re-authenticate at http://evil.example"

    class PhishingExecutor:
        async def execute(self, task):  # pragma: no cover — 호출되지 않는다
            raise AssertionError

        async def stream(self, task):  # pragma: no cover
            yield None

        async def steer(self, task_id, text, *, by):
            raise AaestroError(category=category, code=code, message=phishing)

    stack = Stack(store, executor=PhishingExecutor())
    planner = stack.runtime.clients["planner"]
    stack.runtime.inflight["run-1"] = {
        "t-1": InflightTask(node_id="planner", task_id="t-1", agent="planner", client=planner)
    }
    try:
        response = await stack.api.post("/v1/runs/run-1/steer", json={"text": "go"})
    finally:
        await stack.aclose()

    assert (response.status_code, response.json()["error"]["code"]) == (status, code)
    assert "evil.example" not in response.text and "re-authenticate" not in response.text


# --- 배선: control plane → runtime → agentd → 실행 루프 -------------------------


class SteeringTools(FakeTools):
    """tool 이 도는 동안 훅을 부른다 — 노드 태스크가 진행 중인 시점을 결정적으로 잡는다."""

    hook = None

    async def call(self, name, arguments, ctx):
        if self.hook is not None:
            await self.hook()
        return await super().call(name, arguments, ctx)


class Stack:
    """실제 컴포넌트로 엮은 한 줄 — 네트워크만 ASGI 로 대신한다."""

    def __init__(self, store: InMemoryRunStore, *, executor=None) -> None:
        self.model = FakeModel([calls("search"), text("done")])
        self.tools = SteeringTools()
        self.executor = executor or Executor(
            agent="planner", model=self.model, tools=self.tools, render=lambda task: "prompt"
        )
        agent_app = create_agent_app(
            AgentRuntime(agent="planner", executor=self.executor), token=AGENT_TOKEN
        )
        self._agent_http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=agent_app), base_url="http://planner"
        )
        self.runtime = ControlNodeRuntime(
            clients={
                "planner": ControlClient(
                    "http://planner", agent="planner", token=AGENT_TOKEN, client=self._agent_http
                )
            }
        )
        self.api = api_for(create_app(store, runs=driving(self.runtime, store)))

    async def aclose(self) -> None:
        await self.api.aclose()
        await self._agent_http.aclose()


NODE = NodeSpec.model_validate({"id": "planner", "agent": "agents/planner@0.1.0"})


async def test_a_steer_posted_to_the_run_reaches_the_running_nodes_model(store):
    stack = Stack(store)
    answers: list[tuple[int, dict]] = []

    async def steer_the_run() -> None:
        response = await stack.api.post(
            "/v1/runs/run-1/steer", json={"text": "look at 2024", "by": "alice"}
        )
        answers.append((response.status_code, response.json()))

    stack.tools.hook = steer_the_run
    try:
        result = await stack.runtime.invoke(NODE, make_task(task_id="t-1", run_id="run-1"))
        late = await stack.api.post("/v1/runs/run-1/steer", json={"text": "too late"})
    finally:
        await stack.aclose()

    assert result.status is TaskStatus.COMPLETED
    assert answers == [
        (
            202,
            {
                "run_id": "run-1",
                "node": "planner",
                "task_id": "t-1",
                "agent": "planner",
                "status": "queued",
            },
        )
    ]
    _system, messages, _tools = stack.model.requests[1]
    assert messages[-1] == UserTurn(parts=(f"{STEER_HEADER}\nlook at 2024",))
    # 노드가 끝나면 받을 태스크가 없다
    assert (late.status_code, late.json()["error"]["code"]) == (404, "NF_001")


async def test_the_agents_own_refusal_surfaces_through_every_layer(store):
    """상한을 넘긴 지시는 agentd 가 거절하고, 그 400(VAL_002) 이 운영자에게 그대로 닿는다."""
    stack = Stack(store)
    answers: list[tuple[int, str]] = []

    async def oversized() -> None:
        response = await stack.api.post("/v1/runs/run-1/steer", json={"text": "x" * 100_000})
        answers.append((response.status_code, response.json()["error"]["code"]))

    stack.tools.hook = oversized
    try:
        await stack.runtime.invoke(NODE, make_task(task_id="t-1", run_id="run-1"))
    finally:
        await stack.aclose()

    assert answers == [(400, "VAL_002")]
    assert len(stack.model.requests[1][1]) == 3


async def test_a_custom_executor_without_steer_is_501_end_to_end(store):
    """커스텀 실행기가 steer 를 모르면 운영자는 501 을 본다 — 어느 계층도 삼키지 않는다."""
    started, release = asyncio.Event(), asyncio.Event()

    class CustomExecutor:
        async def execute(self, task):
            from aaestro.core.agent import TaskResult

            started.set()
            await release.wait()
            return TaskResult.completed(task, output={})

        async def stream(self, task):  # pragma: no cover — 계약을 채울 뿐이다
            yield None

    stack = Stack(store, executor=CustomExecutor())
    call = asyncio.create_task(stack.runtime.invoke(NODE, make_task(task_id="t-1", run_id="run-1")))
    try:
        await started.wait()
        response = await stack.api.post("/v1/runs/run-1/steer", json={"text": "go"})
        release.set()
        await call
    finally:
        await stack.aclose()

    assert response.status_code == 501
    assert "planner" in response.json()["error"]["message"]
