"""Memory store backend selection tests.

설정이 선언한 백엔드가 실제로 만들어지는지 검증한다 — 팩토리가 없으면
`backend: postgres` 는 문서상의 약속으로만 남는다.
"""

from __future__ import annotations

import pytest

from aaestro.config import MemoryConfig
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.memory.backend import create_store
from aaestro.memory.store import SqliteMemoryStore


def test_sqlite_backend_builds_a_sqlite_store():
    store = create_store(MemoryConfig())

    try:
        assert isinstance(store, SqliteMemoryStore)
    finally:
        store.close()


def test_sqlite_backend_honours_the_configured_path(tmp_path):
    """경로가 무시되면 개발 데이터가 매번 사라진다."""
    db = tmp_path / "memory.db"

    store = create_store(MemoryConfig(path=str(db)))
    try:
        assert db.exists()
    finally:
        store.close()


def test_postgres_without_a_dsn_is_rejected_when_the_store_is_opened():
    """DSN 은 env 로 오므로 파일 검증이 아니라 저장소를 여는 시점에 확인한다."""
    with pytest.raises(AaestroError) as exc_info:
        create_store(MemoryConfig(backend="postgres"))

    assert exc_info.value.code == ErrorCode.CFG_001
    assert exc_info.value.details["env"] == "AAESTRO_MEMORY__DSN"


def test_unreachable_postgres_surfaces_as_a_config_error():
    """드라이버 예외가 그대로 새어나가면 운영자가 원인을 읽을 수 없다."""
    config = MemoryConfig(backend="postgres", dsn="postgresql://user@127.0.0.1:1/none")

    with pytest.raises(AaestroError) as exc_info:
        create_store(config)

    assert exc_info.value.category is ErrorCategory.CONFIG
    assert exc_info.value.code == ErrorCode.CFG_001
