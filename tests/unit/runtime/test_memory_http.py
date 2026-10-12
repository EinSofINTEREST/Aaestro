"""The agent-side Memory Service client keeps one connection pool (#321).

auto-recall 과 ``memory_search`` 는 태스크마다 서비스를 부른다 — 요청마다 클라이언트를 만들고
닫으면 호출마다 커넥션을 새로 맺는다.
"""

from __future__ import annotations

import httpx
import pytest

from aaestro.runtime import memory_http
from aaestro.runtime.memory_http import HttpMemoryAccess


@pytest.fixture
def created(monkeypatch) -> list[httpx.AsyncClient]:
    """이 모듈이 만드는 클라이언트를 기록한다 — 응답은 MockTransport 가 준다."""
    clients: list[httpx.AsyncClient] = []
    real = httpx.AsyncClient

    def factory(**kwargs):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
        client = real(transport=transport, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(memory_http.httpx, "AsyncClient", factory)
    return clients


async def test_consecutive_calls_share_one_client(created):
    access = HttpMemoryAccess(base_url="http://memory:8090", token="t")

    await access.search("first")
    await access.search("second")

    assert len(created) == 1
    assert not created[0].is_closed
    await access.aclose()


async def test_closing_releases_the_client_it_created(created):
    access = HttpMemoryAccess(base_url="http://memory:8090", token="t")
    await access.search("q")

    await access.aclose()

    assert created[0].is_closed


async def test_an_injected_client_stays_with_its_owner():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
    async with httpx.AsyncClient(transport=transport) as injected:
        access = HttpMemoryAccess(base_url="http://memory:8090", token="t", client=injected)
        await access.search("q")

        await access.aclose()

        assert not injected.is_closed
