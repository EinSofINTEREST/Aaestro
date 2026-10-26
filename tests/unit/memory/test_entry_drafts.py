"""Append drafts (``memory/entry.py``) — append 의 id 와 출처는 서비스가 발급한다 (#456).

``entry_id`` 는 space 를 넘는 전역 기본 키다. 호출자가 고르면 다른 space 에 있는 id 로
append 했을 때 (500 ``MEM_002``) 와 없는 id 로 했을 때 (200) 가 갈려 다른 space 의 항목 존재가
드러나고, 남이 쓸 id 를 선점할 수 있다. 출처 (``source.agent`` · ``created_at``) 도 호출자가
고르면 기억이 위조된다 (09 Entry Rules 4). 요청은 내용만 싣는다 — id 를 지정할 방법이
계약에서 사라졌는지를 본다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import httpx
import pytest

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.memory.client import HttpMemoryAccess
from aaestro.memory.entry import (
    MAX_CONTENT_BYTES,
    MAX_IDENTIFIER_CHARS,
    MAX_TAG_CHARS,
    MAX_TAGS,
    MemoryDraft,
    MemoryEntry,
    MemorySource,
    draft_from,
)
from aaestro.memory.http import TokenRegistry, create_app
from aaestro.memory.index import IndexRegistry
from aaestro.memory.recall import Recall
from aaestro.memory.service import AccessToken, MemoryService, build_token
from aaestro.memory.store import SqliteMemoryStore
from aaestro.modules.memoryset import MemoryKind
from aaestro.runtime.memory import ServiceMemoryAccess

BASE_URL = "http://memory.test"
NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
WRITER = build_token(agent="writer", group=None, local=[("longterm", "writer")])
RESEARCHER = build_token(agent="researcher", group=None, local=[("longterm", "researcher")])
PLANNER = build_token(agent="planner", group=None, local=[("longterm", "planner")], run_id="run-1")
"""그래프 run 의 태스크 토큰 — run 을 안다 (direct 태스크 토큰인 RESEARCHER 는 모른다)."""
SENTINEL = "qq-caller-chosen-id-456"
"""호출자가 고른 값 — 응답 · 에러 어디에도 나오면 안 된다."""


@pytest.fixture
def stack():
    """writer 와 researcher 가 같은 서비스에 붙은 HTTP 표면 — 시계는 고정."""
    store = SqliteMemoryStore()
    service = MemoryService(store=store, clock=lambda: NOW)
    tokens = TokenRegistry()
    indexer = IndexRegistry()
    app = create_app(service, Recall(indexes=indexer.indexes, store=store), tokens, indexer=indexer)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    http = httpx.AsyncClient(transport=transport, base_url=BASE_URL)
    secrets = {
        "writer": tokens.issue(WRITER),
        "researcher": tokens.issue(RESEARCHER),
        "planner": tokens.issue(PLANNER),
    }

    async def post(agent: str, entry: dict) -> httpx.Response:
        return await http.post(
            "/v1/append",
            json={"space": "longterm", "entry": entry},
            headers={"authorization": f"Bearer {secrets[agent]}"},
        )

    def client(agent: str) -> HttpMemoryAccess:
        return HttpMemoryAccess(base_url=BASE_URL, token=secrets[agent], client=http, agent=agent)

    try:
        yield post, client, service
    finally:
        store.close()


def body(**extra: object) -> dict:
    return {"kind": "fact", "content": "sidecar 이미지는 태그를 고정한다", **extra}


# --- (a) 호출자는 id 를 지정할 수 없다 --------------------------------------------------


async def test_append_with_another_spaces_id_answers_like_a_fresh_id(stack):
    """재현 (#456): 남의 space 의 id 는 500, 없는 id 는 200 이었다 — 이제 둘 다 같은 거절이다."""
    post, _client, _service = stack
    victim = (await post("writer", body())).json()["entry_id"]

    foreign = await post("researcher", body(entry_id=victim))
    fresh = await post("researcher", body(entry_id="no-such-entry"))

    assert (foreign.status_code, foreign.json()) == (fresh.status_code, fresh.json())
    assert foreign.status_code == 400
    assert foreign.json()["code"] == ErrorCode.VAL_002
    assert victim not in foreign.text


@pytest.mark.parametrize(
    "field",
    [
        {"entry_id": SENTINEL},
        {"created_at": "2000-01-01T00:00:00+00:00"},
        {"space": SENTINEL},
        {"source": {"agent": SENTINEL}},
    ],
    ids=["entry_id", "created_at", "space", "source.agent"],
)
async def test_append_refuses_what_the_service_issues_without_echoing_it(stack, field):
    """옛 클라이언트가 보내는 완성된 항목의 필드는 조용히 버리지 않고 거절한다 (모르는 키)."""
    post, _client, service = stack

    response = await post("researcher", body(**field))

    assert response.status_code == 400
    assert response.json()["code"] == ErrorCode.VAL_002
    assert SENTINEL not in response.text
    assert "2000-01-01" not in response.text
    assert service.store.list_space("local:researcher:longterm") == ()


@pytest.mark.parametrize(
    "extra",
    [{"entry_id": SENTINEL}, {"created_at": NOW}, {"space": SENTINEL}, {"entry": SENTINEL}],
    ids=["entry_id", "created_at", "space", "entry"],
)
def test_draft_from_refuses_unknown_fields_without_their_values(extra):
    with pytest.raises(AaestroError) as exc_info:
        draft_from({"kind": MemoryKind.FACT, "content": "c", **extra}, agent="researcher")

    err = exc_info.value
    assert (err.category, err.code, err.agent) == (
        ErrorCategory.VALIDATION,
        ErrorCode.VAL_002,
        "researcher",
    )
    assert SENTINEL not in str(err.details)
    assert err.__cause__ is None
    assert err.__context__ is None


# --- (b) id 는 서비스가 만든다 ------------------------------------------------------


async def test_each_append_gets_a_new_service_issued_id(stack):
    post, _client, _service = stack

    first = (await post("researcher", body())).json()
    second = (await post("researcher", body())).json()

    assert first["entry_id"] != second["entry_id"]
    assert UUID(first["entry_id"]).version == 4
    assert UUID(second["entry_id"]).version == 4


async def test_created_at_comes_from_the_service_clock(stack):
    post, _client, _service = stack

    stored = (await post("researcher", body())).json()

    assert datetime.fromisoformat(stored["created_at"]) == NOW


# --- (c) 출처의 신원은 토큰의 에이전트다 ---------------------------------------------


async def test_source_agent_is_the_token_holder(stack):
    post, _client, service = stack

    stored = (await post("researcher", body(source={"task_id": "task-1"}))).json()

    assert stored["source"]["agent"] == "researcher"
    assert stored["source"]["task_id"] == "task-1"
    [kept] = service.store.list_space("local:researcher:longterm")
    assert kept.source.agent == "researcher"


async def test_a_client_cannot_name_another_agent_as_the_source(stack):
    _post, client, service = stack

    with pytest.raises(AaestroError) as exc_info:
        await client("researcher").append(
            "longterm", kind=MemoryKind.FACT, content="c", source={"agent": "librarian"}
        )

    assert exc_info.value.code == ErrorCode.VAL_002
    assert "librarian" not in str(exc_info.value.details)
    assert service.store.list_space("local:researcher:longterm") == ()


# --- (d) 호출자 문맥의 식별자는 형식으로 거른다 ------------------------------------------


MALFORMED_IDENTIFIERS = [
    "",
    "x" * (MAX_IDENTIFIER_CHARS + 1),
    "run\nforged",
    "run\x1b[31m",
    " run-1",
    "run-1 ",
    5,
]
MALFORMED_IDS = ["empty", "too-long", "newline", "control", "leading", "trailing", "not-string"]


@pytest.mark.parametrize("field", ["run_id", "task_id", "node_id"])
@pytest.mark.parametrize("value", MALFORMED_IDENTIFIERS, ids=MALFORMED_IDS)
async def test_malformed_source_identifiers_are_val_002(stack, field, value):
    post, _client, service = stack

    response = await post("researcher", body(source={field: value}))

    assert response.status_code == 400, response.text
    assert response.json()["code"] == ErrorCode.VAL_002
    if isinstance(value, str) and value:
        assert value not in response.text
    assert service.store.list_space("local:researcher:longterm") == ()


@pytest.mark.parametrize("value", MALFORMED_IDENTIFIERS, ids=MALFORMED_IDS)
def test_malformed_supersedes_is_val_002(value):
    with pytest.raises(AaestroError) as exc_info:
        draft_from({"kind": MemoryKind.FACT, "content": "c", "supersedes": value})

    assert exc_info.value.code == ErrorCode.VAL_002


@pytest.mark.parametrize("value", ["r", "x" * MAX_IDENTIFIER_CHARS, "run-1:node/2", "실행-1"])
def test_well_formed_identifiers_pass(value):
    draft = draft_from({"kind": MemoryKind.FACT, "content": "c", "source": {"run_id": value}})

    assert draft.source.run_id == value


# --- (e) 정정은 서비스가 돌려준 id 로 한다 --------------------------------------------


async def test_a_correction_uses_the_id_the_service_returned(stack):
    _post, client, _service = stack
    researcher = client("researcher")
    original = await researcher.append("longterm", kind=MemoryKind.FACT, content="포트는 8000")

    correction = await researcher.append(
        "longterm", kind=MemoryKind.FACT, content="포트는 8080", supersedes=original.entry_id
    )
    latest = await researcher.latest("longterm", original.entry_id)

    assert correction.supersedes == original.entry_id
    assert latest is not None
    assert latest.entry_id == correction.entry_id


# --- (f) HTTP 클라이언트와 in-process 어댑터는 같은 계약이다 -------------------------------


def adapter(service: MemoryService, token: AccessToken = RESEARCHER) -> ServiceMemoryAccess:
    return ServiceMemoryAccess(
        service=service, token=token, recall=Recall(indexes={}, store=service.store)
    )


ARGS = {
    "kind": MemoryKind.OBSERVATION,
    "content": "mcp 재연결이 세 번 실패했다",
    "tags": ["mcp"],
    "importance": 0.7,
    "source": {"run_id": "run-1", "task_id": "task-1", "node_id": "watcher"},
}
ISSUED = {"entry_id", "created_at"}
"""매번 달라지는 발급값 — 나머지 모양이 같아야 한다."""


async def test_both_clients_store_the_same_entry_for_the_same_arguments(stack):
    _post, client, service = stack
    local = MemoryService(store=SqliteMemoryStore(), clock=lambda: NOW)
    try:
        over_http = await client("planner").append("longterm", **ARGS)
        in_process = await adapter(local, PLANNER).append("longterm", **ARGS)
    finally:
        local.store.close()

    assert over_http.model_dump(exclude=ISSUED) == in_process.model_dump(exclude=ISSUED)
    assert over_http.created_at == in_process.created_at == NOW
    assert service.store.get(over_http.entry_id) == over_http
    # 두 경로를 서로만 비교하면 같은 것을 함께 버려도 초록이다 — 입력과 직접 대조한다
    for stored in (over_http, in_process):
        assert stored.source.model_dump() == {"agent": "planner", **ARGS["source"]}
        assert (stored.kind, stored.content, stored.tags, stored.importance) == (
            ARGS["kind"],
            ARGS["content"],
            tuple(ARGS["tags"]),
            ARGS["importance"],
        )


@pytest.mark.parametrize(
    "bad",
    [{"entry_id": SENTINEL}, {"source": {"run_id": " x"}}, {"kind": "not-a-kind"}],
    ids=["entry_id", "run_id", "kind"],
)
async def test_both_clients_refuse_the_same_arguments_the_same_way(bad):
    """클라이언트는 보내기 전에 거절한다 — 서버에 닿으면 안 된다 (요청이 오면 테스트가 실패한다)."""

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the client sent a request it should have refused")

    http = httpx.AsyncClient(transport=httpx.MockTransport(unreachable))
    over_http = HttpMemoryAccess(base_url=BASE_URL, token="t", client=http, agent="researcher")
    local = MemoryService(store=SqliteMemoryStore())
    args = {"kind": MemoryKind.FACT, "content": "c", **bad}
    try:
        with pytest.raises(AaestroError) as from_http:
            await over_http.append("longterm", **args)
        with pytest.raises(AaestroError) as from_adapter:
            await adapter(local).append("longterm", **args)
    finally:
        local.store.close()
        await http.aclose()

    one, other = from_http.value, from_adapter.value
    assert (one.code, one.category, one.details) == (other.code, other.category, other.details)
    assert one.code == ErrorCode.VAL_002


def test_the_draft_has_no_field_for_what_the_service_issues():
    """계약에서 사라졌는지 — 필드가 있으면 언젠가 누가 그 값을 쓴다."""
    assert set(MemoryDraft.model_fields) == {
        "kind",
        "content",
        "tags",
        "importance",
        "supersedes",
        "source",
    }
    assert set(MemoryDraft.model_fields["source"].annotation.model_fields) == {
        "run_id",
        "task_id",
        "node_id",
    }


# --- run 은 토큰이 정한다 ------------------------------------------------------------


async def test_a_run_task_gets_its_run_without_naming_it(stack):
    post, _client, _service = stack

    stored = (await post("planner", body())).json()

    assert stored["source"]["run_id"] == "run-1"


async def test_a_run_task_may_name_its_own_run(stack):
    post, _client, _service = stack

    stored = (await post("planner", body(source={"run_id": "run-1"}))).json()

    assert stored["source"]["run_id"] == "run-1"


async def test_another_run_and_a_direct_task_claiming_a_run_get_the_same_refusal(stack):
    """다른 run 을 대거나 run 이 없는 토큰이 run 을 대면 출처 위조다 — 값은 싣지 않는다."""
    post, _client, service = stack

    other_run = await post("planner", body(source={"run_id": SENTINEL}))
    no_run = await post("researcher", body(source={"run_id": SENTINEL}))

    for response in (other_run, no_run):
        assert response.status_code == 400
        assert response.json()["code"] == ErrorCode.VAL_002
        assert response.json()["details"]["field"] == "source.run_id"
        assert SENTINEL not in response.text
    assert service.store.list_space("local:planner:longterm") == ()
    assert service.store.list_space("local:researcher:longterm") == ()
    assert [r["status"] for r in service.audit_log if r["op"] == "append"] == ["failed", "failed"]


def test_a_token_derives_its_run_from_its_run_spaces():
    token = build_token(agent="a", group=None, run_spaces=[("scratch", "run-9")])

    assert token.run_id == "run-9"


def test_a_token_refuses_run_spaces_of_another_run():
    with pytest.raises(ValueError, match="run spaces"):
        build_token(agent="a", group=None, run_spaces=[("scratch", "run-9")], run_id="run-1")


def test_a_token_refuses_run_spaces_spanning_several_runs_without_a_run_id():
    with pytest.raises(ValueError, match="run spaces"):
        build_token(agent="a", group=None, run_spaces=[("a", "run-1"), ("b", "run-2")])


def test_a_token_refuses_run_spaces_spanning_several_runs_with_a_run_id():
    with pytest.raises(ValueError, match="run spaces"):
        build_token(
            agent="a", group=None, run_spaces=[("a", "run-1"), ("b", "run-2")], run_id="run-1"
        )


def test_a_token_accepts_several_run_spaces_of_one_run():
    token = build_token(agent="a", group=None, run_spaces=[("a", "run-1"), ("b", "run-1")])

    assert token.run_id == "run-1"
    assert len(token.spaces) == 2


def test_a_token_without_run_spaces_keeps_the_given_run_id_or_none():
    assert build_token(agent="a", group=None, run_id="run-1").run_id == "run-1"
    assert build_token(agent="a", group=None, run_spaces=[]).run_id is None


def test_issue_token_carries_the_run_without_run_spaces():
    from aaestro.core.manifest import AgentManifest
    from aaestro.memory.tokens import issue_token
    from tests.fixtures.builders import manifest_dict

    manifest = AgentManifest.model_validate(manifest_dict())

    assert issue_token(manifest, run_id="run-3").run_id == "run-3"
    assert issue_token(manifest).run_id is None


# --- content 상한 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "accepted"),
    [
        ("x" * MAX_CONTENT_BYTES, True),
        ("x" * (MAX_CONTENT_BYTES + 1), False),
        # 상한은 글자 수가 아니라 UTF-8 바이트다 — 한글 한 글자는 3바이트
        ("가" * (MAX_CONTENT_BYTES // 3), True),
        ("가" * (MAX_CONTENT_BYTES // 3 + 1), False),
    ],
    ids=["ascii-at-cap", "ascii-over", "utf8-at-cap", "utf8-over"],
)
async def test_oversized_content_is_val_002_not_a_storage_failure(stack, content, accepted):
    post, _client, _service = stack

    response = await post("researcher", body(content=content))

    if accepted:
        assert response.status_code == 200, response.text
        return
    assert response.status_code == 400
    assert response.json()["code"] == ErrorCode.VAL_002
    assert content[:64] not in response.text


# --- tags -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tags",
    [
        [""],
        [" mcp"],
        ["mcp "],
        ["a\x1fb"],
        ["line\nforged"],
        ["x" * (MAX_TAG_CHARS + 1)],
        [f"t{i}" for i in range(MAX_TAGS + 1)],
        [5],
    ],
    ids=["empty", "leading", "trailing", "separator", "newline", "too-long", "too-many", "int"],
)
def test_malformed_tags_are_val_002(tags):
    with pytest.raises(AaestroError) as exc_info:
        draft_from({"kind": MemoryKind.FACT, "content": "c", "tags": tags})

    assert exc_info.value.code == ErrorCode.VAL_002


async def test_tags_round_trip_through_the_store(stack):
    """저장소는 태그를 구분자로 이어 붙인다 — 받은 태그는 다시 읽어도 같아야 한다."""
    _post, client, service = stack
    tags = ["mcp", "사이드카", "x" * MAX_TAG_CHARS, *(f"t{i}" for i in range(MAX_TAGS - 3))]

    stored = await client("researcher").append(
        "longterm", kind=MemoryKind.FACT, content="c", tags=tags
    )

    assert stored.tags == tuple(tags)
    assert service.store.get(stored.entry_id).tags == tuple(tags)


# --- importance -----------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0.5", True, -0.01, 1.01, None], ids=str)
def test_importance_is_a_strict_float_in_range(value):
    with pytest.raises(AaestroError):
        draft_from({"kind": MemoryKind.FACT, "content": "c", "importance": value})


@pytest.mark.parametrize("value", [0, 1, 0.0, 1.0, 0.25])
def test_importance_accepts_numbers_in_range(value):
    assert (
        draft_from({"kind": MemoryKind.FACT, "content": "c", "importance": value}).importance
        == value
    )


@pytest.mark.parametrize("value", [-0.01, 1.01])
def test_a_stored_entry_has_the_same_importance_range(value):
    with pytest.raises(ValueError):
        MemoryEntry(
            space="s",
            kind=MemoryKind.FACT,
            content="c",
            source=MemorySource(agent="a"),
            importance=value,
        )


# --- 서비스 시계 ----------------------------------------------------------------------


def test_created_at_never_goes_back_and_latest_follows_the_later_correction():
    """시계가 역행해도 나중에 쓴 정정이 최신이다 — 저장소는 created_at 으로 정렬한다."""
    ticks = iter([NOW, NOW - timedelta(hours=1), NOW - timedelta(hours=2)])
    service = MemoryService(store=SqliteMemoryStore(), clock=lambda: next(ticks))
    try:
        v1 = service.append(RESEARCHER, "longterm", MemoryDraft(kind=MemoryKind.FACT, content="v1"))
        v2 = service.append(
            RESEARCHER,
            "longterm",
            MemoryDraft(kind=MemoryKind.FACT, content="v2", supersedes=v1.entry_id),
        )
        v3 = service.append(
            RESEARCHER,
            "longterm",
            MemoryDraft(kind=MemoryKind.FACT, content="v3", supersedes=v2.entry_id),
        )

        latest = service.latest(RESEARCHER, "longterm", v1.entry_id)
    finally:
        service.store.close()

    assert v1.created_at == v2.created_at == v3.created_at == NOW
    assert latest is not None
    assert latest.entry_id == v3.entry_id


def test_a_non_utc_clock_is_normalised_to_utc():
    seoul = timezone(timedelta(hours=9))
    service = MemoryService(store=SqliteMemoryStore(), clock=lambda: NOW.astimezone(seoul))
    try:
        stored = service.append(
            RESEARCHER, "longterm", MemoryDraft(kind=MemoryKind.FACT, content="c")
        )
    finally:
        service.store.close()

    assert stored.created_at == NOW
    assert stored.created_at.utcoffset() == timedelta(0)


def test_a_naive_clock_is_a_typed_failure_with_an_audit_line():
    service = MemoryService(store=SqliteMemoryStore(), clock=lambda: NOW.replace(tzinfo=None))
    try:
        with pytest.raises(AaestroError) as exc_info:
            service.append(RESEARCHER, "longterm", MemoryDraft(kind=MemoryKind.FACT, content="c"))
        stored = service.store.list_space("local:researcher:longterm")
    finally:
        service.store.close()

    assert exc_info.value.code == ErrorCode.MEM_002
    assert stored == ()
    assert service.audit_log[-1]["status"] == "failed"


# --- in-process 어댑터의 형식 거절은 감사에 남는다 --------------------------------------


async def test_the_adapter_audits_a_malformed_draft_as_failed():
    service = MemoryService(store=SqliteMemoryStore())
    try:
        with pytest.raises(AaestroError):
            await adapter(service).append(
                "longterm", kind=MemoryKind.FACT, content="c", entry_id="x"
            )
    finally:
        service.store.close()

    record = service.audit_log[-1]
    assert (record["op"], record["status"], record["memory_space"], record["agent"]) == (
        "append",
        "failed",
        "longterm",
        "researcher",
    )
