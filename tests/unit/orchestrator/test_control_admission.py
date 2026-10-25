"""The Control Plane checks the caller before it reads the body (#462).

FastAPI 는 의존성을 풀기 전에 본문을 읽고 JSON 을 파싱한다. 인증을 ``Depends`` 로 걸면 깨진 JSON ·
UTF-8 본문이 인증보다 먼저 거절되고 (깨진 UTF-8 은 typed 응답도 아니었다), 토큰 없는 호출자가 응답
차이로 라우트의 본문 처리를 알게 된다. 세 청중 (운영자 · 에이전트 · 강제 지점) 모두 같다.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import structlog.testing
from prometheus_client import CollectorRegistry

from aaestro.access.registry import AccessRegistry
from aaestro.access.store import InMemoryAccessStore
from aaestro.catalog import Catalog
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.observability.metrics import Metrics
from aaestro.orchestrator.control import create_app
from aaestro.orchestrator.runstore import InMemoryRunStore
from tests.fixtures.access import STEWARD, access_workspace
from tests.fixtures.routes import unguarded_paths

CONTROL = "control-token"
ENFORCER = "enforcer-token"
OPERATOR = {"authorization": f"Bearer {CONTROL}"}
JSON = {"content-type": "application/json"}

BODIES = [
    pytest.param(b"{not", id="broken-json"),
    pytest.param(b"\xff\xfe", id="broken-utf8-bom"),
    pytest.param(b"\xff\xfd", id="broken-utf8"),
    pytest.param(b'{"text": 5}', id="wrong-shape"),
    pytest.param(b"", id="empty"),
]


@pytest.fixture
def registry(tmp_path: Path) -> AccessRegistry:
    catalog = Catalog.under(access_workspace(tmp_path))
    return AccessRegistry(
        store=InMemoryAccessStore(), catalog=catalog, stewards=frozenset({STEWARD})
    )


@pytest.fixture
async def api(registry):
    app = create_app(
        InMemoryRunStore(),
        catalog=registry.catalog,
        token=CONTROL,
        access=registry,
        enforcer_token=ENFORCER,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        yield client


def shape(response: httpx.Response) -> tuple[int, bytes, str | None]:
    return response.status_code, response.content, response.headers.get("www-authenticate")


# 청중마다 본문 없는 기준 거절 — 본문이 무엇이든 이것과 같아야 한다
AUDIENCES = [
    pytest.param("/v1/runs/x/steer", id="operator"),
    pytest.param("/v1/access/revocations", id="operator-access"),
    pytest.param("/v1/access/decisions", id="enforcer"),
    pytest.param("/v1/access/identities", id="enforcer-identity"),
    pytest.param("/v1/access/grants", id="steward"),
    pytest.param("/v1/access/a2a/tickets", id="agent-ticket"),
    pytest.param("/v1/access/a2a/verify", id="agent-verify"),
]


@pytest.mark.parametrize("path", AUDIENCES)
@pytest.mark.parametrize("body", BODIES)
async def test_without_a_credential_the_body_does_not_change_the_rejection(api, path, body):
    """토큰 없는 요청에는 본문과 무관하게 같은 거절 — 깨진 본문이 400 으로 먼저 나가지 않는다."""
    baseline = await api.post(path, headers={"authorization": "Bearer nope"})
    response = await api.post(path, content=body, headers={**JSON, "authorization": "Bearer nope"})

    assert shape(response) == shape(baseline)
    assert response.status_code in (401, 403), path


@pytest.mark.parametrize("body", BODIES)
async def test_an_operator_route_without_any_header_is_the_plain_401(api, body):
    response = await api.post("/v1/runs/x/steer", content=body, headers=JSON)

    assert shape(response) == (401, b'{"detail":"invalid control plane token"}', "Bearer")


async def test_with_the_token_broken_utf8_is_a_typed_validation_error(api):
    """FastAPI 기본 ``{"detail": "There was an error parsing the body"}`` 이 아니다."""
    response = await api.post(
        "/v1/runs/x/steer",
        content=b"\xff\xfd",
        headers={**JSON, "authorization": f"Bearer {CONTROL}"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VAL_002"


async def test_with_the_token_broken_json_is_still_a_typed_validation_error(api):
    response = await api.post(
        "/v1/access/revocations",
        content=b"{not",
        headers={**JSON, "authorization": f"Bearer {CONTROL}"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VAL_002"


async def test_with_the_enforcer_token_broken_json_is_a_typed_validation_error(api):
    response = await api.post(
        "/v1/access/decisions",
        content=b"{not",
        headers={**JSON, "authorization": f"Bearer {ENFORCER}"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VAL_002"


# --- 무인증으로 열린 것은 의도로만 ------------------------------------------------------


def test_only_the_allowlisted_routes_skip_the_credential_check(registry):
    """앱에 직접 라우트를 붙이면 무인증으로 열린다 — 새 라우트는 이 목록을 고쳐야 통과한다.

    모든 선택 표면을 붙인 앱으로 본다 — 붙일 때마다 라우터가 늘어난다. author · builder ·
    deployments · runs 는 라우트를 등록할 때 쓰이지 않으므로 빈 대역으로 충분하다 (요청을 보내지
    않는다).
    """
    app = create_app(
        InMemoryRunStore(),
        resume=lambda _run_id: None,
        catalog=registry.catalog,
        token=CONTROL,
        author=SimpleNamespace(),  # type: ignore[arg-type]
        builder=SimpleNamespace(),  # type: ignore[arg-type]
        deployments=SimpleNamespace(),  # type: ignore[arg-type]
        runs=SimpleNamespace(),  # type: ignore[arg-type]
        access=registry,
        enforcer_token=ENFORCER,
    )

    assert unguarded_paths(app) == {"/v1/health", "/ui", "/"}


async def test_health_stays_open(api):
    assert (await api.get("/v1/health")).status_code == 200


async def test_the_ui_stays_open(api):
    """화면 자체는 비밀이 아니다 — 인증은 화면이 부르는 `/v1/*` 에 있다."""
    assert (await api.get("/ui/")).status_code == 200
    assert (await api.get("/", follow_redirects=False)).status_code == 307


async def test_the_ui_without_a_slash_still_reaches_the_ui(api):
    """``redirect_slashes`` 를 끈 뒤에도 `/ui` 는 화면으로 간다 — 명시한 리다이렉트다."""
    response = await api.get("/ui", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/ui/"


# 라우트 표를 알려 줄 수 있는 요청들 — 세 청중의 라우트 · 없는 경로 · 틀린 메서드 · `/` · 문서
PROBES = [
    pytest.param("GET", "/v1/runs", id="operator-route"),
    pytest.param("POST", "/v1/access/decisions", id="enforcer-route"),
    pytest.param("POST", "/v1/access/grants", id="steward-route"),
    pytest.param("GET", "/v1/nope-xyz", id="no-route"),
    pytest.param("DELETE", "/v1/runs", id="wrong-method"),
    pytest.param("GET", "/v1/runs/", id="trailing-slash"),
    pytest.param("GET", "/openapi.json", id="openapi"),
    pytest.param("GET", "/v1/health/", id="health-with-slash"),
]


@pytest.mark.parametrize(("method", "path"), PROBES)
@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer nope"}], ids=["none", "wrong"])
async def test_without_a_credential_the_route_table_cannot_be_enumerated(
    api, method, path, headers
):
    """자격 없는 호출자에게는 경로 · 메서드 · 청중과 무관하게 같은 401.

    404 · 405 + `Allow` 로 라우트 표가 새지 않는다.
    """
    response = await api.request(method, path, headers=headers, follow_redirects=False)

    assert response.status_code == 401
    assert response.content == b'{"detail":"invalid control plane token"}'
    assert dict(response.headers) == {
        "content-length": str(len(response.content)),
        "content-type": "application/json",
        "www-authenticate": "Bearer",
    }


async def test_the_operator_token_passes_routing_and_sees_the_plain_404(api):
    """운영자는 전부 닿는다 — 토큰을 가진 쪽의 404 약속 (코드 없는 404 = 표면 없음) 은 그대로다.

    다른 청중은 자기 경로 밖에서 401 이다 (아래 "청중마다 닿는 경로").
    """
    response = await api.get("/v1/nope-xyz", headers=OPERATOR)

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
async def test_api_documentation_is_not_served(api, path):
    """스키마(필드 · 상한)를 토큰을 가진 쪽에게도 열지 않는다."""
    response = await api.get(path, headers=OPERATOR)

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}  # 코드 없는 404 — 라우트 부재의 약속


@pytest.mark.parametrize("path", ["/v1/runs/", "/v1/agents/", "/v1/access/decisions/"])
async def test_a_trailing_slash_does_not_redirect_to_the_real_route(api, path):
    response = await api.get(path, headers=OPERATOR, follow_redirects=False)

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


async def test_with_the_token_an_unknown_route_is_the_plain_not_found(api):
    """UI · CLI 의 약속 — 코드 없는 404 = 그 표면이 없다."""
    response = await api.get("/v1/nope-xyz", headers=OPERATOR)

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert "nope-xyz" not in response.text


async def test_with_the_token_a_wrong_method_is_a_typed_rejection(api):
    response = await api.delete("/v1/runs", headers=OPERATOR)

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "VAL_002"


async def test_without_a_control_token_nothing_is_checked_before_routing(registry):
    """운영자 표면이 열려 있으면 (loopback 무인증) 라우팅 전에 막을 것이 없다."""
    app = create_app(InMemoryRunStore(), catalog=registry.catalog, token=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as open_api:
        response = await open_api.get("/v1/nope-xyz")

    assert response.status_code == 404


# --- 부여는 권한 에이전트만, 본문보다 먼저 ------------------------------------------------


@pytest.fixture
def counted(tmp_path: Path) -> AccessRegistry:
    catalog = Catalog.under(access_workspace(tmp_path))
    return AccessRegistry(
        store=InMemoryAccessStore(),
        catalog=catalog,
        stewards=frozenset({STEWARD}),
        metrics=Metrics(CollectorRegistry()),
    )


def refusals(registry: AccessRegistry, *, resource: str, decided_by: str) -> float:
    assert registry.metrics is not None
    counter = registry.metrics.counter("aaestro_access_grants_total")
    return counter.labels(resource=resource, op="refuse", decided_by=decided_by)._value.get()


async def grant_with(registry: AccessRegistry, credential: str, body: bytes) -> httpx.Response:
    app = create_app(
        InMemoryRunStore(),
        catalog=registry.catalog,
        token=CONTROL,
        access=registry,
        enforcer_token=ENFORCER,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        return await client.post(
            "/v1/access/grants",
            content=body,
            headers={**JSON, "authorization": f"Bearer {credential}"},
        )


@pytest.mark.parametrize("body", BODIES)
async def test_a_worker_grant_is_refused_and_counted_whatever_the_body(counted, body):
    """작업 에이전트는 깨진 본문으로 ``ACC_003`` 의 기록 · 메트릭 · WARN 을 피하지 못한다.

    피하면 AccessGrantRefusalsSpike 가 울리지 않는다.
    """
    worker = counted.issue_identity("worker", "dep-1")

    with structlog.testing.capture_logs() as logs:
        response = await grant_with(counted, worker, body)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACC_003"
    assert refusals(counted, resource="unknown", decided_by="worker") == 1
    warnings = [e for e in logs if e["event"] == "access grant refused before body"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["resource"] == "unknown"
    assert warnings[0]["target"] == "unknown"
    assert (warnings[0]["agent"], warnings[0]["decided_by"]) == ("worker", "worker")
    assert warnings[0]["error_code"] == "ACC_003"
    if body:
        assert body.decode("utf-8", "replace") not in repr(warnings)
    assert counted.rules("worker") == []


async def test_a_permission_agent_with_a_broken_body_passes_admission_then_fails_validation(
    counted,
):
    steward = counted.issue_identity(STEWARD, "dep-1")

    response = await grant_with(counted, steward, b"{not")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VAL_002"
    assert refusals(counted, resource="unknown", decided_by=STEWARD) == 0


# --- 청중마다 닿는 경로 ---------------------------------------------------------------

# 운영자 라우트 표를 알려 줄 수 있는 요청들 — 있는 것 · 없는 것 · 틀린 메서드 · 강제 지점 전용
OPERATOR_PROBES = [
    pytest.param("GET", "/v1/agents", id="operator-route"),
    pytest.param("GET", "/v1/nope-xyz", id="no-route"),
    pytest.param("DELETE", "/v1/runs", id="wrong-method"),
    pytest.param("GET", "/v1/access/agents/worker", id="operator-access-route"),
]


def raw(response: httpx.Response) -> tuple[int, bytes, dict[str, str]]:
    return response.status_code, response.content, dict(response.headers)


@pytest.mark.parametrize(("method", "path"), OPERATOR_PROBES)
async def test_enforcer_and_agent_credentials_cannot_enumerate_operator_routes(
    api, registry, method, path
):
    """에이전트 컨테이너는 신뢰하지 않는다 (01) — 자기 청중 밖은 자격이 없을 때와 완전히 같다."""
    agent = registry.issue_identity("worker", "dep-1")
    nothing = await api.request(method, path)

    for credential in (ENFORCER, agent):
        response = await api.request(
            method, path, headers={"authorization": f"Bearer {credential}"}
        )
        assert raw(response) == raw(nothing), credential == ENFORCER

    assert nothing.status_code == 401


async def test_an_agent_cannot_reach_enforcer_routes_before_routing(api, registry):
    agent = registry.issue_identity("worker", "dep-1")
    nothing = await api.post("/v1/access/decisions", content=b"{not", headers=JSON)

    response = await api.post(
        "/v1/access/decisions",
        content=b"{not",
        headers={**JSON, "authorization": f"Bearer {agent}"},
    )

    assert raw(response) == raw(nothing)


async def test_each_audience_still_reaches_its_own_routes(api, registry):
    """좁히기가 제 청중을 막지 않는다.

    라우트 표에서 만든 집합이므로 라우트를 더해도 같이 늘어난다.
    """
    agent = registry.issue_identity("worker", "dep-1")

    def bearer_of(token: str) -> dict[str, str]:
        return {"authorization": f"Bearer {token}"}

    feed = "/v1/access/changes?wait_s=0"
    assert (await api.get(feed, headers=bearer_of(ENFORCER))).status_code == 200
    assert (await api.get(feed, headers=bearer_of(agent))).status_code == 200
    decide = await api.post("/v1/access/decisions", json={}, headers=bearer_of(ENFORCER))
    assert decide.status_code == 400  # 라우트까지 닿아 본문 검증을 받았다
    ticket = await api.post("/v1/access/a2a/tickets", json={}, headers=bearer_of(agent))
    assert ticket.status_code == 400


def test_the_audience_paths_come_from_the_registered_routes(registry):
    """손으로 쓴 목록이 아니다 — 라우터에 등록된 경로와 정확히 같다."""
    from fastapi import APIRouter, FastAPI

    from aaestro.orchestrator.access_api import mount_access

    audiences = mount_access(FastAPI(), APIRouter(), registry, enforcer_token=ENFORCER)

    assert audiences.enforcer == {
        "/v1/access/decisions",
        "/v1/access/identities",
        "/v1/access/changes",
    }
    assert audiences.agent == {
        "/v1/access/changes",
        "/v1/access/a2a/tickets",
        "/v1/access/a2a/verify",
        "/v1/access/grants",
    }


# --- 라우팅 전 검사의 저장소 장애 ------------------------------------------------------


class _BrokenStore(InMemoryAccessStore):
    """신원 조회가 저장소 오류를 내는 저장소 — 레지스트리 장애를 흉내낸다."""

    def identity(self, credential_hash: str):  # type: ignore[no-untyped-def]
        raise AaestroError(
            category=ErrorCategory.STORAGE,
            code=ErrorCode.STOR_003,
            message="registry store unavailable",
            retryable=True,
        )


async def test_a_storage_failure_before_routing_is_the_typed_503(tmp_path):
    """앱의 예외 핸들러 바깥이라도 plain 500 이 아니다 — enforcer 경로와 같은 typed 503."""
    registry = AccessRegistry(
        store=_BrokenStore(),
        catalog=Catalog.under(access_workspace(tmp_path)),
        stewards=frozenset({STEWARD}),
    )
    app = create_app(
        InMemoryRunStore(),
        catalog=registry.catalog,
        token=CONTROL,
        access=registry,
        enforcer_token=ENFORCER,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        response = await client.get(
            "/v1/access/changes?wait_s=0", headers={"authorization": "Bearer some-agent"}
        )

    assert response.status_code == 503
    error = response.json()["error"]
    assert (error["code"], error["retryable"]) == ("STOR_003", True)
    assert "some-agent" not in response.text


@pytest.mark.parametrize("method", ["POST", "DELETE", "OPTIONS", "HEAD"])
@pytest.mark.parametrize("path", ["/v1/health", "/", "/ui", "/ui/", "/ui/index.html"])
async def test_open_paths_are_open_for_get_only(api, path, method):
    """열린 경로의 다른 메서드는 닫힌 경로와 같은 401 — 405 + `Allow` 로 메서드가 새지 않는다."""
    # 같은 메서드로 닫힌 경로를 부른 응답과 헤더 전체까지 같아야 한다 (HEAD 는 본문이 없다)
    reference = await api.request(method, "/v1/nope", follow_redirects=False)

    response = await api.request(method, path, follow_redirects=False)

    assert reference.status_code == 401
    assert (response.status_code, response.content, dict(response.headers)) == (
        reference.status_code,
        reference.content,
        dict(reference.headers),
    )
    assert "allow" not in response.headers


async def test_an_authenticated_wrong_method_on_health_is_still_405(api):
    """자격이 있는 호출자는 라우팅까지 닿는다."""
    response = await api.post("/v1/health", headers=OPERATOR)

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
