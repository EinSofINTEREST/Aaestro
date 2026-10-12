"""Memory Service HTTP surface and its client.

에이전트는 HTTP 로만 메모리에 닿는다 — **컨테이너에 DB 자격증명을 주지 않기
위해서다** (09 Access Enforcement 1). 여기서는 그 왕복과 경계를 검증한다.
"""

from __future__ import annotations

import httpx
import pytest

from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.memory.entry import MemoryEntry, MemorySource
from aaestro.memory.http import TokenRegistry, create_app
from aaestro.memory.index import IndexRegistry
from aaestro.memory.recall import Recall
from aaestro.memory.service import MemoryService, build_token
from aaestro.memory.store import SqliteMemoryStore
from aaestro.modules.memoryset import MemoryKind
from aaestro.runtime.memory_http import HttpMemoryAccess

BASE_URL = "http://memory.test"


@pytest.fixture
def served():
    """서비스 앱과 그것을 통해 말하는 클라이언트."""
    store = SqliteMemoryStore()
    service = MemoryService(store=store)
    token = build_token(agent="researcher", group=None, local=[("longterm", "researcher")])
    space_id = token.resolve("longterm").space_id  # type: ignore[union-attr]

    indexer = IndexRegistry()
    tokens = TokenRegistry()
    secret = tokens.issue(token)
    app = create_app(service, Recall(indexes=indexer.indexes), tokens, indexer=indexer)

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)
    access = HttpMemoryAccess(base_url=BASE_URL, token=secret, client=client)
    try:
        yield access, service, token, space_id, indexer, tokens
    finally:
        store.close()


def entry(space: str, content: str) -> MemoryEntry:
    return MemoryEntry(
        space=space,
        kind=MemoryKind.FACT,
        content=content,
        source=MemorySource(agent="researcher"),
    )


# --- 왕복 --------------------------------------------------------------------


async def test_append_then_search_round_trips(served):
    """색인은 **서비스가** 한다 — 테스트가 대신 색인하면 배선을 지워도 통과한다."""
    access, _service, _token, space_id, indexer, _tokens = served
    item = entry(space_id, "mcp sidecar 는 이미지 태그 고정이 필요하다")

    await access.append("longterm", entry=item)
    indexer.drain()  # 09 Write Path — 색인은 비동기다
    found = await access.search("sidecar")

    assert [scored.entry.entry_id for scored in found] == [item.entry_id]
    assert found[0].space == space_id


async def test_spaces_reports_what_this_token_reaches(served):
    access, _service, _token, _space_id, _indexes, _tokens = served

    listed = await access.spaces()

    assert [space["alias"] for space in listed] == ["longterm"]


# --- 경계 --------------------------------------------------------------------


async def test_undeclared_space_is_denied(served):
    """선언되지 않은 space 는 서비스가 거부한다 — 클라이언트가 판단하지 않는다."""
    access, *_ = served

    with pytest.raises(AaestroError) as exc_info:
        await access.search("무엇이든", spaces=["secret"])

    assert exc_info.value.code == ErrorCode.MEM_001
    assert not exc_info.value.retryable


async def test_unknown_token_is_denied(served):
    """토큰을 위조할 수 없다 — 서비스가 발급분만 인정한다."""
    access, *_ = served
    forged = HttpMemoryAccess(base_url=BASE_URL, token="forged", client=access.client)

    with pytest.raises(AaestroError) as exc_info:
        await forged.spaces()

    assert exc_info.value.code == ErrorCode.MEM_001


async def test_missing_token_is_denied(served):
    access, *_ = served
    anonymous = HttpMemoryAccess(base_url=BASE_URL, token="", client=access.client)

    with pytest.raises(AaestroError) as exc_info:
        await anonymous.spaces()

    assert exc_info.value.code == ErrorCode.MEM_001


async def test_forgotten_token_stops_working(served):
    """그룹 이동이나 재배포 시 즉시 무효화된다."""
    access, _service, _token, _space_id, _indexes, tokens = served
    tokens.forget(access.token)

    with pytest.raises(AaestroError):
        await access.spaces()


# --- 장애와 거부의 구분 ----------------------------------------------------------


async def test_unreachable_service_is_retryable():
    """거부와 장애를 같게 다루면 재시도 판단이 어긋난다."""

    async def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url=BASE_URL)
    access = HttpMemoryAccess(base_url=BASE_URL, token="t", client=client)

    with pytest.raises(AaestroError) as exc_info:
        await access.search("q")

    assert exc_info.value.code == ErrorCode.MEM_004
    assert exc_info.value.retryable


async def test_server_error_is_retryable():
    def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"message": "index rebuilding"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(fail), base_url=BASE_URL)
    access = HttpMemoryAccess(base_url=BASE_URL, token="t", client=client)

    with pytest.raises(AaestroError) as exc_info:
        await access.search("q")

    assert exc_info.value.retryable


# --- read / latest -------------------------------------------------------------


async def test_read_returns_the_space_newest_first(served):
    """검색 없이 훑는 창구가 없으면 클라이언트가 space 를 읽을 방법이 없다."""
    access, _service, _token, space_id, _indexer, _tokens = served

    first = await access.append("longterm", entry=entry(space_id, "먼저"))
    second = await access.append("longterm", entry=entry(space_id, "나중"))

    found = await access.read("longterm")

    assert [item.entry_id for item in found] == [second.entry_id, first.entry_id]


async def test_read_honours_the_limit(served):
    access, _service, _token, space_id, _indexer, _tokens = served
    for text in ("하나", "둘", "셋"):
        await access.append("longterm", entry=entry(space_id, text))

    found = await access.read("longterm", limit=2)

    assert len(found) == 2


async def test_read_rejects_an_undeclared_space(served):
    """경계는 모든 창구에서 같아야 한다 — 한 곳만 열려도 ACL 이 무의미하다."""
    access, _service, _token, _space_id, _indexer, _tokens = served

    with pytest.raises(AaestroError) as exc_info:
        await access.read("someone-elses")

    assert exc_info.value.code == ErrorCode.MEM_001


async def test_latest_follows_a_correction_chain(served):
    """정정된 기억을 그대로 읽으면 모델이 틀린 사실을 본다 (09 Rule 4)."""
    access, _service, _token, space_id, _indexer, _tokens = served
    original = await access.append("longterm", entry=entry(space_id, "포트는 8000 이다"))
    correction = MemoryEntry(
        space=space_id,
        kind=MemoryKind.FACT,
        content="포트는 8080 이다",
        source=MemorySource(agent="researcher"),
        supersedes=original.entry_id,
    )
    await access.append("longterm", entry=correction)

    found = await access.latest("longterm", original.entry_id)

    assert found is not None
    assert found.content == "포트는 8080 이다"


async def test_latest_rejects_an_undeclared_space(served):
    access, _service, _token, _space_id, _indexer, _tokens = served

    with pytest.raises(AaestroError) as exc_info:
        await access.latest("someone-elses", "any-id")

    assert exc_info.value.code == ErrorCode.MEM_001


# --- 창구마다 같은 실패 변환 ------------------------------------------------------


async def test_spaces_maps_transport_failure_to_mem_004():
    """같은 장애가 창구에 따라 다른 타입으로 나오면 재시도 판단이 갈라진다."""
    access = HttpMemoryAccess(base_url="http://127.0.0.1:1", token="opaque", timeout_s=0.2)

    with pytest.raises(AaestroError) as exc_info:
        await access.spaces()

    assert exc_info.value.code == ErrorCode.MEM_004
    assert exc_info.value.retryable


async def test_read_maps_transport_failure_to_mem_004():
    access = HttpMemoryAccess(base_url="http://127.0.0.1:1", token="opaque", timeout_s=0.2)

    with pytest.raises(AaestroError) as exc_info:
        await access.read("longterm")

    assert exc_info.value.code == ErrorCode.MEM_004


# --- 광고 --------------------------------------------------------------------
# `/v1/spaces` 는 에이전트가 **자기 범위를 확인하는** 창구다. 광고가 실제 권한과
# 어긋나면 에이전트는 자기 권한을 잘못 알고, 쓰기를 시도해 401 을 받는다 (#188)


def advertise(agent: str, *, group: str | None = None, **declarations) -> list[dict[str, str]]:
    """한 에이전트에게 `/v1/spaces` 가 무엇을 광고하는지."""
    from fastapi.testclient import TestClient

    tokens = TokenRegistry()
    secret = tokens.issue(build_token(agent=agent, group=group, **declarations))
    indexer = IndexRegistry()
    store = SqliteMemoryStore()
    try:
        app = create_app(
            MemoryService(store=store), Recall(indexes=indexer.indexes), tokens, indexer=indexer
        )
        with TestClient(app) as client:
            return client.get(  # type: ignore[no-any-return]
                "/v1/spaces", headers={"Authorization": f"Bearer {secret}"}
            ).json()
    finally:
        store.close()


def mode_of(spaces: list[dict[str, str]], alias: str) -> str:
    return next(space["mode"] for space in spaces if space["alias"] == alias)


def test_a_global_space_is_advertised_read_only_without_writers():
    """`writers` 미지정이면 전 에이전트 read-only — rw 로 광고하면 거짓말이다."""
    spaces = advertise("researcher", global_spaces=[("org", ())])

    assert mode_of(spaces, "org") == "ro"


def test_a_global_space_is_advertised_writable_to_a_declared_writer():
    """쓸 수 있는 에이전트에게까지 ro 로 보이면 반대 방향으로 틀린다."""
    spaces = advertise("librarian", global_spaces=[("org", ("librarian",))])

    assert mode_of(spaces, "org") == "rw"


def test_a_local_space_is_still_advertised_writable():
    """local 은 mode 가 정본이다 — global 수정이 나머지를 끌고 가면 안 된다."""
    spaces = advertise("researcher", local=[("longterm", "researcher")])

    assert mode_of(spaces, "longterm") == "rw"


def test_a_read_only_group_space_is_advertised_read_only():
    """group 의 ro 선언도 그대로 보여야 한다."""
    from aaestro.core.manifest import MemoryMode as Mode

    spaces = advertise("researcher", group="research", group_spaces=[("knowledge", Mode.RO)])

    assert mode_of(spaces, "knowledge") == "ro"


def test_the_advertised_mode_matches_what_append_actually_does():
    """광고와 실제가 갈리면 이 endpoint 는 쓸모가 없다."""
    from fastapi.testclient import TestClient

    tokens = TokenRegistry()
    secret = tokens.issue(build_token(agent="researcher", group=None, global_spaces=[("org", ())]))
    indexer = IndexRegistry()
    store = SqliteMemoryStore()
    try:
        app = create_app(
            MemoryService(store=store), Recall(indexes=indexer.indexes), tokens, indexer=indexer
        )
        headers = {"Authorization": f"Bearer {secret}"}
        with TestClient(app) as client:
            advertised = mode_of(client.get("/v1/spaces", headers=headers).json(), "org")
            written = client.post(
                "/v1/append",
                json={"space": "org", "entry": entry("org", "x").model_dump(mode="json")},
                headers=headers,
            )
    finally:
        store.close()

    assert advertised == "ro"
    assert written.status_code == 401
