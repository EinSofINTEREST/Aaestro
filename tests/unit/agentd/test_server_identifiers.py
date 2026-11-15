"""Control API 가 받은 태스크 식별자를 A2A 본문과 같은 규칙으로 거른다 (#575)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from aaestro.agentd.server import AgentRuntime, create_app
from aaestro.core.agent import TaskResult
from aaestro.core.identifiers import identifier_problem

AUTH = {"authorization": "Bearer t"}


class _Executor:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def execute(self, task):
        self.seen.append(task.task_id)
        return TaskResult.completed(task, output={})

    async def stream(self, task):  # pragma: no cover - 거절 경로에서는 부르지 않는다
        self.seen.append(task.task_id)
        yield None


def body(**overrides):
    base = {"task_id": "t1", "run_id": "r1", "node_id": "n1", "trace": {"trace_id": "tr1"}}
    base.update(overrides)
    return json.dumps(base)  # 고립 surrogate 는 \\ud800 이스케이프로 실린다


def post(path, text):
    executor = _Executor()
    api = TestClient(
        create_app(AgentRuntime(agent="a", executor=executor), token="t"),
        raise_server_exceptions=False,
    )
    response = api.post(path, content=text, headers={**AUTH, "content-type": "application/json"})
    return response, executor


BAD = [
    pytest.param({"task_id": "\ud800"}, "task_id", "\ud800", id="surrogate"),
    pytest.param({"task_id": "a\nb"}, "task_id", "a\nb", id="control"),
    pytest.param({"task_id": "x" * 129}, "task_id", "x" * 129, id="too-long"),
    pytest.param({"run_id": " r1"}, "run_id", " r1", id="run-space"),
    pytest.param({"node_id": ""}, "node_id", "", id="node-empty"),
    pytest.param({"trace": {"trace_id": "t\x00"}}, "trace.trace_id", "t\x00", id="trace-id"),
    pytest.param({"trace": {"trace_id": "t", "span_id": "s\n"}}, "trace.span_id", "s\n", id="span"),
    pytest.param(
        {"trace": {"trace_id": "t", "graph": "g" * 129}}, "trace.graph", "g" * 129, id="graph"
    ),
]


@pytest.mark.parametrize("path", ["/v1/invoke", "/v1/stream"])
@pytest.mark.parametrize(("override", "field", "value"), BAD)
def test_malformed_identifier_is_rejected_without_echoing_the_value(path, override, field, value):
    response, executor = post(path, body(**override))

    assert response.status_code == 400
    assert response.json()["code"] == "VAL_002"
    assert response.json()["details"] == {
        "field": field,
        "reason": identifier_problem(value).value,
    }
    assert executor.seen == []
    # 값이 어느 문맥에도 실리지 않는다 — 원문과 JSON 이스케이프 표현 둘 다.
    # 빈 값은 모든 글에 들어 있으므로 건너뛴다
    for shown in (value, json.dumps(value)[1:-1]):
        if shown:
            assert shown not in response.text


@pytest.mark.parametrize("task_id", ["123e4567-e89b-12d3-a456-426614174000", "direct-abc"])
def test_uuid_and_direct_ids_pass(task_id):
    response, executor = post("/v1/invoke", body(task_id=task_id, run_id="direct-1", node_id=None))

    assert response.status_code == 200
    assert executor.seen == [task_id]


class _SteerExecutor(_Executor):
    async def steer(self, task_id, text, *, by):
        return True


def post_path(path, *, steer=True):
    executor = _SteerExecutor() if steer else _Executor()
    api = TestClient(
        create_app(AgentRuntime(agent="a", executor=executor), token="t"),
        raise_server_exceptions=False,
    )
    return api.post(path, json={"text": "go", "by": "op"}, headers=AUTH)


# 경로로 실린 값은 퍼센트 인코딩이다 — 디코드된 값이 식별자 규칙을 지나야 한다
PATH_BAD = [
    pytest.param("a%0Ab", "a\nb", id="control"),
    pytest.param("x" * 129, "x" * 129, id="too-long"),
    pytest.param("%20x", " x", id="leading-space"),
]


@pytest.mark.parametrize("route", ["cancel", "steer"])
@pytest.mark.parametrize(("raw", "decoded"), PATH_BAD)
def test_path_task_id_malformed_is_400_without_echo(route, raw, decoded):
    response = post_path(f"/v1/{route}/{raw}")

    assert response.status_code == 400
    assert response.json()["code"] == "VAL_002"
    assert response.json()["details"] == {
        "field": "task_id",
        "reason": identifier_problem(decoded).value,
    }
    for shown in (decoded, json.dumps(decoded)[1:-1], raw):
        assert shown not in response.text


def test_path_task_id_well_formed_unknown_stays_404():
    response = post_path("/v1/cancel/direct-abc")

    assert response.status_code == 404
    assert response.json()["code"] == "NF_001"


def test_path_task_id_is_checked_before_unsupported_steer():
    response = post_path("/v1/steer/a%0Ab", steer=False)

    assert response.status_code == 400
    assert response.json()["code"] == "VAL_002"
