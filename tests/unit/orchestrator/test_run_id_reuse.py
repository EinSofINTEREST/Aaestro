"""끝난 run 의 id 를 새 제출이 다시 쓰지 못한다 (#663)."""

from __future__ import annotations

import pytest

from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.orchestrator.run import RunManager, RunStatus
from aaestro.orchestrator.runstore import SqliteRunStore
from tests.fixtures.topologies import make_mission, make_service

TOPOLOGIES = [make_mission, make_service]


def finished(path: str, topology) -> None:
    """한 프로세스가 run 을 끝까지 돌리고 사라진다 — 기록만 저장소에 남는다."""
    manager = RunManager(store=SqliteRunStore(path=path))
    manager.acquire(topology, run_id="r1")
    manager.release("r1", RunStatus.COMPLETED)


@pytest.mark.parametrize("make", TOPOLOGIES)
def test_a_restarted_process_refuses_a_finished_runs_id(tmp_path, make):
    path = str(tmp_path / "runs.db")
    finished(path, make())
    restarted = RunManager(store=SqliteRunStore(path=path))

    with pytest.raises(AaestroError) as excinfo:
        restarted.acquire(make(), run_id="r1")

    assert excinfo.value.code == ErrorCode.GRAPH_006
    assert restarted.runs == {}


def test_resuming_takes_a_finished_missions_id(tmp_path):
    """service 재개는 임대 `claim` 을 거친다 — test_run_lease 가 본다."""
    path = str(tmp_path / "runs.db")
    finished(path, make_mission())

    handle = RunManager(store=SqliteRunStore(path=path)).acquire(
        make_mission(), run_id="r1", resuming=True
    )

    assert handle.run_id == "r1"


@pytest.mark.parametrize("make", TOPOLOGIES)
def test_the_same_process_refuses_a_live_runs_id_with_the_same_code(make):
    manager = RunManager()
    manager.acquire(make(), run_id="r1")

    with pytest.raises(AaestroError) as excinfo:
        manager.acquire(make(), run_id="r1")

    assert excinfo.value.code == ErrorCode.GRAPH_006
