"""Memory store contract tests, shared by every backend.

두 백엔드가 **같은 계약**을 지키는지 한 벌의 테스트로 검증한다. 백엔드별
파일이 이 모듈의 테스트를 가져다 쓰고, ``store`` fixture 만 각자 제공한다 —
계약 테스트가 갈라지면 백엔드 교체가 조용히 계약을 깬다.
"""

from __future__ import annotations

import pytest

from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.memory.entry import MAX_CONTENT_BYTES, MemoryEntry, MemorySource
from aaestro.modules.memoryset import MemoryKind


def make_entry(**overrides) -> MemoryEntry:
    base = {
        "space": "local:researcher:longterm",
        "kind": MemoryKind.FACT,
        "content": "mcp sidecar 는 이미지 태그 고정이 필요하다",
        "source": MemorySource(agent="researcher", run_id="run-1", task_id="task-1"),
    }
    base.update(overrides)
    return MemoryEntry(**base)


# --- 저장 --------------------------------------------------------------------


def test_append_stores_and_returns_the_entry(store):
    entry = make_entry()

    stored = store.append(entry)

    assert store.get(stored.entry_id) == entry


def test_entry_round_trips_through_the_row_form(store):
    """행 표현으로 갔다 와도 tags/source 가 보존되어야 한다."""
    entry = make_entry(tags=("mcp", "sidecar"), importance=0.9)

    store.append(entry)
    loaded = store.get(entry.entry_id)

    assert loaded is not None
    assert loaded.tags == ("mcp", "sidecar")
    assert loaded.source.agent == "researcher"
    assert loaded.importance == 0.9


def test_missing_entry_returns_none(store):
    assert store.get("absent") is None


# --- append-only --------------------------------------------------------------


def test_get_many_returns_the_stored_entries_keyed_by_id(store):
    first = store.append(make_entry(content="첫 번째"))
    second = store.append(make_entry(content="두 번째", space="local:writer:longterm"))
    store.append(make_entry(content="요청하지 않은 항목"))

    found = store.get_many([first.entry_id, second.entry_id])

    assert found == {first.entry_id: first, second.entry_id: second}


def test_get_many_leaves_out_unknown_ids(store):
    """없는 id 는 오류가 아니라 빠진 결과다 — TTL 삭제 뒤 인덱스가 늦는 경우다."""
    kept = store.append(make_entry())

    assert store.get_many([kept.entry_id, "no-such-id"]) == {kept.entry_id: kept}


def test_get_many_of_nothing_is_empty(store):
    assert store.get_many([]) == {}


def test_superseded_reports_the_ids_a_correction_points_at(store):
    old = store.append(make_entry(content="포트는 9000"))
    untouched = store.append(make_entry(content="무관한 항목"))
    store.append(make_entry(content="포트는 9100", supersedes=old.entry_id))

    assert store.superseded(old.space, [old.entry_id, untouched.entry_id]) == {old.entry_id}


def test_superseded_ignores_a_correction_from_another_space(store):
    """정정은 같은 space 의 항목만 대체한다 — 다른 space 의 정정이 기억을 숨기면 안 된다."""
    target = store.append(make_entry(content="org policy approved", space="global:global:org"))
    store.append(
        make_entry(content="hide it", space="local:attacker:longterm", supersedes=target.entry_id)
    )

    assert store.superseded("global:global:org", [target.entry_id]) == set()


def test_superseded_ignores_ids_nobody_corrects(store):
    kept = store.append(make_entry())

    assert store.superseded(kept.space, [kept.entry_id, "no-such-id"]) == set()


def test_superseded_of_nothing_is_empty(store):
    assert store.superseded("local:researcher:longterm", []) == set()


def test_correction_is_a_new_entry(store):
    """정정은 수정이 아니라 supersedes 를 단 새 항목이다."""
    original = make_entry(content="포트는 9000 이다")
    store.append(original)

    corrected = make_entry(content="포트는 9100 이다", supersedes=original.entry_id)
    store.append(corrected)

    assert store.get(original.entry_id).content == "포트는 9000 이다"
    assert store.latest_of_chain(original.entry_id).entry_id == corrected.entry_id


def test_correction_chain_follows_to_the_newest(store):
    first = make_entry(content="v1")
    store.append(first)
    second = make_entry(content="v2", supersedes=first.entry_id)
    store.append(second)
    third = make_entry(content="v3", supersedes=second.entry_id)
    store.append(third)

    assert store.latest_of_chain(first.entry_id).content == "v3"


def test_chain_of_an_unsuperseded_entry_is_itself(store):
    entry = make_entry()
    store.append(entry)

    assert store.latest_of_chain(entry.entry_id) == entry


def test_chain_of_unknown_entry_is_none(store):
    assert store.latest_of_chain("absent") is None


def test_cyclic_correction_does_not_hang(store):
    """순환 정정이 들어와도 무한 루프에 빠지지 않는다.

    저장소 API 만으로 순환을 만든다 — 원시 SQL 을 쓰면 백엔드마다 문법이 달라
    계약 테스트가 한쪽 구현에 묶인다.

    ``latest_of_chain`` 은 supersedes 를 **앞으로** 따라간다 (X 의 후계자 =
    ``supersedes == X`` 인 항목). a→b→a 로 서로를 가리키게 하면 고리가 닫힌다.
    """
    # a 는 b 를 대체한다고, b 는 a 를 대체한다고 주장한다 — 서로가 서로의 후계자다
    store.append(make_entry(entry_id="a", content="a", supersedes="b"))
    store.append(make_entry(entry_id="b", content="b", supersedes="a"))

    result = store.latest_of_chain("a")

    assert result is not None


def test_provenance_is_required(store):
    """출처 없는 기억은 나중에 믿어도 되는지 판단할 근거가 없다."""
    entry = make_entry(source=MemorySource(agent=""))

    with pytest.raises(AaestroError) as exc_info:
        store.append(entry)

    assert exc_info.value.code == "MEM_002"
    assert exc_info.value.category is ErrorCategory.MEMORY


def test_oversized_content_is_rejected(store):
    """대용량 원문은 artifact 로 — 검색 품질과 주입 비용이 함께 망가진다."""
    entry = make_entry(content="x" * (MAX_CONTENT_BYTES + 1))

    with pytest.raises(AaestroError) as exc_info:
        store.append(entry)

    assert exc_info.value.code == "MEM_002"
    assert exc_info.value.details["limit_bytes"] == MAX_CONTENT_BYTES


def test_content_at_the_cap_is_accepted(store):
    entry = make_entry(content="x" * MAX_CONTENT_BYTES)

    assert store.append(entry).entry_id == entry.entry_id


def test_multibyte_content_is_measured_in_bytes(store):
    """한글은 문자 수와 바이트 수가 다르다 — 상한은 바이트 기준이다."""
    entry = make_entry(content="가" * (MAX_CONTENT_BYTES // 3 + 1))

    with pytest.raises(AaestroError):
        store.append(entry)


# --- space 격리 ---------------------------------------------------------------


def test_listing_does_not_cross_space_boundaries(store):
    """인덱스/조회가 space 경계를 넘으면 격리가 무너진다."""
    store.append(make_entry(space="local:researcher:longterm", content="mine"))
    store.append(make_entry(space="local:writer:longterm", content="theirs"))

    entries = store.list_space("local:researcher:longterm")

    assert [e.content for e in entries] == ["mine"]


def test_listing_filters_by_kind(store):
    store.append(make_entry(kind=MemoryKind.FACT, content="f"))
    store.append(make_entry(kind=MemoryKind.OBSERVATION, content="o"))

    entries = store.list_space("local:researcher:longterm", kinds=[MemoryKind.OBSERVATION])

    assert [e.content for e in entries] == ["o"]


def test_listing_respects_the_limit(store):
    for i in range(5):
        store.append(make_entry(content=f"e{i}"))

    assert len(store.list_space("local:researcher:longterm", limit=2)) == 2


# --- retention 전용 삭제 -------------------------------------------------------


def test_purge_removes_entries(store):
    entry = make_entry()
    store.append(entry)

    removed = store.purge([entry.entry_id])

    assert removed == 1
    assert store.get(entry.entry_id) is None


def test_purge_of_nothing_is_a_noop(store):
    assert store.purge([]) == 0


def test_empty_kind_filter_returns_nothing(store):
    """kinds=[] 는 '아무 종류도 원하지 않는다' 지 '필터 없음' 이 아니다.

    빈 목록을 falsy 로 흘려보내면 필터링 결과가 전체 조회로 뒤집힌다.
    """
    store.append(make_entry(kind=MemoryKind.FACT))
    store.append(make_entry(kind=MemoryKind.OBSERVATION))

    assert store.list_space("local:researcher:longterm", kinds=[]) == ()


def test_none_kind_filter_returns_everything(store):
    store.append(make_entry(kind=MemoryKind.FACT))
    store.append(make_entry(kind=MemoryKind.OBSERVATION))

    assert len(store.list_space("local:researcher:longterm", kinds=None)) == 2


# --- warm-up 용 전체 조회 (#312) ----------------------------------------------------


def test_spaces_lists_every_space_that_holds_entries(store):
    """재시작 뒤 인덱스를 다시 채울 대상 — 빠진 space 의 기억은 검색되지 않는다."""
    store.append(make_entry(space="local:researcher:longterm"))
    store.append(make_entry(space="group:research:knowledge"))
    store.append(make_entry(space="group:research:knowledge"))

    assert store.spaces() == ("group:research:knowledge", "local:researcher:longterm")


def test_list_space_without_a_limit_returns_everything(store):
    """기본 상한(100)으로 읽으면 오래된 기억이 warm-up 에서 빠진다."""
    for i in range(105):
        store.append(make_entry(content=f"fact {i}"))

    assert len(store.list_space("local:researcher:longterm")) == 100
    assert len(store.list_space("local:researcher:longterm", limit=None)) == 105
