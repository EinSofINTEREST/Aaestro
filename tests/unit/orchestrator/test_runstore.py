"""Cross-process run registry tests.

``RunHandle`` 은 구동 프로세스의 메모리에만 있다 — 이 저장소가 그 격차를
메운다 (#102). 핵심 검증은 **서로 다른 store 인스턴스**가 같은 run 을 본다는
것이다: 한 인스턴스 안에서만 확인하면 프로세스 경계를 넘는지 알 수 없다.
"""

from __future__ import annotations

import sqlite3

import pytest

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.orchestrator.run import RunManager, RunStatus, ServiceRunner
from aaestro.orchestrator.runstore import (
    InMemoryRunStore,
    RunRecord,
    SqliteRunStore,
)
from tests.fixtures.topologies import make_mission, make_service

GRAPH = "research-pipeline"
SERVICE_GRAPH = "feed-monitor"


def record(run_id: str = "run-1", **overrides) -> RunRecord:
    base = {
        "run_id": run_id,
        "graph": GRAPH,
        "mode": "mission",
        "status": "running",
    }
    return RunRecord(**{**base, **overrides})


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    """두 구현이 같은 계약을 지키는지 — 하나만 검증하면 갈라진다."""
    if request.param == "memory":
        yield InMemoryRunStore()
        return
    opened = SqliteRunStore(path=tmp_path / "runs.db")
    try:
        yield opened
    finally:
        opened.close()


# --- 저장소 계약 ----------------------------------------------------------------


def test_a_stored_run_can_be_read_back(store):
    store.upsert(record())

    found = store.get("run-1")

    assert found is not None
    assert found.graph == GRAPH
    assert found.status == "running"


def test_an_unknown_run_reads_as_none(store):
    assert store.get("never-existed") is None


def test_upsert_updates_rather_than_duplicating(store):
    store.upsert(record())
    store.upsert(record(status="halted", iteration=7))

    found = store.get("run-1")

    assert found is not None
    assert found.status == "halted"
    assert found.iteration == 7
    assert len(store.list()) == 1


def test_listing_can_be_narrowed_by_mode(store):
    """mission 과 service 는 슬롯 풀이 다르다 — 목록도 갈라져야 한다."""
    store.upsert(record("run-m", mode="mission"))
    store.upsert(record("run-s", mode="service"))

    assert [r.run_id for r in store.list(mode="service")] == ["run-s"]
    assert [r.run_id for r in store.list(mode="mission")] == ["run-m"]
    assert len(store.list()) == 2


def test_drain_can_be_requested_on_a_stored_run(store):
    store.upsert(record())

    assert store.request_drain("run-1") is True
    found = store.get("run-1")
    assert found is not None
    assert found.drain is True


def test_draining_an_unknown_run_reports_failure(store):
    """조용히 성공하면 호출자가 요청이 전달됐다고 오해한다."""
    assert store.request_drain("never-existed") is False


def test_a_progress_update_does_not_erase_a_drain_request(store):
    """구동 프로세스의 갱신이 요청을 지우면 그 요청은 영원히 전달되지 않는다."""
    store.upsert(record())
    store.request_drain("run-1")

    store.upsert(record(status="running", iteration=3))

    found = store.get("run-1")
    assert found is not None
    assert found.drain is True


# --- 프로세스 경계 --------------------------------------------------------------


def test_model_overrides_are_read_back(store):
    overrides = {"researcher": {"name": "claude-haiku-4-5", "fallbacks": []}}
    store.upsert(record(model_overrides=overrides))

    assert store.get("run-1").model_overrides == overrides


def test_a_handle_update_does_not_erase_the_model_overrides(store):
    """슬롯 · iteration 갱신은 핸들만 안다 — 그 갱신이 지우면 resume 이 다른 모델로 돈다."""
    overrides = {"researcher": {"name": "claude-haiku-4-5"}}
    store.upsert(record(model_overrides=overrides))

    store.upsert(record(status="failed"))

    assert store.get("run-1").status == "failed"
    assert store.get("run-1").model_overrides == overrides


def test_the_sqlite_store_opens_a_database_from_before_model_overrides(tmp_path):
    """#358 이전 DB 에는 컬럼이 없다 — 열 때 더해야 기동이 죽지 않는다 (#254 와 같은 사정)."""
    import sqlite3

    path = tmp_path / "runs.db"
    with sqlite3.connect(path) as old:
        old.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, graph TEXT NOT NULL, "
            "mode TEXT NOT NULL, status TEXT NOT NULL, iteration INTEGER NOT NULL DEFAULT 0, "
            "failure_streak INTEGER NOT NULL DEFAULT 0, drain INTEGER NOT NULL DEFAULT 0, "
            "updated_at TEXT NOT NULL)"
        )
        old.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)",
            ("run-old", GRAPH, "mission", "failed", 0, 0, 0, "t"),
        )

    store = SqliteRunStore(path=path)
    try:
        assert store.get("run-old").model_overrides == {}
        store.upsert(record("run-old", model_overrides={"planner": {"name": "m"}}))
        assert store.get("run-old").model_overrides == {"planner": {"name": "m"}}
    finally:
        store.close()


def test_another_process_sees_the_run(tmp_path):
    """#123 완료 조건 — 별개의 연결이 같은 run 을 본다."""
    path = tmp_path / "runs.db"
    writer = SqliteRunStore(path=path)
    reader = SqliteRunStore(path=path)
    try:
        writer.upsert(record(status="running"))

        found = reader.get("run-1")

        assert found is not None
        assert found.graph == GRAPH
    finally:
        writer.close()
        reader.close()


def test_a_drain_requested_elsewhere_is_visible_here(tmp_path):
    """다른 프로세스가 남긴 요청을 구동 프로세스가 읽을 수 있어야 한다."""
    path = tmp_path / "runs.db"
    driver = SqliteRunStore(path=path)
    operator = SqliteRunStore(path=path)
    try:
        driver.upsert(record(mode="service"))

        assert operator.request_drain("run-1") is True

        seen = driver.get("run-1")
        assert seen is not None
        assert seen.drain is True
    finally:
        driver.close()
        operator.close()


# --- RunManager 배선 -------------------------------------------------------------


def test_acquiring_a_run_publishes_it(store):
    """저장소에 남지 않으면 다른 프로세스가 그 run 을 알 수 없다."""
    manager = RunManager(store=store)

    handle = manager.acquire(make_mission())

    found = store.get(handle.run_id)
    assert found is not None
    assert found.graph == GRAPH
    assert found.mode == "mission"


def test_releasing_a_run_publishes_its_final_status(store):
    manager = RunManager(store=store)
    handle = manager.acquire(make_mission())

    manager.release(handle.run_id, RunStatus.COMPLETED)

    found = store.get(handle.run_id)
    assert found is not None
    assert found.status == "completed"


def test_a_manager_without_a_store_still_works():
    """저장소 미주입이 기존 배선을 깨뜨리면 안 된다."""
    manager = RunManager()

    handle = manager.acquire(make_mission())
    manager.release(handle.run_id, RunStatus.COMPLETED)

    assert manager.store is None
    assert handle.status is RunStatus.COMPLETED


def test_a_drain_from_elsewhere_reaches_the_handle(store):
    """sync_drain 이 없으면 저장소에 요청이 쌓여도 run 은 계속 돈다."""
    manager = RunManager(store=store)
    handle = manager.acquire(make_service())

    store.request_drain(handle.run_id)

    assert manager.sync_drain(handle) is True
    assert handle.drain_requested


# --- 실제 정지 -----------------------------------------------------------------


class EchoRuntime:
    """노드를 빈 출력으로 완료시키는 runtime 대역."""

    async def invoke(self, node, task):
        from aaestro.core.agent import TaskResult

        return TaskResult.completed(task, output={})


async def _no_wait(_seconds: float) -> None:
    return None


async def test_a_service_run_stops_when_another_process_asks(tmp_path):
    """#123 완료 조건 — 요청이 실제로 루프를 멈춰야 한다.

    저장소에 플래그만 남고 루프가 계속 돌면 drain 은 이름뿐이다.
    """
    from aaestro.orchestrator.builder import build_graph

    path = tmp_path / "runs.db"
    driver = SqliteRunStore(path=path)
    operator = SqliteRunStore(path=path)
    try:
        topology = make_service()
        manager = RunManager(store=driver)
        handle = manager.acquire(topology)
        runner = ServiceRunner(
            topology, build_graph(topology, EchoRuntime()), sleep=_no_wait, store=driver
        )

        # 다른 프로세스가 요청을 남긴다 — run 이 돌기 전에
        assert operator.request_drain(handle.run_id) is True

        await runner.run(handle, {"feeds": []}, max_iterations=50)

        assert handle.status is RunStatus.STOPPED
        assert handle.iteration == 0  # 요청을 즉시 읽어 한 회차도 돌지 않는다
    finally:
        driver.close()
        operator.close()


async def test_iteration_progress_is_visible_to_another_process(tmp_path):
    """진행이 보이지 않으면 운영자가 stalled 인지 도는 중인지 구분할 수 없다."""
    from aaestro.orchestrator.builder import build_graph

    path = tmp_path / "runs.db"
    driver = SqliteRunStore(path=path)
    watcher = SqliteRunStore(path=path)
    try:
        topology = make_service()
        manager = RunManager(store=driver)
        handle = manager.acquire(topology)
        runner = ServiceRunner(
            topology, build_graph(topology, EchoRuntime()), sleep=_no_wait, store=driver
        )

        await runner.run(handle, {"feeds": []}, max_iterations=3)

        seen = watcher.get(handle.run_id)
        assert seen is not None
        assert seen.iteration == 3
    finally:
        driver.close()
        watcher.close()


async def test_a_service_run_without_a_store_is_unaffected(tmp_path):
    """저장소 미주입 시 기존 동작 그대로여야 한다."""
    from aaestro.orchestrator.builder import build_graph

    topology = make_service()
    handle = RunManager().acquire(topology)
    runner = ServiceRunner(topology, build_graph(topology, EchoRuntime()), sleep=_no_wait)

    await runner.run(handle, {"feeds": []}, max_iterations=2)

    assert handle.iteration == 2


class _BrokenConnection:
    """닫을 때 sqlite 오류를 내는 연결 — close() 의 변환을 확인한다."""

    def close(self) -> None:
        raise sqlite3.OperationalError("disk I/O error")


def _close_with_broken_connection(store: SqliteRunStore) -> None:
    store._conn.close()  # noqa: SLF001
    store._conn = _BrokenConnection()  # type: ignore[assignment]  # noqa: SLF001
    store.close()


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.upsert(record()),
        lambda s: s.get("run-1"),
        lambda s: s.list(),
        lambda s: s.list(mode="mission"),
        lambda s: s.request_drain("run-1"),
        _close_with_broken_connection,
    ],
    ids=["upsert", "get", "list", "list-by-mode", "request_drain", "close"],
)
def test_sqlite_methods_convert_database_errors_to_stor_003(tmp_path, call):
    """모든 공개 메서드가 같은 boundary 변환을 한다 — 호출자는 sqlite 예외 타입을 몰라도 된다."""
    store = SqliteRunStore(path=tmp_path / "runs.db")
    if call is not _close_with_broken_connection:
        store.close()

    with pytest.raises(AaestroError) as caught:
        call(store)

    assert caught.value.code == ErrorCode.STOR_003
    assert caught.value.category is ErrorCategory.STORAGE
    assert isinstance(caught.value.__cause__, sqlite3.Error)


@pytest.mark.parametrize(
    "read", [lambda s: s.get("run-1"), lambda s: s.list()], ids=["get", "list"]
)
def test_a_corrupted_row_reads_as_stor_003(tmp_path, read):
    store = SqliteRunStore(path=tmp_path / "runs.db")
    try:
        store.upsert(record())
        store._conn.execute("UPDATE runs SET model_overrides = '{not json'")  # noqa: SLF001

        with pytest.raises(AaestroError) as caught:
            read(store)
    finally:
        store.close()

    assert caught.value.code == ErrorCode.STOR_003
    assert isinstance(caught.value.__cause__, ValueError)
