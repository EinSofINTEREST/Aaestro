"""The agent-side Memory Service client keeps one connection pool (#321).

auto-recall 과 ``memory_search`` 는 태스크마다 서비스를 부른다 — 요청마다 클라이언트를 만들고
닫으면 호출마다 커넥션을 새로 맺는다.
"""

from __future__ import annotations

import httpx
import pytest

from aaestro.memory import client as memory_client
from aaestro.memory.client import HttpMemoryAccess


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

    monkeypatch.setattr(memory_client.httpx, "AsyncClient", factory)
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


async def test_the_run_header_is_carried_per_call_only():
    """run 은 호출 인자다 — 넘기지 않은 호출에는 헤더가 없고, direct- run 도 싣지 않는다 (#610)."""
    seen: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get(memory_client.MEMORY_RUN_HEADER))
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        access = HttpMemoryAccess(base_url="http://memory:8090", token="t", client=http)
        await access.search("q", run_id="run-1")
        await access.search("q")
        await access.search("q", run_id="direct-9")
        await access.spaces(run_id="run-2")

    assert seen == ["run-1", None, None, "run-2"]


def test_the_client_holds_no_run():
    """run 기본값을 쥐는 클라이언트는 동시 태스크의 run 을 섞는다 — 생성자가 run 을 받지 않는다."""
    with pytest.raises(TypeError):
        HttpMemoryAccess(base_url="http://memory:8090", token="t", run_id="run-1")  # type: ignore[call-arg]
