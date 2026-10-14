"""SQLite memory store — contract plus SQLite-specific guarantees.

계약 테스트는 ``tests/fixtures/store_contract`` 를 그대로 가져다 쓴다 (두
백엔드가 같은 한 벌을 통과해야 한다). 이 파일에는 SQLite 고유의 보증만 남긴다.
"""

from __future__ import annotations

import sqlite3

import pytest

from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.memory.entry import MemorySource
from aaestro.memory.store import SqliteMemoryStore, query_methods

# 계약 테스트를 이 모듈로 수집시킨다 — pytest 는 import 된 테스트 함수도 모은다
from tests.fixtures.store_contract import *  # noqa: F403
from tests.fixtures.store_contract import QUERY_CALLS, contract_methods, make_entry


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


def test_chain_successor_lookup_uses_the_supersedes_index(store):
    """체인을 한 칸 따라갈 때마다 저장소 전체를 훑으면 latest 의 비용이 저장소 크기에 비례한다."""
    from aaestro.memory.store import successor_sql

    plan = store._conn.execute(  # noqa: SLF001
        f"EXPLAIN QUERY PLAN {successor_sql()}", ("a", "local:researcher:longterm")
    ).fetchall()

    detail = " ".join(str(row["detail"]) for row in plan)
    assert "memory_entries_supersedes" in detail, detail
    assert "supersedes=? AND space=?" in detail, detail


# --- 드라이버 예외 변환 (#447) --------------------------------------------------


def test_every_contract_method_is_either_converted_or_explicitly_left_out():
    """계약에 메서드가 늘면 여기서 걸린다 — 변환 대상이거나, 이유가 적힌 예외여야 한다."""
    assert set(contract_methods()) == set(query_methods()) | {"close"}
    assert set(QUERY_CALLS) == set(query_methods())


@pytest.fixture
def dropped(tmp_path):
    """테이블이 사라진 저장소 — 어떤 쿼리든 드라이버가 실패한다."""
    path = tmp_path / "memory.db"
    opened = SqliteMemoryStore(path=str(path))
    outside = sqlite3.connect(path)
    outside.execute("DROP TABLE memory_entries")
    outside.commit()
    outside.close()
    try:
        yield opened
    finally:
        opened.close()


CODE_BY_METHOD = {
    "append": "MEM_002",
    "purge": "MEM_002",
    "get": "MEM_004",
    "space_of": "MEM_004",
    "get_many": "MEM_004",
    "superseded": "MEM_004",
    "list_space": "MEM_004",
    "spaces": "MEM_004",
    "latest_of_chain": "MEM_004",
}
"""메서드 → 실패 코드. 구현의 분류 상수에서 뽑지 않고 **여기 적는다**.

뽑으면 분류가 틀려도 통과한다."""


def test_the_code_table_covers_the_contract():
    assert set(CODE_BY_METHOD) == set(contract_methods()) - {"close"}


@pytest.mark.parametrize("method", sorted(CODE_BY_METHOD))
def test_driver_failure_leaves_every_query_method_as_a_structured_error(dropped, method):
    with pytest.raises(AaestroError) as exc_info:
        QUERY_CALLS[method](dropped)

    err = exc_info.value
    assert err.category is ErrorCategory.MEMORY
    assert err.code == CODE_BY_METHOD[method]
    assert not err.retryable, "없는 테이블은 다시 걸어도 같다"
    assert isinstance(err.__cause__, sqlite3.OperationalError), "같은 타입이어도 영구 실패다"


# --- 일시 장애와 영구 실패 (#447) -------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    ["SELECT * FROM no_such_table", "SELEC 1", "SELECT no_such_column FROM sqlite_master"],
)
def test_permanent_operational_errors_are_not_retryable(statement):
    """SQLite 는 문법 오류 · 없는 테이블도 OperationalError 로 낸다.

    타입이 아니라 결과 코드로 가른다."""
    from aaestro.memory.store import _sqlite_transient

    conn = sqlite3.connect(":memory:")
    with pytest.raises(sqlite3.OperationalError) as exc_info:
        conn.execute(statement)

    assert _sqlite_transient(exc_info.value) is False


@pytest.fixture
def locked(tmp_path):
    """다른 연결이 쓰기 잠금을 쥔 저장소 — 기다리지 않고 바로 `database is locked` 가 난다."""
    path = tmp_path / "memory.db"
    opened = SqliteMemoryStore(path=str(path))
    opened._conn.execute("PRAGMA busy_timeout = 0")  # noqa: SLF001
    holder = sqlite3.connect(path)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        yield opened
    finally:
        holder.rollback()
        holder.close()
        opened.close()


@pytest.mark.parametrize(
    ("method", "code"), [("append", "MEM_002"), ("get", "MEM_004"), ("list_space", "MEM_004")]
)
def test_a_locked_database_is_a_retryable_failure(locked, method, code):
    """잠금 · 연결류는 다시 걸면 풀린다 — 비재시도로 내보내면 호출자가 영구 실패로 읽는다."""
    with pytest.raises(AaestroError) as exc_info:
        QUERY_CALLS[method](locked)

    assert isinstance(exc_info.value.__cause__, sqlite3.OperationalError)
    assert "locked" in str(exc_info.value.__cause__)
    assert (exc_info.value.code, exc_info.value.retryable) == (code, True)


def test_an_integrity_failure_is_not_retryable(store):
    """같은 id 를 다시 넣는 것은 다시 걸어도 같다."""
    entry = store.append(make_entry())

    with pytest.raises(AaestroError) as exc_info:
        store.append(entry)

    assert isinstance(exc_info.value.__cause__, sqlite3.IntegrityError)
    assert (exc_info.value.code, exc_info.value.retryable) == ("MEM_002", False)


# --- 실패한 쿼리가 트랜잭션을 남기지 않는다 (#447) ------------------------------------


def test_a_failed_append_leaves_no_open_transaction(store):
    entry = store.append(make_entry())

    with pytest.raises(AaestroError):
        store.append(entry)

    assert store._conn.in_transaction is False  # noqa: SLF001


def test_a_purge_that_fails_midway_removes_nothing_even_after_a_later_commit(store):
    """앞 묶음의 삭제가 열린 트랜잭션에 남으면, 다음에 성공한 호출의 commit 이 그것을 확정한다."""
    from aaestro.memory.store import SQLITE_BATCH_SIZE

    ids = [store.append(make_entry(content=f"항목 {i}")).entry_id for i in range(600)]
    assert len(ids) > SQLITE_BATCH_SIZE, "둘째 묶음이 있어야 한다"
    store._conn.executescript(  # noqa: SLF001
        "CREATE TRIGGER refuse_one BEFORE DELETE ON memory_entries "
        f"WHEN OLD.entry_id = '{ids[-1]}' "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END;"
    )

    with pytest.raises(AaestroError) as exc_info:
        store.purge(ids)
    store.append(make_entry(content="무관한 항목"))

    assert exc_info.value.code == "MEM_002"
    assert len(store.list_space("local:researcher:longterm", limit=None)) == 601


# --- 손상된 행 (#447) ----------------------------------------------------------


def insert_corrupt_row(store, entry) -> None:
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


SPACE = "local:researcher:longterm"
CORRUPT_READS = {
    "get": lambda store, entry_id: store.get(entry_id),
    "get_many": lambda store, entry_id: store.get_many([entry_id]),
    "list_space": lambda store, entry_id: store.list_space(SPACE),
    "latest_of_chain": lambda store, entry_id: store.latest_of_chain(SPACE, entry_id),
}


@pytest.mark.parametrize("method", sorted(CORRUPT_READS))
def test_a_corrupt_row_leaves_a_read_method_as_mem_004(store, method):
    bad = make_entry(content="qqsecret payload")
    insert_corrupt_row(store, bad)

    with pytest.raises(AaestroError) as exc_info:
        CORRUPT_READS[method](store, bad.entry_id)

    err = exc_info.value
    assert (err.category, err.code, err.retryable) == (ErrorCategory.MEMORY, "MEM_004", False)
    assert isinstance(err.__cause__, ValueError), "원인 체인을 보존한다"
    carried = str(err) + str(err.details)
    assert "qqsecret" not in carried
    assert "qqbogus" not in carried


def test_space_of_answers_for_a_corrupt_row_without_building_it(store):
    bad = make_entry()
    insert_corrupt_row(store, bad)

    assert store.space_of(bad.entry_id) == SPACE


def test_chain_from_another_space_does_not_read_the_foreign_row(store):
    """남의 space 의 손상된 id 와 없는 id 의 답이 같아야 한다 — 다르면 id 존재 여부가 샌다."""
    bad = make_entry(space="local:writer:longterm")
    insert_corrupt_row(store, bad)

    assert store.latest_of_chain(SPACE, bad.entry_id) is None
    assert store.latest_of_chain(SPACE, "no-such-entry") is None


def test_a_store_must_define_the_query_methods_it_wants_converted():
    """상속받은 메서드를 조용히 건너뛰면 그 메서드만 날 드라이버 예외를 낸다."""
    from aaestro.memory.store import DriverErrors, converts_driver_errors

    class Inherited(SqliteMemoryStore):
        pass

    with pytest.raises(TypeError, match="must define MemoryStore.append itself"):
        converts_driver_errors(DriverErrors(base=sqlite3.Error))(Inherited)


def test_driver_failure_does_not_carry_the_stored_content(dropped):
    """실패 사유에 싣는 것은 식별자까지다 — 기억의 내용은 에러와 로그로 새지 않는다."""
    secret = make_entry(content="qqsecret payload")

    with pytest.raises(AaestroError) as exc_info:
        dropped.append(secret)

    assert "qqsecret" not in str(exc_info.value)
    assert "qqsecret" not in str(exc_info.value.details)
    assert exc_info.value.details["entry_id"] == secret.entry_id


def test_structured_errors_from_validation_pass_through_unchanged(store):
    """변환은 드라이버 예외만 잡는다 — 저장 전 검증의 MEM_002 를 다시 감싸지 않는다."""
    with pytest.raises(AaestroError) as exc_info:
        store.append(make_entry(source=MemorySource(agent="")))

    assert exc_info.value.message == "memory entry requires provenance"
    assert exc_info.value.__cause__ is None


# --- 바인드 변수 상한 --------------------------------------------------------


def _entries_with_corrections(store, count):
    """원본 ``count`` 개와 각각을 정정하는 항목 — 정정된 id 와 전체 id 를 돌려준다."""
    originals = [store.append(make_entry(content=f"원본 {i}")).entry_id for i in range(count)]
    for i, target in enumerate(originals):
        store.append(make_entry(content=f"정정 {i}", supersedes=target))
    return originals


def test_get_many_splits_ids_beyond_the_batch_size(store):
    from aaestro.memory.store import SQLITE_BATCH_SIZE

    ids = [
        store.append(make_entry(content=f"항목 {i}")).entry_id for i in range(SQLITE_BATCH_SIZE + 7)
    ]

    found = store.get_many(ids)

    assert set(found) == set(ids)
    assert all(found[entry_id].entry_id == entry_id for entry_id in ids)


def test_superseded_splits_ids_beyond_the_batch_size(store):
    from aaestro.memory.store import SQLITE_BATCH_SIZE

    originals = _entries_with_corrections(store, SQLITE_BATCH_SIZE + 7)

    assert store.superseded("local:researcher:longterm", originals) == set(originals)


def test_purge_splits_ids_beyond_the_batch_size(store):
    from aaestro.memory.store import SQLITE_BATCH_SIZE

    ids = [
        store.append(make_entry(content=f"항목 {i}")).entry_id for i in range(SQLITE_BATCH_SIZE + 7)
    ]

    assert store.purge(ids) == len(ids)
    assert store.get_many(ids) == {}
