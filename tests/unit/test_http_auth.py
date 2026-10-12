"""One bearer check for every HTTP surface (#241)."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from aaestro.http_auth import presented_token, require_token


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
