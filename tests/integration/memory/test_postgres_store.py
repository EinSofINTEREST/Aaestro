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
from aaestro.memory.backend import create_store  # noqa: E402
from aaestro.memory.postgres import APPEND_ONLY_MESSAGE, PostgresMemoryStore  # noqa: E402

# 계약 테스트를 이 모듈로 수집시킨다 — pytest 는 import 된 테스트 함수도 모은다
from tests.fixtures.store_contract import *  # noqa: E402, F403
from tests.fixtures.store_contract import make_entry  # noqa: E402

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
