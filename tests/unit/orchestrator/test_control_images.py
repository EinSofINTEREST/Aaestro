"""Image build routes (#265)."""

from __future__ import annotations

import httpx
import pytest

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.orchestrator.control import create_app
from aaestro.orchestrator.runstore import InMemoryRunStore
from aaestro.runtime.images import BuildRecord, BuildStatus, image_tag


class FakeBuilder:
    """라우트가 빌더 계약을 어떻게 부르는지만 본다 — 조립은 단위 테스트 소관."""

    class _Catalog:
        def agent(self, name: str):
            if name == "nobody":
                raise AaestroError(
                    category=ErrorCategory.NOT_FOUND, code=ErrorCode.NF_001, message="unknown agent"
                )
            return type("M", (), {"metadata": type("Meta", (), {"version": "0.1.0"})()})()

    def __init__(self) -> None:
        self.catalog = self._Catalog()
        self.records: dict[tuple[str, str], BuildRecord] = {}
        self.custom = {"custom"}
        self.running: set[str] = set()

    def needs_build(self, agent: str, version: str) -> bool:
        return agent in self.custom

    def record_of(self, agent: str, version: str) -> BuildRecord | None:
        return self.records.get((agent, version))

    async def start(self, agent: str) -> BuildRecord:
        """굽지 않고 접수만 한다 — 라우트가 기다리지 않는지 보려면 끝나지 않아야 한다."""
        self.catalog.agent(agent)
        if agent not in self.custom:
            raise AaestroError(
                category=ErrorCategory.VALIDATION,
                code=ErrorCode.VAL_002,
                message="agent has no build materials",
            )
        if agent in self.running:
            raise AaestroError(
                category=ErrorCategory.RUNTIME,
                code=ErrorCode.RT_011,
                message="an image build for this version is already running",
            )
        self.running.add(agent)
        return self._record(agent, BuildStatus.BUILDING)

    def finish(self, agent: str) -> None:
        """빌드가 끝난 척한다 — GET 이 진행을 따라가는지 보기 위한 것."""
        self.running.discard(agent)
        self._record(agent, BuildStatus.BUILT)

    def _record(self, agent: str, status: BuildStatus) -> BuildRecord:
        record = BuildRecord(
            agent=agent,
            version="0.1.0",
            status=status,
            image=f"aaestro/agent-{agent}:0.1.0",
            log="Step 1/3" if status is BuildStatus.BUILT else "",
        )
        self.records[agent, "0.1.0"] = record
        return record


@pytest.fixture
def builder() -> FakeBuilder:
    return FakeBuilder()


@pytest.fixture
async def api(builder):
    app = create_app(InMemoryRunStore(), builder=builder)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://cp") as client:
        yield client


async def test_a_build_is_accepted_without_waiting_for_it(api):
    """분 단위가 될 수 있는 빌드로 요청을 붙잡지 않는다 — 제출만 하고 202 로 돌아온다."""
    response = await api.post("/v1/agents/custom/image")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == BuildStatus.BUILDING
    assert body["image"] == "aaestro/agent-custom:0.1.0"


async def test_a_second_build_while_one_runs_is_409(api, builder):
    """같은 태그를 두 번 구우면 결과가 뒤집힌다 — 상태 충돌이므로 409 다."""
    await api.post("/v1/agents/custom/image")

    response = await api.post("/v1/agents/custom/image")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ErrorCode.RT_011

    builder.finish("custom")
    assert (await api.post("/v1/agents/custom/image")).status_code == 202


async def test_an_agent_that_was_never_built_says_so(api):
    body = (await api.get("/v1/agents/custom/image")).json()

    assert body["status"] is None
    assert body["needs_build"] is True
    assert body["image"] == "aaestro/agent-custom:0.1.0"
    assert body["expected_image"] == "aaestro/agent-custom:0.1.0"


async def test_the_status_follows_the_build(api, builder):
    await api.post("/v1/agents/custom/image")
    assert (await api.get("/v1/agents/custom/image")).json()["status"] == BuildStatus.BUILDING

    builder.finish("custom")
    body = (await api.get("/v1/agents/custom/image")).json()

    assert body["status"] == BuildStatus.BUILT
    assert body["log"] == "Step 1/3"
    # UI 가 기록의 태그와 대조할 기준 — 게이트와 같은 image_tag() 에서 온다
    assert body["expected_image"] == image_tag("custom", "0.1.0")


async def test_a_declarative_agent_does_not_need_a_build(api):
    """base 이미지로 도는 에이전트를 굽게 만들면 모듈 시스템의 이점이 사라진다."""
    body = (await api.get("/v1/agents/plain/image")).json()

    assert body["needs_build"] is False


async def test_building_an_agent_without_materials_is_400(api):
    response = await api.post("/v1/agents/plain/image")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.VAL_002


async def test_an_unknown_agent_is_404(api):
    assert (await api.post("/v1/agents/nobody/image")).status_code == 404
    assert (await api.get("/v1/agents/nobody/image")).status_code == 404


async def test_without_a_builder_the_routes_do_not_exist():
    app = create_app(InMemoryRunStore())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://cp") as api:
        assert (await api.get("/v1/agents/custom/image")).status_code in (404, 405)
