"""The Control Plane requires a token once one is configured (#241).

이 표면은 파일을 쓰고 컨테이너를 띄우게 된다 (#242~). 그 전에 인증이 있어야 한다.
읽기도 보호한다 — 카탈로그에는 `env_allowlist` 같은 운영 정보가 있다.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from aaestro.catalog import Catalog
from aaestro.orchestrator import __main__ as entrypoint
from aaestro.orchestrator.control import create_app
from aaestro.orchestrator.runstore import InMemoryRunStore

REPO_ROOT = Path(__file__).resolve().parents[3]
TOKEN = "cp-secret"  # noqa: S105
AUTH = {"authorization": f"Bearer {TOKEN}"}


def client(token: str | None = TOKEN) -> httpx.AsyncClient:
    app = create_app(InMemoryRunStore(), catalog=Catalog.under(REPO_ROOT), token=token)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cp")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/runs"),
        ("GET", "/v1/runs/x"),
        ("POST", "/v1/runs/x/drain"),
        ("POST", "/v1/runs/x/resume"),
        ("GET", "/v1/agents"),
        ("GET", "/v1/agents/planner"),
        ("GET", "/v1/graphs"),
        ("GET", "/v1/groups"),
        ("GET", "/v1/modules/promptsets"),
    ],
)
async def test_every_route_rejects_a_missing_token(method, path):
    """읽기 하나라도 빠지면 그 경로로 운영 정보가 샌다 — 전수로 본다."""
    async with client() as api:
        response = await api.request(method, path)

    assert response.status_code == 401, path


async def test_the_right_token_passes():
    async with client() as api:
        response = await api.get("/v1/agents", headers=AUTH)

    assert response.status_code == 200


async def test_a_wrong_token_is_401_not_403():
    """401 은 "다시 제시하라", 403 은 "제시해도 안 된다" — 토큰 오류는 전자다."""
    async with client() as api:
        response = await api.get("/v1/agents", headers={"authorization": "Bearer nope"})

    assert response.status_code == 401
    assert "nope" not in response.text


async def test_health_stays_unauthenticated():
    """살아 있는지는 누구나 물을 수 있어야 한다 — 02 API Rules 4 와 같은 이유."""
    async with client() as api:
        response = await api.get("/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_no_token_means_no_check():
    """켜는 것은 조립하는 쪽의 결정 — 안전한지는 진입점이 판단한다."""
    async with client(token=None) as api:
        response = await api.get("/v1/agents")

    assert response.status_code == 200


# --- 진입점: loopback 밖 + 토큰 없음 → 거부 -----------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.9.9.9"])
def test_loopback_is_recognised(host):
    assert entrypoint.is_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "::", "example.internal"])  # noqa: S104
def test_anything_else_is_not_loopback(host):
    assert not entrypoint.is_loopback(host)


def _serve(monkeypatch, tmp_path, *, host: str, token: str | None):
    import yaml

    orchestrator = {
        "run_store": str(tmp_path / "runs.db"),
        "control_host": host,
        "control_port": 18999,
    }
    if token is not None:
        orchestrator["control_token"] = token
    (tmp_path / "local.yaml").write_text(
        yaml.safe_dump({"orchestrator": orchestrator}), encoding="utf-8"
    )
    monkeypatch.setenv("AAESTRO_ENV", "local")
    monkeypatch.setenv("AAESTRO_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AAESTRO_REPO_ROOT", str(REPO_ROOT))
    monkeypatch.setattr(entrypoint, "_setup_observability", lambda: None)
    served: list[object] = []
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **_kw: served.append(app))
    return served


def test_binding_outside_loopback_without_a_token_is_refused(monkeypatch, tmp_path):
    """무인증으로 밖에 열면 누구나 컨테이너를 띄울 수 있다."""
    from aaestro.core.errors import AaestroError, ErrorCode

    served = _serve(monkeypatch, tmp_path, host="0.0.0.0", token=None)  # noqa: S104

    with pytest.raises(AaestroError) as excinfo:
        entrypoint._start(None)

    assert excinfo.value.code == ErrorCode.CFG_001
    assert "control_token" in excinfo.value.message
    assert not served, "거부했는데 서빙을 시도했다"


def test_binding_outside_loopback_with_a_token_serves(monkeypatch, tmp_path):
    served = _serve(monkeypatch, tmp_path, host="0.0.0.0", token="t")  # noqa: S104

    entrypoint.main()

    assert served


def test_loopback_without_a_token_serves_with_a_warning(monkeypatch, tmp_path):
    """개발 편의 — 단 조용히는 아니다."""
    served = _serve(monkeypatch, tmp_path, host="127.0.0.1", token=None)

    entrypoint.main()

    assert served


# --- CLI 클라이언트가 토큰을 싣는다 ---------------------------------------------------


def test_the_cli_client_sends_the_bearer_header(monkeypatch):
    from aaestro.cli.control import ControlClient

    seen: dict = {}

    def fake_request(method, url, **kwargs):
        seen.update(kwargs)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(httpx, "request", fake_request)
    ControlClient("http://cp", token="abc").list_runs()

    assert seen["headers"] == {"authorization": "Bearer abc"}


def test_the_cli_client_sends_nothing_without_a_token(monkeypatch):
    from aaestro.cli.control import ControlClient

    seen: dict = {}

    def fake_request(method, url, **kwargs):
        seen.update(kwargs)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(httpx, "request", fake_request)
    ControlClient("http://cp").list_runs()

    assert seen["headers"] == {}


def test_the_cli_reads_the_token_from_the_environment(monkeypatch):
    """CLI 사용자는 매 명령마다 토큰을 타이핑하지 않는다."""
    import argparse

    from aaestro.cli.main import _control_client

    monkeypatch.setenv("AAESTRO_CONTROL_TOKEN", "from-env")

    built = _control_client(argparse.Namespace(control_url=None, control_token=None))

    assert built._headers == {"authorization": "Bearer from-env"}
