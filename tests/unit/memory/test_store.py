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
