"""프로세스 사이의 같은 run id 동시 제출은 하나만 이긴다 (#711)."""

from __future__ import annotations

import multiprocessing
from collections.abc import Callable
from typing import Any

import pytest

from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.orchestrator.run import RunManager
from aaestro.orchestrator.runs import RunService
from aaestro.orchestrator.runstore import RunRecord, SqliteRunStore
from tests.fixtures.topologies import make_mission, make_service
from tests.unit.orchestrator.test_runs import FakeCatalog, FakeDeployments, FakeSubmitter, deployed

PROCESSES = 4


class Interleaved(SqliteRunStore):
    """기록 유무를 확인하는 순간 (``get`` 이든 ``create`` 든) 다른 프로세스의 제출을 끼워 넣는다.

    확인과 쓰기가 갈라져 있으면 확인은 "없음" 을 보고 쓰기는 다른 쪽의 기록을 덮는다 —
    스레드 경합을 기다리지 않고 그 틈을 결정적으로 만든다.
    """

    rival: Callable[[], Any] | None = None

    def _interleave(self) -> None:
        rival, self.rival = self.rival, None
        if rival is not None:
            rival()

    def get(self, run_id: str) -> RunRecord | None:
        found = super().get(run_id)
        self._interleave()
        return found

    def create(self, record: RunRecord) -> bool:
        self._interleave()
        return super().create(record)


def _create(path: str, start: Any, results: Any) -> None:
    store = SqliteRunStore(path=path)
    start.wait()
    results.put(store.create(RunRecord(run_id="r1", graph="g", mode="mission", status="running")))


def test_separate_processes_creating_the_same_id_only_one_wins(tmp_path):
    path = str(tmp_path / "runs.db")
    SqliteRunStore(path=path).close()  # 스키마를 먼저 — 프로세스마다의 CREATE 가 겹치지 않게
    ctx = multiprocessing.get_context("spawn")
    start, results = ctx.Barrier(PROCESSES), ctx.Queue()
    procs = [ctx.Process(target=_create, args=(path, start, results)) for _ in range(PROCESSES)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=60)

    assert all(proc.exitcode == 0 for proc in procs)
    assert sorted(results.get(timeout=5) for _ in procs) == [False] * (PROCESSES - 1) + [True]


@pytest.mark.parametrize("make", [make_mission, make_service])
def test_two_connections_submitting_the_same_id_only_one_records(tmp_path, make):
    path = str(tmp_path / "runs.db")
    rival = RunManager(store=SqliteRunStore(path=path))
    store = Interleaved(path=path)
    store.rival = lambda: rival.acquire(make(), run_id="r1")
    manager = RunManager(store=store)

    with pytest.raises(AaestroError) as excinfo:
        manager.acquire(make(), run_id="r1")

    assert excinfo.value.code == ErrorCode.GRAPH_006
    assert manager.runs == {}
    assert list(rival.runs) == ["r1"]


async def test_control_plane_loses_to_a_cli_submission_of_the_same_id(tmp_path):
    mission = make_mission()
    path = str(tmp_path / "runs.db")
    cli = RunManager(store=SqliteRunStore(path=path))
    store = Interleaved(path=path)
    store.rival = lambda: cli.acquire(mission, run_id="r1")
    submitter = FakeSubmitter(store=store)  # type: ignore[arg-type]
    runs = RunService(
        catalog=FakeCatalog({mission.metadata.name: mission}),  # type: ignore[arg-type]
        deployments=FakeDeployments({"dep-1": deployed(mission.metadata.name)}),  # type: ignore[arg-type]
        submitter=submitter,  # type: ignore[arg-type]
        store=store,
    )

    with pytest.raises(AaestroError) as excinfo:
        await runs.submit("dep-1", {"query": "q"}, run_id="r1")

    assert excinfo.value.code == ErrorCode.GRAPH_006
    assert submitter.calls == []  # 드라이버를 띄우지 않았다
    assert list(cli.runs) == ["r1"]
