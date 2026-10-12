"""Control Plane API tests.

핵심은 **프로세스 밖에서 run 을 조작하는 경로**다 (#102). 그래서 조회·drain 은
API 가 저장소만으로 답할 수 있어야 하고, resume 은 구동 프로세스만 할 수 있다.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.orchestrator.control import create_app
from aaestro.orchestrator.runstore import InMemoryRunStore, RunRecord, SqliteRunStore

BASE_URL = "http://control.test"


def record(run_id: str = "run-1", **overrides) -> RunRecord:
    base = {
        "run_id": run_id,
        "graph": "feed-monitor",
        "mode": "service",
        "status": "running",
    }
    return RunRecord(**{**base, **overrides})


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)


@pytest.fixture
def store() -> InMemoryRunStore:
    return InMemoryRunStore()


@pytest.fixture
async def api(store):
    async with client_for(create_app(store)) as client:
        yield client


# --- 조회 --------------------------------------------------------------------


async def test_a_recorded_run_is_listed(api, store):
    store.upsert(record())

    listed = (await api.get("/v1/runs")).json()

    assert [item["run_id"] for item in listed] == ["run-1"]
    assert listed[0]["graph"] == "feed-monitor"


async def test_listing_distinguishes_mission_from_service(api, store):
    """#124 완료 조건 — 두 모드는 슬롯 풀이 다르므로 목록도 갈라져야 한다."""
    store.upsert(record("run-m", mode="mission"))
    store.upsert(record("run-s", mode="service"))

    services = (await api.get("/v1/runs", params={"mode": "service"})).json()
    missions = (await api.get("/v1/runs", params={"mode": "mission"})).json()

    assert [item["run_id"] for item in services] == ["run-s"]
    assert [item["run_id"] for item in missions] == ["run-m"]


async def test_a_single_run_reports_its_progress(api, store):
    store.upsert(record(status="halted", iteration=4, failure_streak=5))

    found = (await api.get("/v1/runs/run-1")).json()

    assert found["status"] == "halted"
    assert found["iteration"] == 4
    assert found["failure_streak"] == 5


async def test_an_unknown_run_is_404(api):
    """조용히 200 을 돌려주면 호출자가 run 이 있다고 오해한다."""
    response = await api.get("/v1/runs/never-existed")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NF_001"


# --- drain -------------------------------------------------------------------


async def test_drain_records_the_request(api, store):
    store.upsert(record())

    response = await api.post("/v1/runs/run-1/drain")

    assert response.status_code == 200
    assert response.json()["drain_requested"] is True
    stored = store.get("run-1")
    assert stored is not None
    assert stored.drain is True


async def test_draining_an_unknown_run_is_404(api):
    response = await api.post("/v1/runs/never-existed/drain")

    assert response.status_code == 404


async def test_drain_returns_without_waiting_for_the_iteration(api, store):
    """완료를 여기서 기다리면 HTTP timeout 과 drain timeout 이 뒤엉킨다.

    응답 시점에 run 은 아직 running 이다 — 정지는 구동 프로세스 몫이다.
    """
    store.upsert(record(status="running"))

    body = (await api.post("/v1/runs/run-1/drain")).json()

    assert body["drain_requested"] is True
    assert body["status"] == "running"


# --- resume ------------------------------------------------------------------


async def test_resume_delegates_to_the_driving_process(store):
    """이어갈 state 는 구동 프로세스의 핸들에 있다."""
    resumed: list[str] = []

    async def resume(run_id: str):
        resumed.append(run_id)
        return type("H", (), {"run_id": f"{run_id}:resumed"})()

    store.upsert(record(status="halted"))

    async with client_for(create_app(store, resume=resume)) as api:
        body = (await api.post("/v1/runs/run-1/resume")).json()

    assert resumed == ["run-1"]
    assert body["run_id"] == "run-1:resumed"


async def test_resume_without_a_driver_is_refused(api, store):
    """조용히 성공하면 운영자가 재개됐다고 믿고 손을 뗀다."""
    store.upsert(record(status="halted"))

    response = await api.post("/v1/runs/run-1/resume")

    assert response.status_code == 501


async def test_resuming_an_unknown_run_is_404(api):
    response = await api.post("/v1/runs/never-existed/resume")

    assert response.status_code == 404


# --- 프로세스 경계 --------------------------------------------------------------


async def test_the_api_serves_runs_another_process_started(tmp_path):
    """#124 의 존재 이유 — API 가 자기가 띄우지 않은 run 을 보여줘야 한다."""
    path = tmp_path / "runs.db"
    driver = SqliteRunStore(path=path)
    serving = SqliteRunStore(path=path)
    try:
        driver.upsert(record(mode="service"))

        async with client_for(create_app(serving)) as api:
            listed = (await api.get("/v1/runs")).json()

        assert [item["run_id"] for item in listed] == ["run-1"]
    finally:
        driver.close()
        serving.close()


async def test_a_drain_through_the_api_reaches_the_driving_process(tmp_path):
    """API 가 남긴 요청을 구동 프로세스가 읽을 수 있어야 한다."""
    path = tmp_path / "runs.db"
    driver = SqliteRunStore(path=path)
    serving = SqliteRunStore(path=path)
    try:
        driver.upsert(record(mode="service"))

        async with client_for(create_app(serving)) as api:
            assert (await api.post("/v1/runs/run-1/drain")).status_code == 200

        seen = driver.get("run-1")
        assert seen is not None
        assert seen.drain is True
    finally:
        driver.close()
        serving.close()


# --- 실제 정지 -----------------------------------------------------------------


class EchoRuntime:
    """노드를 빈 출력으로 완료시키는 runtime 대역."""

    async def invoke(self, node, task):
        from aaestro.core.agent import TaskResult

        return TaskResult.completed(task, output={})


async def _no_wait(_seconds: float) -> None:
    return None


async def test_an_api_drain_actually_stops_the_loop(tmp_path):
    """플래그만 남고 루프가 계속 돌면 drain 은 이름뿐이다 (#124 완료 조건).

    운영 프로세스가 API 로 요청하고, 구동 프로세스의 루프가 그것을 읽는다.
    """
    from aaestro.orchestrator.builder import build_graph
    from aaestro.orchestrator.run import RunManager, RunStatus, ServiceRunner
    from tests.fixtures.topologies import make_service

    path = tmp_path / "runs.db"
    driver = SqliteRunStore(path=path)
    serving = SqliteRunStore(path=path)
    try:
        topology = make_service()
        handle = RunManager(store=driver).acquire(topology)
        runner = ServiceRunner(
            topology, build_graph(topology, EchoRuntime()), sleep=_no_wait, store=driver
        )

        async with client_for(create_app(serving)) as api:
            assert (await api.post(f"/v1/runs/{handle.run_id}/drain")).status_code == 200

        await runner.run(handle, {"feeds": []}, max_iterations=50)

        assert handle.status is RunStatus.STOPPED
        assert handle.iteration == 0
    finally:
        driver.close()
        serving.close()


# --- 에러 → 상태코드 (#234) ---------------------------------------------------


class BrokenStore:
    """모든 조회가 저장소 오류로 실패하는 대역."""

    def list(self, *, mode: str | None = None):  # noqa: A003, ARG002
        raise self._boom()

    def get(self, run_id: str):  # noqa: ARG002
        raise self._boom()

    def request_drain(self, run_id: str) -> bool:  # noqa: ARG002
        raise self._boom()

    @staticmethod
    def _boom() -> AaestroError:
        return AaestroError(
            category=ErrorCategory.STORAGE,
            code=ErrorCode.STOR_003,
            message="run store could not be opened",
        )


async def test_a_storage_failure_is_reported_as_server_side():
    """#234 — 저장소가 깨졌는데 400 을 내리면 운영자가 요청을 의심한다.

    05 의 사고 대응은 4xx/5xx 로 버킷을 가른다. 서버 결함이 4xx 로 새면
    알림이 울리지 않는다.
    """
    async with client_for(create_app(BrokenStore())) as api:
        response = await api.get("/v1/runs")

    assert response.status_code >= 500


async def test_an_unknown_run_is_still_not_found(api):
    """공용 매핑으로 옮기면서 기존 404 가 유지되어야 한다."""
    response = await api.post("/v1/runs/nope/drain")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.NF_001


# --- 카탈로그 (#240) ---------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
async def catalog_api(store):
    from aaestro.catalog import Catalog

    async with client_for(create_app(store, catalog=Catalog.under(REPO_ROOT))) as client:
        yield client


async def test_agents_are_listed_with_a_summary(catalog_api):
    body = (await catalog_api.get("/v1/agents")).json()

    names = {item["name"] for item in body["items"]}
    assert {"planner", "researcher", "writer"} <= names
    planner = next(i for i in body["items"] if i["name"] == "planner")
    assert {"name", "version", "group", "model"} <= planner.keys()
    assert body["problems"] == []


async def test_an_agent_is_returned_in_full(catalog_api):
    body = (await catalog_api.get("/v1/agents/planner")).json()

    assert body["metadata"]["name"] == "planner"
    assert "promptset" in body["spec"]


async def test_an_unknown_agent_is_404(catalog_api):
    response = await catalog_api.get("/v1/agents/nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.NF_001


async def test_graphs_carry_mode_and_node_count(catalog_api):
    body = (await catalog_api.get("/v1/graphs")).json()

    pipeline = next(i for i in body["items"] if i["name"] == "research-pipeline")
    assert pipeline["mode"] == "mission"
    assert pipeline["nodes"] == 3


async def test_a_graph_is_returned_in_full(catalog_api):
    body = (await catalog_api.get("/v1/graphs/feed-monitor")).json()

    assert body["spec"]["mode"] == "service"
    assert [n["id"] for n in body["spec"]["nodes"]]


async def test_groups_are_listed_with_quotas(catalog_api):
    body = (await catalog_api.get("/v1/groups")).json()

    assert any(item["name"] == "global" for item in body["items"])
    assert all("quotas" in item for item in body["items"])


async def test_modules_are_listed_by_version(catalog_api):
    body = (await catalog_api.get("/v1/modules/promptsets")).json()

    assert body["items"], "저장소에 promptset 이 없을 리 없다"
    assert all(item["versions"] for item in body["items"])


async def test_a_module_document_is_returned(catalog_api):
    listed = (await catalog_api.get("/v1/modules/promptsets")).json()["items"][0]
    version = listed["versions"][0]

    body = (await catalog_api.get(f"/v1/modules/promptsets/{listed['name']}/{version}")).json()

    assert body["metadata"]["name"] == listed["name"]
    assert body["metadata"]["version"] == version


async def test_an_unknown_module_type_is_404(catalog_api):
    response = await catalog_api.get("/v1/modules/agents")

    assert response.status_code == 404


async def test_without_a_catalog_the_routes_do_not_exist(api):
    """빈 목록을 돌려주면 "선언이 없다" 로 읽힌다 — 라우트가 아예 없어야 한다."""
    response = await api.get("/v1/agents")

    assert response.status_code == 404
    assert (
        "error" not in response.json() or response.json()["error"].get("code") != ErrorCode.NF_001
    )


async def test_module_listing_reports_broken_versions(store, tmp_path):
    """게시되지 않은 것을 조용히 빼면 UI 는 그 버전이 왜 안 보이는지 모른다."""
    from aaestro.catalog import Catalog

    (tmp_path / "modules" / "promptsets" / "ghost" / "0.1.0").mkdir(parents=True)
    for d in ("agents", "graphs", "groups"):
        (tmp_path / d).mkdir()
    async with client_for(create_app(store, catalog=Catalog.under(tmp_path))) as api:
        body = (await api.get("/v1/modules/promptsets")).json()

    assert body["items"] == []
    assert body["problems"] and "ghost" in body["problems"][0]["path"]
