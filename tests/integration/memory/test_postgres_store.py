"""PostgreSQL memory store — the same contract, on a real server.

계약 테스트는 SQLite 와 **같은 한 벌**을 쓴다. 백엔드를 바꿔도 계약이
그대로여야 교체가 호출부에 보이지 않는다.
"""

from __future__ import annotations

import pytest

# 드라이버 확인이 먼저다 — aaestro.memory.postgres 가 psycopg 를 import 하므로,
# 아래로 내리면 의존성 부재 시 skip 이 아니라 수집 단계에서 그대로 실패한다
psycopg = pytest.importorskip("psycopg")
postgres_module = pytest.importorskip("testcontainers.postgres")

from aaestro.config import MemoryConfig  # noqa: E402
from aaestro.core.errors import AaestroError, ErrorCategory  # noqa: E402
from aaestro.memory.backend import create_store  # noqa: E402
from aaestro.memory.postgres import APPEND_ONLY_MESSAGE, PostgresMemoryStore  # noqa: E402

# 계약 테스트를 이 모듈로 수집시킨다 — pytest 는 import 된 테스트 함수도 모은다
from tests.fixtures.store_contract import *  # noqa: E402, F403
from tests.fixtures.store_contract import QUERY_CALLS, make_entry  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def postgres_dsn():
    """postgres 컨테이너 — Docker 가 없으면 skip 한다."""
    try:
        container = postgres_module.PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as err:  # noqa: BLE001 — Docker 부재/기동 실패는 skip 사유다
        pytest.skip(f"postgres container unavailable: {type(err).__name__}: {err}")

    try:
        yield container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    finally:
        container.stop()


@pytest.fixture
def store(postgres_dsn):
    """빈 테이블로 시작하는 저장소 — 테스트 간 상태가 새지 않게 한다."""
    store = PostgresMemoryStore(dsn=postgres_dsn)
    with store._conn.cursor() as cursor:
        cursor.execute("TRUNCATE memory_entries")
    try:
        yield store
    finally:
        store.close()


# --- PostgreSQL 고유 ---------------------------------------------------------


def test_update_is_rejected_by_the_store(store):
    """계약을 코드 규율로만 지키면 결국 누군가 UPDATE 를 쓴다."""
    entry = make_entry()
    store.append(entry)

    with (
        pytest.raises(psycopg.errors.RaiseException, match=APPEND_ONLY_MESSAGE),
        store._conn.cursor() as cursor,
    ):
        cursor.execute(
            "UPDATE memory_entries SET content = 'tampered' WHERE entry_id = %s",
            (entry.entry_id,),
        )


def test_purge_still_removes_entries(store):
    """append-only 트리거가 retention 삭제까지 막으면 space 가 무한 성장한다."""
    entry = store.append(make_entry())

    removed = store.purge([entry.entry_id])

    assert removed == 1
    assert store.get(entry.entry_id) is None


# --- 드라이버 예외 변환 (#447) --------------------------------------------------


@pytest.fixture
def dropped(postgres_dsn):
    """테이블 이름이 바뀐 저장소 — 어떤 쿼리든 드라이버가 실패한다. 끝나면 되돌린다."""
    store = PostgresMemoryStore(dsn=postgres_dsn)
    with store._conn.cursor() as cursor:
        cursor.execute("ALTER TABLE memory_entries RENAME TO memory_entries_away")
    try:
        yield store
    finally:
        with store._conn.cursor() as cursor:
            cursor.execute("ALTER TABLE memory_entries_away RENAME TO memory_entries")
        store.close()


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
"""메서드 → 실패 코드 — 구현의 분류 상수에서 뽑지 않고 여기 적는다."""


def test_the_code_table_covers_every_query_call():
    assert set(CODE_BY_METHOD) == set(QUERY_CALLS)


@pytest.mark.parametrize("method", sorted(CODE_BY_METHOD))
def test_driver_failure_leaves_every_query_method_as_a_structured_error(dropped, method):
    with pytest.raises(AaestroError) as exc_info:
        QUERY_CALLS[method](dropped)

    err = exc_info.value
    assert err.category is ErrorCategory.MEMORY
    assert err.code == CODE_BY_METHOD[method]
    assert not err.retryable, "없는 테이블은 다시 걸어도 같다"
    assert isinstance(err.__cause__, psycopg.Error), "원인 체인을 보존한다"


@pytest.mark.parametrize(("method", "code"), [("get", "MEM_004"), ("append", "MEM_002")])
def test_a_terminated_connection_is_a_retryable_failure(postgres_dsn, method, code):
    """연결이 끊긴 저장소를 비재시도로 내보내면 호출자가 영구 실패로 읽는다."""
    store = PostgresMemoryStore(dsn=postgres_dsn)
    try:
        pid = store._conn.info.backend_pid
        with psycopg.connect(postgres_dsn, autocommit=True) as other:
            other.execute("SELECT pg_terminate_backend(%s)", (pid,))

        with pytest.raises(AaestroError) as exc_info:
            QUERY_CALLS[method](store)
        # retryable 은 "다시 하면 된다" 는 약속이다 — 저장소가 끊긴 연결을 다시 열어야 지켜진다
        again = store.append(make_entry(content="after reconnect"))
        assert store.get(again.entry_id) == again
    finally:
        store.close()

    assert isinstance(exc_info.value.__cause__, psycopg.OperationalError)
    assert (exc_info.value.code, exc_info.value.retryable) == (code, True)


def test_a_failed_reconnect_keeps_the_original_failure(postgres_dsn, monkeypatch):
    """다시 열기가 실패해도 호출자가 받는 것은 원래 실패다 — 다음 호출이 다시 시도한다."""
    from aaestro.memory import postgres as postgres_store

    store = PostgresMemoryStore(dsn=postgres_dsn)
    try:
        pid = store._conn.info.backend_pid
        with psycopg.connect(postgres_dsn, autocommit=True) as other:
            other.execute("SELECT pg_terminate_backend(%s)", (pid,))
        real_connect = postgres_store._connect

        def refuse(dsn):
            raise psycopg.OperationalError("qqreconnect refused")

        monkeypatch.setattr(postgres_store, "_connect", refuse)
        with pytest.raises(AaestroError) as exc_info:
            store.get("a")
        assert "qqreconnect" not in str(exc_info.value.__cause__)
        assert exc_info.value.retryable

        monkeypatch.setattr(postgres_store, "_connect", real_connect)
        assert store.get("a") is None, "다음 호출이 다시 열고 성공한다"
    finally:
        store.close()


def test_a_corrupt_row_leaves_a_read_as_mem_004_and_space_of_still_answers(store):
    bad = make_entry(content="qqsecret payload")
    with store._conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO memory_entries "
            "(entry_id, space, kind, content, tags, agent, created_at, importance) "
            "VALUES (%s, %s, 'qqbogus-kind', %s, '', 'researcher', %s, 0.5)",
            (bad.entry_id, bad.space, bad.content, bad.created_at.isoformat()),
        )

    with pytest.raises(AaestroError) as exc_info:
        store.get(bad.entry_id)

    err = exc_info.value
    assert (err.code, err.retryable) == ("MEM_004", False)
    assert isinstance(err.__cause__, ValueError)
    assert "qqsecret" not in str(err) + str(err.details)
    assert store.space_of(bad.entry_id) == bad.space
    assert store.latest_of_chain("local:someone-else:longterm", bad.entry_id) is None


# --- backend 팩토리 ----------------------------------------------------------


def test_config_backend_builds_a_live_postgres_store(postgres_dsn):
    """`backend: postgres` 설정으로 실제 저장소가 서야 한다."""
    config = MemoryConfig(backend="postgres", dsn=postgres_dsn)

    store = create_store(config)
    try:
        entry = store.append(make_entry())
        assert store.get(entry.entry_id) == entry
    finally:
        store.close()
