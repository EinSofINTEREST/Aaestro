"""Regression tests for the Memory Service follow-ups of #447.

#324 리뷰가 범위 밖으로 보고한 다섯 항목 (번호는 이슈 본문의 순서) 과, 그 수정의 리뷰에서 나온
항목의 회귀 테스트다. 각 테스트는 고쳐진 동작을 단언한다 — 수정 전에는 실패했다.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from prometheus_client import CollectorRegistry

from aaestro.access.client import DecisionSource, Verdict
from aaestro.catalog import Catalog
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.memory.client import HttpMemoryAccess
from aaestro.memory.entry import MemoryEntry, MemorySource
from aaestro.memory.gate import RegistryGate
from aaestro.memory.http import TokenRegistry, create_app
from aaestro.memory.index import IndexRegistry
from aaestro.memory.recall import Recall
from aaestro.memory.service import AccessToken, MemoryService, build_token
from aaestro.memory.store import SqliteMemoryStore
from aaestro.modules.memoryset import ChunkSpec, MemoryKind, RecallSpec
from aaestro.observability.metrics import Metrics
from aaestro.runtime.memory import ServiceMemoryAccess

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_URL = "http://memory.test"
ORG = "global:global:org"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def fact(
    content: str,
    *,
    agent: str = "researcher",
    space: str = "placeholder",
    supersedes: str | None = None,
    day: int = 0,
) -> MemoryEntry:
    return MemoryEntry(
        space=space,
        kind=MemoryKind.FACT,
        content=content,
        source=MemorySource(agent=agent),
        created_at=T0 + timedelta(days=day),
        supersedes=supersedes,
    )


def org_token(agent: str, *, writers: tuple[str, ...] = ()) -> AccessToken:
    return build_token(
        agent=agent, group=None, local=[("longterm", agent)], global_spaces=[("org", writers)]
    )


LIBRARIAN = org_token("librarian", writers=("librarian",))
VICTIM = org_token("victim")
ATTACKER = org_token("attacker")
RESEARCHER = build_token(agent="researcher", group=None, local=[("longterm", "researcher")])


@pytest.fixture
def store():
    opened = SqliteMemoryStore()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def service(store):
    return MemoryService(store=store)


def legacy_foreign_correction(store, target: str, *, day: int) -> MemoryEntry:
    """#324 이전에 저장될 수 있었던 교차 space 정정 — 지금의 append 는 거절하므로 저장소에 직접."""
    return store.append(
        fact(
            "hide it",
            agent="attacker",
            space="local:attacker:longterm",
            supersedes=target,
            day=day,
        )
    )


# --- 1. /v1/latest 의 정정 체인은 space 를 넘지 않는다 ------------------------------------


def test_latest_ignores_a_correction_stored_in_another_space(service, store):
    """형제인 검색 경로 (`superseded(space, ids)`) 는 이 정정을 무시한다 — latest 도 같다."""
    policy = service.append(LIBRARIAN, "org", fact("policy v1", agent="librarian"))
    legacy_foreign_correction(store, policy.entry_id, day=1)
    assert store.superseded(ORG, [policy.entry_id]) == set(), "검색 경로는 넘지 않는다"

    latest = service.latest(VICTIM, "org", policy.entry_id)

    assert latest is not None, "다른 space 의 정정이 이 space 의 기억을 가렸다"
    assert latest.entry_id == policy.entry_id
    assert service.audit_log[-1]["status"] == "ok"


def test_latest_prefers_the_same_space_correction_over_a_newer_foreign_one(service, store):
    v1 = service.append(LIBRARIAN, "org", fact("policy v1", agent="librarian"))
    service.append(
        LIBRARIAN, "org", fact("policy v2", agent="librarian", supersedes=v1.entry_id, day=1)
    )
    legacy_foreign_correction(store, v1.entry_id, day=2)

    latest = service.latest(VICTIM, "org", v1.entry_id)

    assert latest is not None
    assert latest.content == "policy v2"


def test_latest_ignores_a_foreign_correction_on_the_tail_of_the_chain(service, store):
    v1 = service.append(LIBRARIAN, "org", fact("policy v1", agent="librarian"))
    v2 = service.append(
        LIBRARIAN, "org", fact("policy v2", agent="librarian", supersedes=v1.entry_id, day=1)
    )
    legacy_foreign_correction(store, v2.entry_id, day=2)

    latest = service.latest(VICTIM, "org", v1.entry_id)

    assert latest is not None
    assert latest.content == "policy v2"


def test_latest_does_not_start_a_chain_from_another_space(service, store):
    """남의 space 의 id 에서 시작해 내 space 로 넘어오면 없는 id 와 답이 달라 존재 여부가 샌다."""
    writer = build_token(agent="writer", group=None, local=[("longterm", "writer")])
    secret = service.append(writer, "longterm", fact("secret", agent="writer"))
    legacy_foreign_correction(store, secret.entry_id, day=1)

    assert service.latest(ATTACKER, "longterm", "no-such-entry") is None
    assert service.latest(ATTACKER, "longterm", secret.entry_id) is None


async def test_latest_over_http_ignores_a_correction_stored_in_another_space(service, store):
    tokens = TokenRegistry()
    app = create_app(service, Recall(indexes={}, store=store), tokens)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)
    victim = HttpMemoryAccess(base_url=BASE_URL, token=tokens.issue(VICTIM), client=http)
    policy = service.append(LIBRARIAN, "org", fact("policy v1", agent="librarian"))
    legacy_foreign_correction(store, policy.entry_id, day=1)

    latest = await victim.latest("org", policy.entry_id)

    assert latest is not None
    assert latest.entry_id == policy.entry_id


# --- 2. 읽기 계열의 저장소 예외는 AaestroError 로 나온다 ----------------------------------


@pytest.fixture
def broken(tmp_path):
    """항목 하나를 저장·색인한 뒤 저장소가 망가진 서비스 — 테이블이 사라진 DB."""
    path = tmp_path / "memory.db"
    opened = SqliteMemoryStore(path=str(path))
    service = MemoryService(store=opened)
    stored = service.append(RESEARCHER, "longterm", fact("qq fact"))
    indexer = IndexRegistry()
    indexer.submit(stored, ChunkSpec())
    indexer.drain()
    outside = sqlite3.connect(path)
    outside.execute("DROP TABLE memory_entries")
    outside.commit()
    outside.close()
    try:
        yield service, Recall(indexes=indexer.indexes, store=opened), stored
    finally:
        opened.close()


def _read(service, _recall, _stored):
    return service.read(RESEARCHER, "longterm")


def _latest(service, _recall, stored):
    return service.latest(RESEARCHER, "longterm", stored.entry_id)


def _search(_service, recall, stored):
    return recall.search("qq fact", spaces=[stored.space])


def _append_correction(service, _recall, stored):
    return service.append(RESEARCHER, "longterm", fact("v2", supersedes=stored.entry_id))


@pytest.mark.parametrize("call", [_read, _latest, _search, _append_correction])
def test_storage_failure_on_a_read_path_is_a_structured_error(broken, call):
    """05 MUST boundary — `memory/` 의 결과는 `MEMORY` / `STORAGE` 다 (append 는 이미 그렇다)."""
    service, recall, stored = broken

    with pytest.raises(AaestroError) as exc_info:
        call(service, recall, stored)

    assert exc_info.value.category in (ErrorCategory.MEMORY, ErrorCategory.STORAGE)
    assert exc_info.value.code == ErrorCode.MEM_004
    assert isinstance(exc_info.value.__cause__, sqlite3.Error), "원인 체인을 보존한다"


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/v1/read", {"space": "longterm"}),
        ("/v1/latest", {"space": "longterm", "entry_id": "any"}),
        ("/v1/search", {"query": "qq fact"}),
    ],
)
async def test_storage_failure_on_a_read_route_answers_with_a_structured_payload(
    broken, path, payload
):
    service, recall, _stored = broken
    tokens = TokenRegistry()
    app = create_app(service, recall, tokens)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        response = await http.post(
            path,
            json=payload,
            headers={"authorization": f"Bearer {tokens.issue(RESEARCHER)}"},
        )

    assert response.status_code >= 500
    assert response.headers["content-type"].startswith("application/json"), response.text
    assert response.json()["category"] in ("memory", "storage")
    assert response.json()["code"] == "MEM_004"


# --- 3. 접근 감사 로그는 decision_source 를 싣는다 ----------------------------------------


def test_every_audit_record_carries_decision_source(service):
    """09 Access Enforcement 6 — 감사 줄의 필수 필드에 `decision_source` 가 있다."""
    stored = service.append(RESEARCHER, "longterm", fact("one"))
    service.read(RESEARCHER, "longterm")
    service.latest(RESEARCHER, "longterm", stored.entry_id)
    service.record_search(RESEARCHER, ["longterm"])
    with pytest.raises(AaestroError):
        service.read(RESEARCHER, "undeclared")

    missing = [row["op"] for row in service.audit_log if not row.get("decision_source")]

    assert missing == []
    assert {row["decision_source"] for row in service.audit_log} == {"token"}


class CachedVerdicts:
    """레지스트리가 캐시에서 허용으로 답한 판정 클라이언트."""

    async def identify(self, credential: str) -> tuple[str | None, DecisionSource]:
        return "researcher", DecisionSource.CACHE

    async def decide(self, credential: str, kind, target: str, mode=None) -> Verdict:
        return Verdict("researcher", True, "declaration", DecisionSource.CACHE)


async def test_audit_record_reports_where_the_registry_decision_came_from(service, store):
    """레지스트리 모드에서 판정 출처가 감사 줄까지 닿는다 — 지금은 입구에서 버려진다."""
    gate = RegistryGate(client=CachedVerdicts(), catalog=Catalog.under(REPO_ROOT))  # type: ignore[arg-type]
    app = create_app(service, Recall(indexes={}, store=store), None, gate=gate)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)
    researcher = HttpMemoryAccess(base_url=BASE_URL, token="identity", client=http)

    await researcher.read("longterm")

    record = service.audit_log[-1]
    assert (record["op"], record["status"]) == ("read", "ok")
    assert record.get("decision_source") == "cache"


# --- 4. aaestro_memory_operations_total 의 space 라벨은 한 어휘다 --------------------------


def operation_spaces(metrics: Metrics) -> dict[str, set[str]]:
    """op → 그 op 가 쓴 `space` 라벨 값들."""
    seen: dict[str, set[str]] = {}
    for family in metrics.registry.collect():
        for sample in family.samples:
            if sample.name == "aaestro_memory_operations_total":
                seen.setdefault(sample.labels["op"], set()).add(sample.labels["space"])
    return seen


async def test_operations_counter_names_one_space_with_one_label(store):
    """같은 space 를 append · read · latest · search 가 다른 이름으로 부르면 집계가 갈린다."""
    metrics = Metrics(registry=CollectorRegistry())
    service = MemoryService(store=store, metrics=metrics)
    indexer = IndexRegistry()
    recall = Recall(indexes=indexer.indexes, store=store, metrics=metrics)
    access = ServiceMemoryAccess(service=service, token=RESEARCHER, recall=recall)

    stored = await access.append("longterm", entry=fact("qq fact"))
    indexer.submit(stored, ChunkSpec())
    indexer.drain()
    await access.read("longterm")
    await access.latest("longterm", stored.entry_id)
    await access.search("qq fact")

    seen = operation_spaces(metrics)
    assert set(seen) == {"append", "read", "latest", "search"}
    assert len(set().union(*seen.values())) == 1, seen


def test_operations_counter_keeps_two_spaces_with_the_same_alias_apart(store):
    """별칭은 에이전트 관점의 이름이다 — 서로 다른 space 둘이 한 시계열로 합쳐진다."""
    metrics = Metrics(registry=CollectorRegistry())
    service = MemoryService(store=store, metrics=metrics)
    writer = build_token(agent="writer", group=None, local=[("longterm", "writer")])

    service.append(RESEARCHER, "longterm", fact("mine"))
    service.append(writer, "longterm", fact("theirs", agent="writer"))

    assert len(operation_spaces(metrics)["append"]) == 2


# --- 5. 클라이언트는 서버의 4xx 를 MEM_004 로 접지 않는다 ----------------------------------


@pytest.fixture
def attacker_over_http(service, store):
    tokens = TokenRegistry()
    app = create_app(service, Recall(indexes={}, store=store), tokens)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)
    return HttpMemoryAccess(base_url=BASE_URL, token=tokens.issue(ATTACKER), client=http)


async def test_client_keeps_the_validation_code_the_service_answered(attacker_over_http, service):
    """서버는 `VAL_002` (400) 로 답한다 — 클라이언트가 `MEM_004` (검색 실패) 로 바꾼다."""
    policy = service.append(LIBRARIAN, "org", fact("policy v1", agent="librarian"))

    with pytest.raises(AaestroError) as exc_info:
        await attacker_over_http.append(
            "longterm", entry=fact("hide it", agent="attacker", supersedes=policy.entry_id)
        )

    assert exc_info.value.code == ErrorCode.VAL_002
    assert exc_info.value.category is ErrorCategory.VALIDATION
    assert not exc_info.value.retryable


async def test_client_reports_a_rejected_request_body_as_validation(attacker_over_http):
    """요청 형식 거절 (422 — `k` 상한 초과) 도 장애가 아니라 호출자의 잘못이다."""
    with pytest.raises(AaestroError) as exc_info:
        await attacker_over_http.search("q", k=500)

    assert exc_info.value.category is ErrorCategory.VALIDATION
    assert exc_info.value.code != ErrorCode.MEM_004
    assert not exc_info.value.retryable


# --- 추가: 회귀 방지와 배선 ---------------------------------------------------------------


def test_latest_still_follows_a_same_space_chain_to_its_end(service):
    v1 = service.append(LIBRARIAN, "org", fact("policy v1", agent="librarian"))
    v2 = service.append(
        LIBRARIAN, "org", fact("policy v2", agent="librarian", supersedes=v1.entry_id, day=1)
    )
    v3 = service.append(
        LIBRARIAN, "org", fact("policy v3", agent="librarian", supersedes=v2.entry_id, day=2)
    )

    for start in (v1, v2, v3):
        latest = service.latest(VICTIM, "org", start.entry_id)
        assert latest is not None
        assert latest.entry_id == v3.entry_id


def test_unknown_and_foreign_ids_get_the_same_latest_answer_and_differ_only_in_the_audit(
    service, store
):
    writer = build_token(agent="writer", group=None, local=[("longterm", "writer")])
    secret = service.append(writer, "longterm", fact("secret", agent="writer"))

    assert service.latest(ATTACKER, "longterm", "no-such-entry") is None
    unknown = service.audit_log[-1]["status"]
    assert service.latest(ATTACKER, "longterm", secret.entry_id) is None
    foreign = service.audit_log[-1]["status"]

    assert (unknown, foreign) == ("ok", "denied")


@pytest.mark.parametrize(
    ("call", "op"),
    [(_read, "read"), (_latest, "latest"), (_append_correction, "append")],
)
def test_storage_failure_is_audited_as_failed(broken, call, op):
    """실패한 접근이 감사에서 빠지면 장애 중에 누가 무엇을 하려 했는지 남지 않는다."""
    service, recall, stored = broken
    before = len(service.audit_log)

    with pytest.raises(AaestroError):
        call(service, recall, stored)

    written = service.audit_log[before:]
    assert [(row["op"], row["status"]) for row in written] == [(op, "failed")]
    assert written[0]["memory_space"] == "longterm"


def test_write_failure_is_audited_as_failed_and_keeps_mem_002(tmp_path):
    """쓰기 자체의 실패 — 정정 대상 조회를 거치지 않는 append."""
    path = tmp_path / "memory.db"
    opened = SqliteMemoryStore(path=str(path))
    service = MemoryService(store=opened, metrics=Metrics(registry=CollectorRegistry()))
    outside = sqlite3.connect(path)
    outside.execute("DROP TABLE memory_entries")
    outside.commit()
    outside.close()
    try:
        with pytest.raises(AaestroError) as exc_info:
            service.append(RESEARCHER, "longterm", fact("one"))
    finally:
        opened.close()

    assert exc_info.value.code == ErrorCode.MEM_002
    assert isinstance(exc_info.value.__cause__, sqlite3.Error)
    assert [(row["op"], row["status"]) for row in service.audit_log] == [("append", "failed")]
    assert operation_statuses(service.metrics) == {("append", "failed")}


def operation_statuses(metrics: Metrics) -> set[tuple[str, str]]:
    return {
        (sample.labels["op"], sample.labels["status"])
        for family in metrics.registry.collect()
        for sample in family.samples
        if sample.name == "aaestro_memory_operations_total"
    }


async def test_failed_search_is_audited_and_counted_as_failed(broken):
    service, recall, stored = broken
    metrics = Metrics(registry=CollectorRegistry())
    recall.metrics = metrics
    access = ServiceMemoryAccess(service=service, token=RESEARCHER, recall=recall)
    before = len(service.audit_log)

    with pytest.raises(AaestroError):
        await access.search("qq fact")

    written = service.audit_log[before:]
    assert [(row["op"], row["status"]) for row in written] == [("search", "failed")]
    assert operation_statuses(metrics) == {("search", "failed")}
    assert operation_spaces(metrics) == {"search": {stored.space}}


def test_undeclared_aliases_share_one_space_label(store):
    """호출자가 고른 문자열이 라벨이 되면 시계열 수를 호출자가 정한다."""
    metrics = Metrics(registry=CollectorRegistry())
    service = MemoryService(store=store, metrics=metrics)

    for alias in ("secret", "another", "local:writer:longterm", "x" * 64):
        with pytest.raises(AaestroError):
            service.read(RESEARCHER, alias)

    assert operation_spaces(metrics) == {"read": {"undeclared"}}
    assert [row["memory_space"] for row in service.audit_log][:2] == ["secret", "another"]


class Verdicts:
    """레지스트리 판정 대역 — 신원은 fresh, space 판정은 스크립트대로."""

    def __init__(self, *, allowed: bool, source: DecisionSource) -> None:
        self.allowed = allowed
        self.source = source

    async def identify(self, credential: str) -> tuple[str | None, DecisionSource]:
        return "researcher", DecisionSource.FRESH

    async def decide(self, credential: str, kind, target: str, mode=None) -> Verdict:
        return Verdict("researcher", self.allowed, "operator", self.source)


def registry_client(service, store, verdicts) -> HttpMemoryAccess:
    gate = RegistryGate(client=verdicts, catalog=Catalog.under(REPO_ROOT))  # type: ignore[arg-type]
    app = create_app(service, Recall(indexes={}, store=store), None, gate=gate)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)
    return HttpMemoryAccess(base_url=BASE_URL, token="identity", client=http)


async def test_a_registry_refusal_is_audited_with_the_source_of_that_verdict(service, store):
    """거부된 space 는 토큰에서 빠진다 — 그래도 감사 줄은 그 판정의 출처를 잃지 않는다."""
    researcher = registry_client(
        service, store, Verdicts(allowed=False, source=DecisionSource.UNREACHABLE)
    )

    with pytest.raises(AaestroError) as exc_info:
        await researcher.read("longterm")

    assert exc_info.value.code == ErrorCode.MEM_001
    record = service.audit_log[-1]
    assert (record["op"], record["status"]) == ("read", "denied")
    assert record["decision_source"] == "unreachable"


async def test_an_undeclared_alias_in_registry_mode_carries_the_identity_source(service, store):
    """space 판정을 묻지 않은 거부 — 출처는 신원 확인의 것이고 `token` 이 아니다."""
    researcher = registry_client(
        service, store, Verdicts(allowed=True, source=DecisionSource.CACHE)
    )

    with pytest.raises(AaestroError):
        await researcher.read("nothing-declared")

    assert service.audit_log[-1]["decision_source"] == "fresh"


async def test_search_and_recall_records_carry_decision_source(store):
    service = MemoryService(store=store)
    access = ServiceMemoryAccess(
        service=service, token=RESEARCHER, recall=Recall(indexes={}, store=store)
    )

    await access.search("anything")
    await access.recall_for_task("anything", policy=RecallSpec())

    assert [(row["op"], row["decision_source"]) for row in service.audit_log] == [
        ("search", "token"),
        ("recall", "token"),
    ]


# 클라이언트 — 서버의 답을 그대로 옮긴다


def answering(status: int, body) -> HttpMemoryAccess:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url=BASE_URL)
    return HttpMemoryAccess(base_url=BASE_URL, token="t", client=http)


async def test_client_keeps_a_non_retryable_server_failure_non_retryable():
    """5xx 라고 다시 걸면 고쳐지는 것은 아니다 — 서버가 정한 retryable 을 따른다."""
    payload = AaestroError(
        category=ErrorCategory.MEMORY,
        code=ErrorCode.MEM_002,
        message="failed to store memory entry",
        agent="researcher",
        details={"memory_space": "local:researcher:longterm"},
    ).payload()
    access = answering(500, payload.model_dump(mode="json"))

    with pytest.raises(AaestroError) as exc_info:
        await access.read("longterm")

    restored = exc_info.value
    assert (restored.category, restored.code) == (ErrorCategory.MEMORY, ErrorCode.MEM_002)
    assert not restored.retryable
    assert restored.details == {"memory_space": "local:researcher:longterm"}


@pytest.mark.parametrize(
    ("status", "code", "category", "retryable"),
    [
        (401, ErrorCode.MEM_001, ErrorCategory.MEMORY, False),
        (403, ErrorCode.MEM_001, ErrorCategory.MEMORY, False),
        (400, ErrorCode.VAL_002, ErrorCategory.VALIDATION, False),
        (422, ErrorCode.VAL_002, ErrorCategory.VALIDATION, False),
        (404, ErrorCode.MEM_004, ErrorCategory.MEMORY, False),
        (405, ErrorCode.MEM_004, ErrorCategory.MEMORY, False),
        (408, ErrorCode.MEM_004, ErrorCategory.MEMORY, True),
        (429, ErrorCode.MEM_004, ErrorCategory.MEMORY, True),
        (500, ErrorCode.MEM_004, ErrorCategory.MEMORY, True),
        (503, ErrorCode.MEM_004, ErrorCategory.MEMORY, True),
    ],
)
async def test_client_classifies_a_response_without_a_payload_by_status(
    status, code, category, retryable
):
    access = answering(status, {"message": "qqproxy page"})

    with pytest.raises(AaestroError) as exc_info:
        await access.read("longterm")

    err = exc_info.value
    assert (err.code, err.category, err.retryable) == (code, category, retryable)
    assert "qqproxy" not in str(err) + str(err.details), "payload 가 아닌 본문의 글은 옮기지 않는다"


async def test_client_does_not_echo_the_rejected_input_of_a_422(attacker_over_http):
    """FastAPI 의 기본 422 본문은 거절된 값을 `input` 과 `msg` 에 되돌려 준다."""
    with pytest.raises(AaestroError) as exc_info:
        await attacker_over_http.search("qqsecret query", k=98765)

    err = exc_info.value
    carried = str(err) + str(err.details)
    assert "98765" not in carried
    assert "qqsecret" not in carried
    assert "200" not in carried.replace("'status': 422", ""), "상한을 인용한 msg 도 옮기지 않는다"
    assert err.details["problems"] == [{"loc": "body.k", "type": "less_than_equal"}]


# --- 리뷰 후속: 클라이언트는 payload 를 검증한 뒤에 복원한다 --------------------------------


def forged(**overrides) -> dict:
    """모양은 에러 payload 인 본문 — 네트워크 너머의 무엇이든 이렇게 답할 수 있다."""
    body = {"category": "memory", "code": "MEM_002", "message": "nope", "retryable": False}
    body.update(overrides)
    return body


async def refusal(access: HttpMemoryAccess) -> AaestroError:
    with pytest.raises(AaestroError) as exc_info:
        await access.read("longterm")
    return exc_info.value


async def test_client_does_not_restore_a_payload_with_an_unknown_code():
    err = await refusal(answering(500, forged(code="QQ_999", message="qqforged text")))

    assert (err.code, err.retryable) == (ErrorCode.MEM_004, True), "상태 코드 분류로 간다"
    assert "qqforged" not in str(err) + str(err.details)


async def test_client_does_not_restore_a_payload_with_an_unknown_category():
    err = await refusal(answering(400, forged(category="qqcategory")))

    assert (err.code, err.category) == (ErrorCode.VAL_002, ErrorCategory.VALIDATION)


async def test_client_bounds_the_message_and_details_it_restores():
    huge = "x" * 100_000
    details = {
        "memory_space": "local:researcher:longterm",
        "blob": huge,
        "nested": {"a": huge},
        "items": [huge],
        huge: "long key",
        "count": 3,
    } | {f"extra-{i}": i for i in range(100)}
    err = await refusal(answering(500, forged(message=huge, details=details)))

    assert err.code == ErrorCode.MEM_002
    assert len(err.message) == 500
    assert len(err.details) == 16
    assert err.details["memory_space"] == "local:researcher:longterm"
    assert err.details["count"] == 3
    assert not {"blob", "nested", "items", huge} & set(err.details)
    assert len(str(err.details)) < 2_000


async def test_client_ignores_the_agent_and_task_the_body_names():
    body = forged(agent="someone-else", task_id="their-task")
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500, json=body)),
        base_url=BASE_URL,
    )
    named = HttpMemoryAccess(base_url=BASE_URL, token="t", client=http, agent="researcher")

    err = await refusal(named)
    anonymous = await refusal(answering(500, body))

    assert (err.agent, err.task_id) == ("researcher", None)
    assert (anonymous.agent, anonymous.task_id) == (None, None)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
async def test_client_never_retries_a_4xx_because_the_body_says_so(status):
    err = await refusal(answering(status, forged(code="MEM_001", retryable=True)))

    assert err.code == ErrorCode.MEM_001
    assert not err.retryable


async def test_client_keeps_retryable_from_a_5xx_payload():
    err = await refusal(answering(503, forged(code="MEM_004", retryable=True)))

    assert (err.code, err.retryable) == (ErrorCode.MEM_004, True)


# --- 리뷰 후속: 손상된 행 --------------------------------------------------------------

LONGTERM = "local:researcher:longterm"


def insert_corrupt_row(store, entry: MemoryEntry) -> None:
    """`kind` 가 enum 밖인 행 — 저장소 API 로는 만들 수 없어 직접 넣는다."""
    row = entry.to_row() | {"kind": "qqbogus-kind"}
    store._conn.execute(  # noqa: SLF001
        "INSERT INTO memory_entries "
        "(entry_id, space, kind, content, tags, agent, run_id, task_id, "
        " node_id, created_at, importance, supersedes) "
        "VALUES (:entry_id, :space, :kind, :content, :tags, :agent, :run_id, "
        " :task_id, :node_id, :created_at, :importance, :supersedes)",
        row,
    )
    store._conn.commit()  # noqa: SLF001


@pytest.fixture
def corrupted(service, store):
    """researcher 의 space 에 손상된 행 하나 — 인덱스에는 멀쩡히 올라 있다."""
    bad = fact("qqsecret fact", space=LONGTERM)
    indexer = IndexRegistry()
    indexer.submit(bad, ChunkSpec())
    indexer.drain()
    insert_corrupt_row(store, bad)
    tokens = TokenRegistry()
    app = create_app(service, Recall(indexes=indexer.indexes, store=store), tokens)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    http = httpx.AsyncClient(transport=transport, base_url=BASE_URL)

    async def post(token: AccessToken, path: str, payload: dict) -> httpx.Response:
        headers = {"authorization": f"Bearer {tokens.issue(token)}"}
        return await http.post(path, json=payload, headers=headers)

    return post, bad


@pytest.mark.parametrize(
    ("path", "op"), [("/v1/read", "read"), ("/v1/latest", "latest"), ("/v1/search", "search")]
)
async def test_a_corrupt_row_answers_with_mem_004_and_is_audited(corrupted, service, path, op):
    post, bad = corrupted
    payload = {
        "/v1/read": {"space": "longterm"},
        "/v1/latest": {"space": "longterm", "entry_id": bad.entry_id},
        "/v1/search": {"query": "qqsecret fact"},
    }[path]

    response = await post(RESEARCHER, path, payload)

    assert response.status_code == 500, response.text
    body = response.json()
    assert (body["code"], body["category"], body["retryable"]) == ("MEM_004", "memory", False)
    assert "qqsecret" not in response.text
    assert "qqbogus" not in response.text
    record = service.audit_log[-1]
    assert (record["op"], record["status"], record["memory_space"]) == (op, "failed", "longterm")


async def test_latest_answers_the_same_for_a_corrupt_foreign_id_and_an_unknown_id(
    corrupted, service
):
    """남의 행을 만들어 보면 그 내용이 답을 가른다 — 500 과 200 의 차이가 id 존재 오라클이다."""
    post, bad = corrupted

    foreign = await post(ATTACKER, "/v1/latest", {"space": "longterm", "entry_id": bad.entry_id})
    unknown = await post(ATTACKER, "/v1/latest", {"space": "longterm", "entry_id": "no-such"})

    assert (foreign.status_code, foreign.text) == (unknown.status_code, unknown.text)
    assert foreign.status_code == 200
    assert foreign.json() is None


async def test_a_correction_gets_the_same_refusal_for_a_corrupt_foreign_target(corrupted):
    post, bad = corrupted

    async def probe(target: str) -> httpx.Response:
        forged_entry = fact("hide it", agent="attacker", supersedes=target)
        body = {"space": "longterm", "entry": forged_entry.model_dump(mode="json")}
        return await post(ATTACKER, "/v1/append", body)

    foreign, unknown = await probe(bad.entry_id), await probe("no-such")

    assert (foreign.status_code, foreign.json()) == (unknown.status_code, unknown.json())
    assert foreign.json()["code"] == "VAL_002"


# --- 리뷰 후속: 검색 · 회상의 거부와 실패도 감사에 남는다 -----------------------------------


async def test_http_search_naming_an_undeclared_alias_is_audited_and_counted(store):
    metrics = Metrics(registry=CollectorRegistry())
    service = MemoryService(store=store, metrics=metrics)
    tokens = TokenRegistry()
    app = create_app(service, Recall(indexes={}, store=store), tokens)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)
    researcher = HttpMemoryAccess(base_url=BASE_URL, token=tokens.issue(RESEARCHER), client=http)

    with pytest.raises(AaestroError) as exc_info:
        await researcher.search("q", spaces=["longterm", "secret", "another"])

    assert exc_info.value.code == ErrorCode.MEM_001
    assert [(r["op"], r["status"], r["memory_space"]) for r in service.audit_log] == [
        ("search", "denied", "secret"),
        ("search", "denied", "another"),
    ]
    assert {r["decision_source"] for r in service.audit_log} == {"token"}
    assert operation_spaces(metrics) == {"search": {"undeclared"}}
    assert operation_statuses(metrics) == {("search", "denied")}


async def test_in_process_search_naming_an_undeclared_alias_is_audited_and_counted(store):
    metrics = Metrics(registry=CollectorRegistry())
    service = MemoryService(store=store, metrics=metrics)
    access = ServiceMemoryAccess(
        service=service, token=RESEARCHER, recall=Recall(indexes={}, store=store)
    )

    with pytest.raises(AaestroError) as exc_info:
        await access.search("q", spaces=["secret"])

    assert exc_info.value.code == ErrorCode.MEM_001
    assert [(r["op"], r["status"], r["memory_space"]) for r in service.audit_log] == [
        ("search", "denied", "secret")
    ]
    assert operation_spaces(metrics) == {"search": {"undeclared"}}


async def test_a_registry_refused_search_is_audited_with_the_source_of_that_verdict(service, store):
    researcher = registry_client(
        service, store, Verdicts(allowed=False, source=DecisionSource.CACHE)
    )

    with pytest.raises(AaestroError) as exc_info:
        await researcher.search("q", spaces=["longterm"])

    assert exc_info.value.code == ErrorCode.MEM_001
    record = service.audit_log[-1]
    assert (record["op"], record["status"]) == ("search", "denied")
    assert record["decision_source"] == "cache"


async def test_a_failed_http_search_is_audited_as_failed(broken):
    service, recall, _stored = broken
    tokens = TokenRegistry()
    app = create_app(service, recall, tokens)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    before = len(service.audit_log)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        response = await http.post(
            "/v1/search",
            json={"query": "qq fact"},
            headers={"authorization": f"Bearer {tokens.issue(RESEARCHER)}"},
        )

    assert response.json()["code"] == "MEM_004"
    written = service.audit_log[before:]
    assert [(r["op"], r["status"], r["memory_space"]) for r in written] == [
        ("search", "failed", "longterm")
    ]


async def test_a_failed_task_recall_is_audited_and_counted_as_failed(broken):
    service, recall, stored = broken
    metrics = Metrics(registry=CollectorRegistry())
    recall.metrics = metrics
    access = ServiceMemoryAccess(service=service, token=RESEARCHER, recall=recall)
    before = len(service.audit_log)

    with pytest.raises(AaestroError):
        await access.recall_for_task("qq fact", policy=RecallSpec())

    written = service.audit_log[before:]
    assert [(r["op"], r["status"], r["memory_space"]) for r in written] == [
        ("recall", "failed", "longterm")
    ]
    # 검색 전에 ok 로 세면 실패한 회상이 성공으로 남는다
    assert operation_statuses(metrics) == {("recall", "failed"), ("search", "failed")}
    assert operation_spaces(metrics)["recall"] == {stored.space}


async def test_a_successful_task_recall_is_counted_ok_once(store):
    metrics = Metrics(registry=CollectorRegistry())
    service = MemoryService(store=store)
    indexer = IndexRegistry()
    stored = service.append(RESEARCHER, "longterm", fact("qq fact"))
    indexer.submit(stored, ChunkSpec())
    indexer.drain()
    recall = Recall(indexes=indexer.indexes, store=store, metrics=metrics)
    access = ServiceMemoryAccess(service=service, token=RESEARCHER, recall=recall)

    await access.recall_for_task("qq fact", policy=RecallSpec())

    assert operation_statuses(metrics) == {("recall", "ok"), ("search", "ok")}


def test_a_search_that_fails_on_a_later_space_counts_every_space_as_failed(broken):
    """한 호출은 한 결과다 — 앞 space 를 ok 로 세면 감사 줄 (전부 failed) 과 어긋난다."""
    _service, recall, stored = broken
    metrics = Metrics(registry=CollectorRegistry())
    recall.metrics = metrics
    empty = "local:researcher:empty"
    from aaestro.memory.index import SpaceIndex

    recall.indexes[empty] = SpaceIndex(space=empty)

    with pytest.raises(AaestroError):
        recall.search("qq fact", spaces=[empty, stored.space])

    assert operation_statuses(metrics) == {("search", "failed")}
    assert operation_spaces(metrics) == {"search": {empty, stored.space}}
