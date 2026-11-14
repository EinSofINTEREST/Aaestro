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
