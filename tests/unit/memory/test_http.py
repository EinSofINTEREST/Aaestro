"""Memory Service HTTP surface and its client.

에이전트는 HTTP 로만 메모리에 닿는다 — **컨테이너에 DB 자격증명을 주지 않기
위해서다** (09 Access Enforcement 1). 여기서는 그 왕복과 경계를 검증한다.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.memory.client import HttpMemoryAccess
from aaestro.memory.entry import MemoryEntry, MemorySource
from aaestro.memory.http import TokenRegistry, create_app
from aaestro.memory.index import IndexRegistry
from aaestro.memory.recall import Recall
from aaestro.memory.service import MemoryService, build_token
from aaestro.memory.store import SqliteMemoryStore
from aaestro.modules.memoryset import MemoryKind

BASE_URL = "http://memory.test"
REPO_ROOT = Path(__file__).resolve().parents[3]


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
    app = create_app(service, Recall(indexes=indexer.indexes, store=store), tokens, indexer=indexer)

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
            MemoryService(store=store),
            Recall(indexes=indexer.indexes, store=store),
            tokens,
            indexer=indexer,
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
            MemoryService(store=store),
            Recall(indexes=indexer.indexes, store=store),
            tokens,
            indexer=indexer,
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


# --- 검색 경계 (#324) -----------------------------------------------------------
# get_many 는 service.read 를 거치지 않는다 — 별칭 해석이 유일한 경계이므로 부류로 지킨다


OTHER_SPACE = "local:writer:longterm"


async def _index_foreign_memory(served):
    """다른 에이전트의 space 에 기억을 저장하고 색인한다 — 새면 검색에 나온다."""
    from aaestro.modules.memoryset import ChunkSpec

    _access, service, _token, _space_id, indexer, _tokens = served
    secret = service.store.append(entry(OTHER_SPACE, "another agent private sidecar secret"))
    indexer.submit(secret, ChunkSpec())
    indexer.drain()
    return secret


@pytest.mark.parametrize("target", [OTHER_SPACE, "secret", "group:research:knowledge"])
async def test_search_refuses_spaces_this_token_does_not_declare(served, target):
    """다른 에이전트의 space id, 미선언 별칭, 선언 밖 id 는 모두 MEM_001 — 새면 기억이 샌다."""
    access, *_ = served
    await _index_foreign_memory(served)

    with pytest.raises(AaestroError) as exc_info:
        await access.search("sidecar secret", spaces=[target])

    assert exc_info.value.code == ErrorCode.MEM_001


# --- 정정 반영은 검색이 한다 (#324) ------------------------------------------------


async def _fill_with_a_correction(served):
    """원본과 정정 뒤에 filler 를 쌓는다 — 둘 다 어떤 최근 N건 창에도 들지 않는다."""
    access, service, token, space_id, indexer, _tokens = served
    old = entry(space_id, "the port is 9000 for the sidecar")
    new = entry(space_id, "the port is 9100 for the sidecar").model_copy(
        update={"supersedes": old.entry_id}
    )
    for item in (old, new):
        await access.append("longterm", entry=item)
    for i in range(30):
        await access.append("longterm", entry=entry(space_id, f"filler record {i} about nothing"))
    indexer.drain()
    return old, new


async def test_http_search_returns_only_the_correction(served):
    access, *_ = served
    old, new = await _fill_with_a_correction(served)

    found = await access.search("sidecar port", k=5)

    ids = [scored.entry.entry_id for scored in found]
    assert new.entry_id in ids
    assert old.entry_id not in ids


async def test_memory_search_tool_returns_only_the_correction(served):
    from aaestro.memory.tool import run_memory_search

    access, *_ = served
    await _fill_with_a_correction(served)

    results = await run_memory_search(access, {"query": "sidecar port", "k": 5})

    contents = [r["content"] for r in results]
    assert "the port is 9100 for the sidecar" in contents
    assert "the port is 9000 for the sidecar" not in contents


async def test_http_auto_recall_injects_only_the_correction(served):
    from aaestro.modules.memoryset import RecallSpec

    access, *_ = served
    await _fill_with_a_correction(served)

    context = await access.recall_for_task(
        "sidecar port", policy=RecallSpec(auto=True, k=5, min_score=0.0)
    )

    assert "9100" in context
    assert "9000" not in context


# --- 성공한 검색도 감사에 남는다 (#324) --------------------------------------------


async def test_a_successful_search_is_audited_per_space(served):
    access, service, *_ = served
    await access.append("longterm", entry=entry(served[3], "mcp sidecar 는 태그 고정이 필요하다"))
    served[4].drain()

    await access.search("sidecar")

    record = service.audit_log[-1]
    assert record["op"] == "search"
    assert record["status"] == "ok"
    assert record["memory_space"] == "longterm"
    assert record["agent"] == "researcher"


# --- 다른 space 의 정정은 기억을 숨기지 못한다 (#324) ---------------------------------


@pytest.fixture
def org_stack(served):
    """org 를 읽기만 하는 공격자와 피해자, org 에 쓰는 사서를 같은 서비스에 붙인다."""
    access, service, _token, _space_id, indexer, tokens = served
    client = access.client

    def agent(name: str, *, writers: tuple[str, ...] = ()) -> HttpMemoryAccess:
        token = build_token(
            agent=name,
            group=None,
            local=[("longterm", name)],
            global_spaces=[("org", writers)],
        )
        return HttpMemoryAccess(base_url=BASE_URL, token=tokens.issue(token), client=client)

    return agent("librarian", writers=("librarian",)), agent("attacker"), agent("victim"), indexer


async def test_a_read_only_agent_cannot_hide_a_shared_memory_with_a_correction(org_stack):
    librarian, attacker, victim, indexer = org_stack
    org = await librarian.append("org", entry=entry("global:global:org", "qqorg policy approved"))
    indexer.drain()
    seen = await attacker.search("qqorg policy", spaces=["org"])
    assert [s.entry.entry_id for s in seen] == [org.entry_id], "공격자는 id 를 읽을 수 있다"

    forged = entry("local:attacker:longterm", "hide it").model_copy(
        update={"supersedes": org.entry_id}
    )
    with pytest.raises(AaestroError) as exc_info:
        await attacker.append("longterm", entry=forged)

    refused = exc_info.value
    assert refused.code == ErrorCode.VAL_002
    assert not refused.retryable
    assert org.entry_id not in str(refused)
    assert org.entry_id not in str(refused.details)
    found = await victim.search("qqorg policy", spaces=["org"])
    assert [s.entry.entry_id for s in found] == [org.entry_id]


async def test_a_correction_stored_out_of_band_is_ignored_by_search(org_stack, served):
    """append 검사를 우회해 저장소에 들어간 교차 space 정정도 검색은 무시한다."""
    librarian, _attacker, victim, indexer = org_stack
    _access, service, *_ = served
    org = await librarian.append("org", entry=entry("global:global:org", "qqorg policy approved"))
    indexer.drain()
    service.store.append(
        entry("local:attacker:longterm", "hide it").model_copy(update={"supersedes": org.entry_id})
    )

    found = await victim.search("qqorg policy", spaces=["org"])

    assert [s.entry.entry_id for s in found] == [org.entry_id]


async def test_unknown_and_foreign_correction_targets_get_the_same_response(org_stack):
    """없는 id 와 다른 space 의 id 에 같은 응답을 줘야 다른 space 의 id 존재 여부가 새지 않는다."""
    librarian, attacker, _victim, _indexer = org_stack
    org = await librarian.append("org", entry=entry("global:global:org", "qqorg policy approved"))

    async def probe(target: str) -> httpx.Response:
        forged = entry("local:attacker:longterm", "hide it").model_copy(
            update={"supersedes": target}
        )
        return await attacker.client.post(
            f"{BASE_URL}/v1/append",
            json={"space": "longterm", "entry": forged.model_dump(mode="json")},
            headers={"authorization": f"Bearer {attacker.token}"},
        )

    missing, foreign = await probe("no-such-entry"), await probe(org.entry_id)

    assert missing.status_code == foreign.status_code == 400
    assert missing.json() == foreign.json()
    assert missing.json()["code"] == "VAL_002"


# --- 검색 k 상한 -------------------------------------------------------------


def test_search_request_bounds_k():
    from pydantic import ValidationError

    from aaestro.memory.http import MAX_SEARCH_K, SearchRequest

    assert SearchRequest(query="q", k=MAX_SEARCH_K).k == MAX_SEARCH_K
    for bad in (0, MAX_SEARCH_K + 1):
        with pytest.raises(ValidationError):
            SearchRequest(query="q", k=bad)


async def test_http_search_rejects_an_oversized_k_as_val_002(served):
    from aaestro.memory.http import MAX_SEARCH_K

    access, *_ = served
    response = await access.client.post(
        f"{BASE_URL}/v1/search",
        headers={"authorization": f"Bearer {access.token}"},
        json={"query": "sidecar", "k": MAX_SEARCH_K + 1},
    )

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "VAL_002"
    assert body["details"]["errors"][0]["field"] == "body.k"
    assert str(MAX_SEARCH_K + 1) not in str(body["details"])


# --- 인증은 본문 파싱보다 먼저 (#450) -------------------------------------------

HEALTH_PATH = "/v1/health"
AUTH_FIRST_BODIES = {
    "malformed_shape": b'{"query": 5, "space": 5, "k": "x", "entry": 1, "limit": "z"}',
    "broken_json": b"{not json",
    "broken_utf8": b"\xff\xfe\xfd",
    "empty": b"",
    # 어느 라우트에도 형식이 맞는 본문은 없다 — 라우트별로 맞는 본문은 아래 WELL_FORMED
}
WELL_FORMED = {
    "/v1/search": b'{"query": "q"}',
    "/v1/append": (
        b'{"space": "longterm", "entry": {"space": "longterm", "kind": "fact", "content": "c",'
        b' "source": {"agent": "researcher"}}}'
    ),
    "/v1/read": b'{"space": "longterm"}',
    "/v1/latest": b'{"space": "longterm", "entry_id": "e"}',
    "/v1/spaces": b"",
}
BAD_CREDENTIALS = {"no_token": {}, "wrong_token": {"authorization": "Bearer not-issued"}}


def _api_routes(app) -> list[tuple[str, str]]:
    """인증이 걸린 라우트 — 무인증 `/v1/health` 는 의도된 예외라 뺀다 (아래 전용 테스트)."""
    from fastapi.routing import APIRoute

    return sorted(
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if route.path != HEALTH_PATH
    )


def _auth_first_app(*, gate=None):
    store = SqliteMemoryStore()
    indexer = IndexRegistry()
    tokens = TokenRegistry()
    app = create_app(
        MemoryService(store=store),
        Recall(indexes=indexer.indexes, store=store),
        None if gate else tokens,
        indexer=indexer,
        gate=gate,
    )
    return app, tokens, store


@pytest.mark.parametrize("body", [*AUTH_FIRST_BODIES, "well_formed"])
@pytest.mark.parametrize("credentials", list(BAD_CREDENTIALS))
async def test_every_route_refuses_without_identity_before_reading_the_body(credentials, body):
    app, _, store = _auth_first_app()
    routes = _api_routes(app)
    # 라우트가 추가되면 이 목록이 자동으로 따라온다 — 빠진 라우트는 같은 표 안에서 실패한다
    assert len(routes) >= 5
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)
    try:
        for method, path in routes:
            response = await client.request(
                method,
                path,
                content=WELL_FORMED[path] if body == "well_formed" else AUTH_FIRST_BODIES[body],
                headers={"content-type": "application/json", **BAD_CREDENTIALS[credentials]},
            )
            payload = response.json()
            assert (response.status_code, payload["code"]) == (401, "MEM_001"), (path, payload)
            # 필드 경로나 받은 값이 응답에 없다 — 본문을 보지 않았다는 증거
            assert payload["details"] == {}, (path, payload)
            expected = "required" if credentials == "no_token" else "not recognised"
            assert expected in payload["message"], (path, payload)
    finally:
        await client.aclose()
        store.close()


@pytest.mark.parametrize("body", ["malformed_shape", "broken_json", "broken_utf8", "empty"])
async def test_a_valid_token_with_a_bad_body_still_gets_a_validation_refusal(body):
    app, tokens, store = _auth_first_app()
    secret = tokens.issue(build_token(agent="researcher", group=None, local=[("longterm", "r")]))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)
    try:
        response = await client.post(
            "/v1/search",
            content=AUTH_FIRST_BODIES[body],
            headers={"content-type": "application/json", "authorization": f"Bearer {secret}"},
        )
        assert response.status_code == 400, response.text
        assert response.json()["code"] in {"VAL_001", "VAL_002"}
        assert "detail" not in response.json()
    finally:
        await client.aclose()
        store.close()


async def test_registry_mode_refuses_an_unknown_identity_before_reading_the_body():
    from aaestro.access.client import DecisionSource
    from aaestro.catalog import Catalog
    from aaestro.memory.gate import RegistryGate

    class UnknownIdentity:
        async def identify(self, credential):
            return None, DecisionSource.FRESH

    gate = RegistryGate(client=UnknownIdentity(), catalog=Catalog.under(Path()))  # type: ignore[arg-type]
    app, _, store = _auth_first_app(gate=gate)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)
    try:
        for method, path in _api_routes(app):
            response = await client.request(
                method,
                path,
                content=AUTH_FIRST_BODIES["malformed_shape"],
                headers={"content-type": "application/json", "authorization": "Bearer who"},
            )
            assert (response.status_code, response.json()["code"]) == (401, "MEM_001"), path
            assert response.json()["details"] == {}
    finally:
        await client.aclose()
        store.close()


async def test_identity_is_checked_once_per_request():
    from aaestro.access.client import DecisionSource, Verdict
    from aaestro.catalog import Catalog
    from aaestro.memory.gate import RegistryGate

    class Counting:
        identified = 0

        async def identify(self, credential):
            self.identified += 1
            return "researcher", DecisionSource.FRESH

        async def decide(self, credential, kind, target, mode=None):
            return Verdict("researcher", True, "operator", DecisionSource.FRESH)

    counting = Counting()
    gate = RegistryGate(client=counting, catalog=Catalog.under(REPO_ROOT))  # type: ignore[arg-type]
    app, _, store = _auth_first_app(gate=gate)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)
    try:
        response = await client.post(
            "/v1/read", json={"space": "longterm"}, headers={"authorization": "Bearer who"}
        )
        assert response.status_code == 200, response.text
        assert counting.identified == 1
    finally:
        await client.aclose()
        store.close()


async def test_health_is_the_only_route_open_without_identity():
    app, _, store = _auth_first_app()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)
    try:
        response = await client.get(HEALTH_PATH)
        assert (response.status_code, response.json()) == (200, {"status": "ok"})
        # 문서 경로는 열지 않는다 — 스키마(필드 · 상한)를 열거하지 못하게
        for path in ("/openapi.json", "/docs", "/redoc"):
            assert (await client.get(path)).status_code == 404, path
    finally:
        await client.aclose()
        store.close()


async def test_unknown_routes_and_methods_get_a_typed_refusal_without_values():
    app, _, store = _auth_first_app()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)
    try:
        missing = await client.get("/v1/nope")
        assert missing.status_code == 404
        assert (missing.json()["category"], missing.json()["code"]) == ("not_found", "NF_001")
        wrong = await client.get("/v1/search")
        assert wrong.status_code == 405
        assert (wrong.json()["category"], wrong.json()["code"]) == ("validation", "VAL_002")
        assert "detail" not in missing.json() and "detail" not in wrong.json()
    finally:
        await client.aclose()
        store.close()


async def test_slash_variants_of_routes_are_not_redirected_but_refused_as_unroutable():
    app, _, store = _auth_first_app()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)
    try:
        # 인증 없이 slash 변형을 불러도 정식 경로가 있다는 사실이 리다이렉트로 드러나면 안 된다
        for method, path in (
            ("POST", "/v1/search/"),
            ("POST", "/v1/append/"),
            ("GET", "/v1/health/"),
        ):
            response = await client.request(method, path, json={})
            assert response.status_code == 404, (path, response.status_code)
            assert "location" not in response.headers, path
            body = response.json()
            assert (body["category"], body["code"]) == ("not_found", "NF_001"), path
    finally:
        await client.aclose()
        store.close()
