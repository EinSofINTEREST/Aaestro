"""SQLite memory store — contract plus SQLite-specific guarantees.

계약 테스트는 ``tests/fixtures/store_contract`` 를 그대로 가져다 쓴다 (두
백엔드가 같은 한 벌을 통과해야 한다). 이 파일에는 SQLite 고유의 보증만 남긴다.
"""

from __future__ import annotations

import sqlite3

import pytest

from aaestro.memory.store import SqliteMemoryStore

# 계약 테스트를 이 모듈로 수집시킨다 — pytest 는 import 된 테스트 함수도 모은다
from tests.fixtures.store_contract import *  # noqa: F403
from tests.fixtures.store_contract import make_entry


@pytest.fixture
def store():
    """메모리 내 저장소 — finalizer 가 연결을 닫는다."""
    store = SqliteMemoryStore()
    try:
        yield store
    finally:
        store.close()


# --- SQLite 고유 -------------------------------------------------------------


def test_update_is_rejected_by_the_store(store):
    """계약을 코드 규율로만 지키면 결국 누군가 UPDATE 를 쓴다."""
    entry = make_entry()
    store.append(entry)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute(
            "UPDATE memory_entries SET content = 'tampered' WHERE entry_id = ?",
            (entry.entry_id,),
        )


def test_get_many_issues_one_select_for_many_ids(store):
    """get 를 반복하면 hit 수만큼 왕복한다 — id N 개에 쿼리 1회여야 한다."""
    ids = [store.append(make_entry(content=f"항목 {i}")).entry_id for i in range(8)]
    statements: list[str] = []
    store._conn.set_trace_callback(statements.append)  # noqa: SLF001

    found = store.get_many(ids)

    store._conn.set_trace_callback(None)  # noqa: SLF001
    assert set(found) == set(ids)
    assert len([s for s in statements if s.lstrip().upper().startswith("SELECT")]) == 1


def test_superseded_issues_one_select_for_many_ids(store):
    ids = [store.append(make_entry(content=f"항목 {i}")).entry_id for i in range(8)]
    statements: list[str] = []
    store._conn.set_trace_callback(statements.append)  # noqa: SLF001

    store.superseded("local:researcher:longterm", ids)

    store._conn.set_trace_callback(None)  # noqa: SLF001
    assert len([s for s in statements if s.lstrip().upper().startswith("SELECT")]) == 1


def test_superseded_lookup_uses_an_index(store):
    """색인이 없으면 검색마다 저장소 전체를 훑는다 — 비용이 저장소 크기에 비례한다."""
    from aaestro.memory.store import superseded_sql

    plan = store._conn.execute(  # noqa: SLF001
        f"EXPLAIN QUERY PLAN {superseded_sql(2)}", ("local:researcher:longterm", "a", "b")
    ).fetchall()

    detail = " ".join(str(row["detail"]) for row in plan)
    # space 색인만으로는 space 전체를 훑는다 — supersedes 색인을 타야 한다
    assert "memory_entries_supersedes" in detail, detail
