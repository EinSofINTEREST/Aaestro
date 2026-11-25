"""Control plane body guard — duplicate JSON keys and the size cap (#667)."""

from __future__ import annotations

import httpx
import pytest

from aaestro.access.registry import AccessRegistry
from aaestro.access.store import InMemoryAccessStore
from aaestro.catalog import Catalog
from aaestro.materials import MAX_FILE_BYTES, MAX_FILES, MAX_PATH_CHARS
from aaestro.orchestrator.bodies import DEFAULT_BODY_LIMIT, MATERIALS_BODY_LIMIT
from aaestro.orchestrator.control import create_app
from aaestro.orchestrator.runstore import InMemoryRunStore
from tests.fixtures.access import STEWARD, access_workspace

TOKEN = "control-token"
AUTH = {"authorization": f"Bearer {TOKEN}"}
JSON = {"content-type": "application/json"}
STEER = "/v1/runs/x/steer"


@pytest.fixture
async def api():
    app = create_app(InMemoryRunStore(), token=TOKEN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        yield client


def padded(size: int) -> bytes:
    """정확히 ``size`` 바이트인 JSON 객체."""
    head, tail = b'{"text": "', b'"}'
    return head + b"a" * (size - len(head) - len(tail)) + tail


@pytest.mark.parametrize(
    ("body", "key"),
    [
        pytest.param(b'{"text": "SECRET1", "text": "SECRET2"}', "text", id="top"),
        pytest.param(b'{"a": {"b": 1, "b": 2}}', "b", id="nested"),
        pytest.param(b'{"a": [1, {"k": 1, "k": 2}]}', "k", id="in-array"),
        pytest.param(b'{"a": {"b": 1, "b": 2}, "a": {"c": 1}}', "b", id="overwritten-inner"),
    ],
)
async def test_duplicate_key_is_val_002_with_the_key_but_no_value(api, body, key):
    response = await api.post(STEER, content=body, headers={**JSON, **AUTH})

    error = response.json()["error"]
    assert (response.status_code, error["code"]) == (400, "VAL_002")
    assert error["details"] == {"key": key}
    assert "SECRET" not in response.text


async def test_unique_keys_still_reach_the_route(api):
    response = await api.post(STEER, json={"text": "go"}, headers=AUTH)

    assert response.status_code != 413
    assert "duplicate" not in response.text


async def test_body_at_the_limit_passes_and_one_more_byte_is_413(api):
    at = await api.post(STEER, content=padded(DEFAULT_BODY_LIMIT), headers={**JSON, **AUTH})
    over = await api.post(STEER, content=padded(DEFAULT_BODY_LIMIT + 1), headers={**JSON, **AUTH})

    assert at.status_code != 413
    assert over.status_code == 413
    assert over.json()["error"]["code"] == "VAL_002"


async def test_chunked_body_without_content_length_is_capped(api):
    async def chunks():
        for _ in range(DEFAULT_BODY_LIMIT // 1024 + 2):
            yield b"a" * 1024

    response = await api.post(STEER, content=chunks(), headers={**JSON, **AUTH})

    assert response.status_code == 413


async def test_without_a_token_a_huge_body_is_401_not_413(api):
    response = await api.post(STEER, content=padded(DEFAULT_BODY_LIMIT * 2), headers=JSON)

    assert response.status_code == 401


async def test_enforcer_route_is_guarded_after_its_credential(tmp_path):
    """access 의 강제 지점 라우트는 자기 라우터로 자격을 본다 — 거기도 가드가 걸린다."""
    registry = AccessRegistry(
        store=InMemoryAccessStore(),
        catalog=Catalog.under(access_workspace(tmp_path)),
        stewards=frozenset({STEWARD}),
    )
    app = create_app(
        InMemoryRunStore(),
        catalog=registry.catalog,
        token=TOKEN,
        access=registry,
        enforcer_token="e",
    )
    path, headers = "/v1/access/decisions", {**JSON, "authorization": "Bearer e"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cp") as c:
        dup = await c.post(path, content=b'{"a": 1, "a": 2}', headers=headers)
        big = await c.post(path, content=padded(DEFAULT_BODY_LIMIT + 1), headers=headers)
        anon = await c.post(path, content=padded(DEFAULT_BODY_LIMIT + 1), headers=JSON)

    assert (dup.status_code, big.status_code, anon.status_code) == (400, 413, 401)


def test_materials_cap_fits_the_worst_case_escaped_legal_upload():
    """제어 문자는 JSON 에서 문자당 6 바이트 (``\\u0000``) — 합법 최대 업로드가 상한 안이다."""
    assert MATERIALS_BODY_LIMIT >= MAX_FILES * (6 * MAX_FILE_BYTES + 12 * MAX_PATH_CHARS)


async def test_a_huge_content_length_is_413_not_a_crash(api):
    response = await api.post(
        STEER, content=b"{}", headers={**JSON, **AUTH, "content-length": "9" * 5000}
    )

    assert response.status_code == 413


async def test_a_content_length_with_leading_zeros_is_read_by_value(api):
    response = await api.post(
        STEER, content=b"{}", headers={**JSON, **AUTH, "content-length": "0" * 20 + "2"}
    )

    assert response.status_code != 413
