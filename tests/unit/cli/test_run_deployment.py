"""`aaestro run --deployment` — 주소를 모르고도 배포에 run 을 낸다 (#244)."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest
import uvicorn

from aaestro.cli.main import build_parser, main
from aaestro.orchestrator.control import create_app
from aaestro.orchestrator.run import RunStatus
from aaestro.orchestrator.runstore import InMemoryRunStore, RunRecord
from aaestro.orchestrator.submit import RunResult
from aaestro.orchestrator.topology import GraphMode


class TwoStepRuns:
    """제출 직후는 running, 다음 조회부터 completed — 폴링이 실제로 도는지 본다."""

    def __init__(self, store: InMemoryRunStore) -> None:
        self.store = store
        self.submissions: list[dict[str, Any]] = []
        self.results: dict[str, RunResult] = {}

    async def submit(self, deployment_id, initial_state, *, mode=None, run_id=None) -> RunRecord:
        self.submissions.append({"deployment_id": deployment_id, "input": dict(initial_state)})
        record = RunRecord(run_id=run_id or "run-d", graph="g", mode="mission", status="running")
        self.store.upsert(record)
        return record

    async def resume(self, run_id):  # pragma: no cover - 이 테스트는 제출만 본다
        raise NotImplementedError

    def result_of(self, run_id):
        record = self.store.get(run_id)
        if record is not None and record.status == "running":
            # 첫 조회에서 완료로 넘긴다 — CLI 는 최소 한 번 더 GET 해야 한다
            self.store.upsert(RunRecord(**{**record.__dict__, "status": "completed"}))
            self.results[run_id] = RunResult(
                run_id=run_id,
                graph="g",
                mode=GraphMode.MISSION,
                status=RunStatus.COMPLETED,
                state={"report": "done"},
            )
            return None
        return self.results.get(run_id)

    async def close(self) -> None:
        return None


@pytest.fixture
def served_with_runs():
    store = InMemoryRunStore()
    runs = TwoStepRuns(store)
    config = uvicorn.Config(
        create_app(store, runs=runs), host="127.0.0.1", port=0, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "control plane did not start"
        port = server.servers[0].sockets[0].getsockname()[1]
        yield runs, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_run_by_deployment_submits_and_waits_for_the_result(served_with_runs, capsys):
    runs, url = served_with_runs

    code = main(
        [
            "--json",
            "run",
            "--deployment",
            "dep-1",
            "--input",
            '{"query": "q"}',
            "--control-url",
            url,
            "--poll",
            "0.01",
        ]
    )

    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert runs.submissions == [{"deployment_id": "dep-1", "input": {"query": "q"}}]
    assert out["status"] == "completed" and out["state"] == {"report": "done"}


def test_run_by_deployment_can_return_without_waiting(served_with_runs, capsys):
    runs, url = served_with_runs

    code = main(["--json", "run", "--deployment", "dep-1", "--no-wait", "--control-url", url])

    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["status"] == "running"


def test_run_still_requires_a_graph_without_a_deployment(capsys):
    code = main(["--json", "run"])

    assert code == 1
    assert "graph path or --deployment" in capsys.readouterr().out


def test_the_graph_argument_stays_positional():
    parsed = build_parser().parse_args(["run", "graphs/feed-monitor.yaml"])

    assert parsed.graph == "graphs/feed-monitor.yaml" and parsed.deployment is None
