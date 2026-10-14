"""One bearer check for every HTTP surface (#241)."""

from __future__ import annotations

import pytest
import structlog.testing
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.http_auth import (
    ALLOW_UNAUTHENTICATED_ENV,
    admit_first,
    presented_token,
    require_token,
    resolve_required_token,
)


def app_with(expected: str | None) -> TestClient:
    app = FastAPI()

    @app.get("/guarded", dependencies=[Depends(require_token(expected, realm="test token"))])
    async def guarded() -> dict[str, str]:
        return {"ok": "yes"}

    return TestClient(app)


def test_the_right_token_passes():
    assert (
        app_with("s3cret").get("/guarded", headers={"authorization": "Bearer s3cret"}).status_code
        == 200
    )


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": "Bearer wrong"},
        {"authorization": "s3cret"},
        {"authorization": "Basic s3cret"},
        {"authorization": "Bearer "},
    ],
)
def test_anything_else_is_401(headers):
    response = app_with("s3cret").get("/guarded", headers=headers)

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


def test_no_expected_token_means_no_check():
    """켜는 것은 조립하는 쪽의 결정이다 — 미설정이면 통과."""
    assert app_with(None).get("/guarded").status_code == 200


def test_the_rejection_names_the_realm_not_the_token():
    """사유에 토큰 값이 섞이면 로그로 샌다."""
    body = app_with("s3cret").get("/guarded", headers={"authorization": "Bearer nope"}).json()

    assert body["detail"] == "invalid test token"
    assert "nope" not in body["detail"] and "s3cret" not in body["detail"]


class _Req:
    def __init__(self, value: str | None) -> None:
        self.headers = {} if value is None else {"authorization": value}


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc", "abc"),
        ("bearer abc", None),
        ("Bearer ", None),
        (None, None),
        ("Token abc", None),
    ],
)
def test_presented_token_is_strict_about_the_scheme(header, expected):
    assert presented_token(_Req(header)) == expected  # type: ignore[arg-type]


def test_the_comparison_is_constant_time(monkeypatch):
    """일반 `!=` 는 첫 불일치에서 끝나 토큰을 한 바이트씩 맞춰 볼 수 있다.

    타이밍 자체를 재는 것은 불안정하다 — 비교가 `hmac.compare_digest` 를 **거치는지**를 본다.
    """
    import hmac

    seen: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a, b):
        seen.append((bytes(a), bytes(b)))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    app_with("s3cret").get("/guarded", headers={"authorization": "Bearer wrong"})

    assert seen == [(b"wrong", b"s3cret")]


def test_a_missing_token_still_goes_through_the_constant_time_path(monkeypatch):
    """없는 토큰을 먼저 걸러 내면 그 분기가 타이밍으로 드러난다."""
    import hmac

    calls: list[int] = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))

    app_with("s3cret").get("/guarded")

    assert calls == [1]


# --- 기동 시 토큰 판단 (#394) ----------------------------------------------------------

TOKEN_KEY = "AAESTRO_TEST_TOKEN"  # noqa: S105


def resolve(environ: dict[str, str], host: str = "0.0.0.0") -> str | None:  # noqa: S104
    return resolve_required_token(environ, token_env=TOKEN_KEY, bind_host=host, component="test")


def test_resolve_missing_token_is_refused_naming_the_env():
    with pytest.raises(AaestroError) as exc:
        resolve({})

    assert exc.value.code == ErrorCode.CFG_001
    assert TOKEN_KEY in exc.value.message


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_resolve_blank_token_is_refused(value):
    with pytest.raises(AaestroError) as exc:
        resolve({TOKEN_KEY: value})

    assert exc.value.code == ErrorCode.CFG_001


def test_resolve_token_present_is_returned_and_never_echoed():
    assert resolve({TOKEN_KEY: "s3cret-value"}) == "s3cret-value"


def test_resolve_refusal_never_contains_a_token_value():
    # 토큰은 있지만 opt-in 값이 잘못된 경우에도 값이 메시지에 실리지 않는다
    with pytest.raises(AaestroError) as exc:
        resolve({TOKEN_KEY: "s3cret-value", ALLOW_UNAUTHENTICATED_ENV: "maybe"})

    assert "s3cret-value" not in str(exc.value)
    assert "s3cret-value" not in repr(exc.value.details)


def test_resolve_opt_in_on_loopback_allows_and_warns():
    with structlog.testing.capture_logs() as logs:
        token = resolve({ALLOW_UNAUTHENTICATED_ENV: "1"}, host="127.0.0.1")

    assert token is None
    warnings = [entry for entry in logs if entry["log_level"] == "warning"]
    assert len(warnings) == 1
    assert warnings[0]["host"] == "127.0.0.1"


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "::"])  # noqa: S104
def test_resolve_opt_in_outside_loopback_is_refused(host):
    with pytest.raises(AaestroError) as exc:
        resolve({ALLOW_UNAUTHENTICATED_ENV: "true"}, host=host)

    assert exc.value.code == ErrorCode.CFG_001
    assert "loopback" in exc.value.message


def test_resolve_unknown_opt_in_value_is_refused():
    with pytest.raises(AaestroError):
        resolve({ALLOW_UNAUTHENTICATED_ENV: "yes please"}, host="127.0.0.1")


def test_resolve_opt_in_off_does_not_allow_missing_token():
    with pytest.raises(AaestroError):
        resolve({ALLOW_UNAUTHENTICATED_ENV: "0"}, host="127.0.0.1")


@pytest.mark.parametrize("value", ["abc\n", " abc", "abc ", "\tabc"])
def test_resolve_token_with_surrounding_whitespace_is_refused_without_the_value(value):
    with pytest.raises(AaestroError) as exc:
        resolve({TOKEN_KEY: value})

    assert exc.value.code == ErrorCode.CFG_001
    assert "whitespace" in exc.value.message
    assert "abc" not in str(exc.value)
    assert "abc" not in repr(exc.value.details)


def test_resolve_refusal_never_contains_the_bind_value():
    marker = "marker-host.invalid"

    with pytest.raises(AaestroError) as exc:
        resolve({ALLOW_UNAUTHENTICATED_ENV: "1"}, host=marker)

    assert marker not in str(exc.value)
    assert marker not in repr(exc.value.details)


def test_resolve_opt_in_ignored_because_of_a_token_is_warned():
    with structlog.testing.capture_logs() as logs:
        token = resolve({TOKEN_KEY: "s3cret", ALLOW_UNAUTHENTICATED_ENV: "1"}, host="127.0.0.1")

    assert token == "s3cret"  # noqa: S105
    warnings = [entry for entry in logs if entry["log_level"] == "warning"]
    assert len(warnings) == 1
    assert "s3cret" not in repr(warnings)


# --- 본문보다 먼저 (#462) ---------------------------------------------------------------


class _Body(BaseModel):
    text: str


def admitting_app(admit) -> tuple[TestClient, list[str]]:
    """검사를 라우트 클래스로 건 앱 — 핸들러가 본문을 받았는지 기록한다."""
    seen: list[str] = []
    router = APIRouter(route_class=admit_first(admit))

    @router.post("/guarded")
    async def guarded(body: _Body) -> dict[str, str]:
        seen.append(body.text)
        return {"ok": "yes"}

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False), seen


@pytest.mark.parametrize("body", [b"{not", b"\xff\xfd", b'{"text": 5}', b""])
def test_the_route_class_refuses_before_the_body_is_read(body):
    """`Depends` 였다면 깨진 본문이 400/422 로 먼저 나간다."""
    client, seen = admitting_app(require_token("s3cret", realm="test token"))

    response = client.post("/guarded", content=body, headers={"content-type": "application/json"})

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid test token"}
    assert response.headers.get("www-authenticate") == "Bearer"
    assert seen == []


def test_the_route_class_lets_the_right_token_through_to_the_body():
    client, seen = admitting_app(require_token("s3cret", realm="test token"))

    response = client.post(
        "/guarded", json={"text": "hi"}, headers={"authorization": "Bearer s3cret"}
    )

    assert response.status_code == 200
    assert seen == ["hi"]


def test_the_route_class_awaits_an_async_admission():
    calls: list[str] = []

    async def admit(request: Request) -> None:
        calls.append(request.url.path)

    client, seen = admitting_app(admit)

    assert client.post("/guarded", json={"text": "hi"}).status_code == 200
    assert calls == ["/guarded"]
    assert seen == ["hi"]


def test_the_route_class_lets_a_typed_refusal_through():
    """신원 검사가 레지스트리라면 거절은 ``AaestroError`` 다 — 그대로 앱의 핸들러로 간다."""

    def admit(_request: Request) -> None:
        raise AaestroError(category=ErrorCategory.FORBIDDEN, code=ErrorCode.ACC_001, message="no")

    async def forbidden(_request: Request, err: Exception) -> JSONResponse:
        assert isinstance(err, AaestroError)
        return JSONResponse(status_code=403, content={"code": str(err.code)})

    client, seen = admitting_app(admit)
    client.app.add_exception_handler(AaestroError, forbidden)  # type: ignore[attr-defined]

    response = client.post(
        "/guarded", content=b"{not", headers={"content-type": "application/json"}
    )

    assert (response.status_code, response.json()) == (403, {"code": "ACC_001"})
    assert seen == []
