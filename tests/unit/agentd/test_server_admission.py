"""The Control API checks the token before it reads the body (#462).

FastAPI 는 의존성을 풀기 전에 본문을 읽고 JSON 을 파싱한다. 인증을 ``Depends`` 로 걸면 깨진 JSON ·
UTF-8 본문이 인증보다 먼저 400 으로 거절되고, 토큰 없는 호출자가 응답 차이로 라우트의 본문 처리를
알게 된다. 본문이 무엇이든 거절은 하나여야 한다.
"""

from __future__ import annotations

import pytest
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from fastapi.testclient import TestClient

from aaestro.agentd.server import AgentRuntime, create_app
from aaestro.core.agent import TaskResult
from tests.fixtures.builders import make_task
from tests.fixtures.routes import unguarded_paths

TOKEN = "agent-token"
AUTH = {"authorization": f"Bearer {TOKEN}"}

BODIES = [
    pytest.param(b"{not", id="broken-json"),
    pytest.param(b"\xff\xfe", id="broken-utf8-bom"),
    pytest.param(b"\xff\xfd", id="broken-utf8"),
    pytest.param(b'{"task_id": 5}', id="wrong-shape"),
    pytest.param(b"", id="empty"),
]

BODY_ROUTES = ["/v1/invoke", "/v1/stream", "/v1/steer/t1"]


class _Executor:
    async def execute(self, task):
        return TaskResult.completed(task, output={})

    async def stream(self, task):  # pragma: no cover - 인증 거절 경로에서는 부르지 않는다
        yield None


def client(token: str | None = TOKEN) -> TestClient:
    runtime = AgentRuntime(agent="researcher", executor=_Executor())
    return TestClient(create_app(runtime, token=token), raise_server_exceptions=False)


def rejection(response) -> tuple[int, bytes, str | None]:
    return response.status_code, response.content, response.headers.get("www-authenticate")


@pytest.mark.parametrize("path", BODY_ROUTES)
@pytest.mark.parametrize("body", BODIES)
def test_without_a_token_the_body_does_not_change_the_rejection(path, body):
    """토큰 없는 요청에는 본문과 무관하게 같은 401 — 깨진 본문이 400 으로 먼저 나가지 않는다."""
    api = client()

    response = api.post(path, content=body, headers={"content-type": "application/json"})

    assert rejection(response) == (401, b'{"detail":"invalid agent token"}', "Bearer")


@pytest.mark.parametrize("body", BODIES)
def test_a_wrong_token_is_rejected_before_the_body_too(body):
    api = client()

    response = api.post(
        "/v1/invoke",
        content=body,
        headers={"content-type": "application/json", "authorization": "Bearer wrong"},
    )

    assert response.status_code == 401


def test_with_the_token_a_broken_body_is_a_typed_validation_error():
    """인증을 통과한 뒤에는 본문 검증이 그대로 돈다 — 순서만 바뀐다."""
    api = client()

    response = api.post(
        "/v1/invoke", content=b"{not", headers={**AUTH, "content-type": "application/json"}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "VAL_002"


def test_with_the_token_broken_utf8_is_a_typed_validation_error():
    """FastAPI 기본 ``{"detail": "There was an error parsing the body"}`` 이 아니다."""
    api = client()

    response = api.post(
        "/v1/invoke", content=b"\xff\xfd", headers={**AUTH, "content-type": "application/json"}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "VAL_002"
    assert "\\xff" not in response.text


def test_a_valid_task_still_runs():
    api = client()

    response = api.post("/v1/invoke", json=make_task().model_dump(mode="json"), headers=AUTH)

    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/v1/card", AGENT_CARD_WELL_KNOWN_PATH])
def test_the_cards_stay_behind_the_token(path):
    """앱에 직접 붙은 well-known 라우트도 검사하는 라우터에 있다 — 열린 라우트는 health 뿐이다."""
    api = client()

    assert api.get(path).status_code == 401
    assert api.get(path, headers=AUTH).status_code == 200


# --- 무인증으로 열린 것은 의도로만 ------------------------------------------------------


def test_only_the_allowlisted_routes_skip_the_token_check():
    """앱에 직접 라우트를 붙이면 무인증으로 열린다 — 새 라우트는 이 목록을 고쳐야 통과한다."""
    runtime = AgentRuntime(agent="researcher", executor=_Executor())

    assert unguarded_paths(create_app(runtime, token=TOKEN)) == {"/v1/health"}


def test_health_is_the_only_route_open_without_a_token():
    """Docker healthcheck 가 직접 부른다 (02 API Rules 4)."""
    api = client()

    assert api.get("/v1/health").status_code == 200


# 라우트 표를 알려 줄 수 있는 요청들 — 있는 경로 · 없는 경로 · 틀린 메서드 · 뒤에 붙은 `/` · 문서
PROBES = [
    pytest.param("POST", "/v1/invoke", id="real-route"),
    pytest.param("GET", "/v1/nope", id="no-route"),
    pytest.param("GET", "/v1/invoke", id="wrong-method"),
    pytest.param("POST", "/v1/invoke/", id="trailing-slash"),
    pytest.param("GET", "/openapi.json", id="openapi"),
    pytest.param("GET", "/v1/health/", id="health-with-slash"),
    pytest.param("POST", "/v1/health", id="health-post"),
    pytest.param("DELETE", "/v1/health", id="health-delete"),
    pytest.param("OPTIONS", "/v1/health", id="health-options"),
]

# HEAD 응답은 본문이 없다 — 따로 상태와 챌린지만 본다
HEAD_PROBE = "/v1/health"


@pytest.mark.parametrize(("method", "path"), PROBES)
@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer wrong"}], ids=["none", "wrong"])
def test_without_a_token_the_route_table_cannot_be_enumerated(method, path, headers):
    """토큰 없는 호출자에게는 경로 · 메서드와 무관하게 같은 401 — 404 · 405 + `Allow` · 307 없음."""
    api = client()

    response = api.request(method, path, headers=headers, follow_redirects=False)

    assert response.status_code == 401
    assert response.content == b'{"detail":"invalid agent token"}'
    assert dict(response.headers) == {
        "content-length": str(len(response.content)),
        "content-type": "application/json",
        "www-authenticate": "Bearer",
    }


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_api_documentation_is_not_served(path):
    """스키마(필드 · 상한)를 토큰을 가진 쪽에게도 열지 않는다."""
    response = client().get(path, headers=AUTH)

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}  # 코드 없는 404 — 라우트 부재의 약속


def test_a_trailing_slash_does_not_redirect_to_the_real_route():
    response = client().post("/v1/invoke/", content=b"{}", headers=AUTH, follow_redirects=False)

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_with_the_token_an_unknown_route_is_the_plain_not_found():
    """토큰을 가진 쪽의 약속 — 코드 없는 404 = 그 표면이 없다 (runtime 이 이것으로 가른다)."""
    response = client().get("/v1/nope", headers=AUTH)

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert "nope" not in response.text


def test_with_the_token_a_wrong_method_is_a_typed_rejection_without_the_agent_name():
    response = client().get("/v1/invoke", headers=AUTH)

    assert response.status_code == 405
    assert response.json()["code"] == "VAL_002"
    assert response.json()["agent"] is None
    assert "researcher" not in response.text


def test_without_a_configured_token_the_body_is_validated_directly():
    """무인증 조립(loopback opt-in)에서는 검사가 없다 — 본문 검증이 바로 돈다."""
    api = client(token=None)

    response = api.post("/v1/invoke", content=b"{not", headers={"content-type": "application/json"})

    assert response.status_code == 400
    assert response.json()["code"] == "VAL_002"


@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer wrong"}], ids=["none", "wrong"])
def test_head_on_health_is_rejected_like_any_closed_path(headers):
    """Docker healthcheck 는 GET 만 쓴다 — HEAD 는 열지 않는다."""
    api = client()

    closed = api.head("/v1/nope", headers=headers)
    response = api.head(HEAD_PROBE, headers=headers)

    assert closed.status_code == 401
    assert (response.status_code, dict(response.headers)) == (
        closed.status_code,
        dict(closed.headers),
    )
    assert "allow" not in response.headers


def test_an_authenticated_wrong_method_on_health_is_still_405():
    """자격이 있는 호출자는 라우팅까지 닿는다 — 405 는 그대로다 (열거 방어는 무자격 대상)."""
    response = client().post("/v1/health", headers=AUTH)

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
