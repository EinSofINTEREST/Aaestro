"""Memory store backend selection.

설정이 선언한 백엔드로 저장소를 만든다. 팩토리가 없으면 설정의
``backend: postgres`` 는 문서상의 약속으로만 남는다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.memory.store import SqliteMemoryStore

if TYPE_CHECKING:
    from aaestro.config import MemoryConfig
    from aaestro.memory.store import MemoryStore


def create_store(config: MemoryConfig) -> MemoryStore:
    """Build the memory store declared by the config.

    설정이 선언한 메모리 저장소를 만듭니다.

    Args:
        config: The validated memory settings.

    Returns:
        A store implementing the ``MemoryStore`` contract.

    Raises:
        AaestroError: CONFIG/``CFG_001`` if the backend cannot be built.
    """
    if config.backend == "sqlite":
        return SqliteMemoryStore(path=config.path)

    # DSN 은 자격증명을 담으므로 설정 파일이 아니라 AAESTRO_MEMORY__DSN 으로
    # 주입한다 — 그래서 파일 검증이 아니라 저장소를 실제로 여는 여기서 확인한다
    if not config.dsn:
        raise AaestroError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message="postgres memory backend requires a dsn",
            details={"backend": config.backend, "env": "AAESTRO_MEMORY__DSN"},
        )

    # psycopg 는 postgres 를 실제로 쓸 때만 import 한다 — sqlite 개발 환경이
    # 드라이버 부재로 기동에 실패하지 않게 한다
    from aaestro.memory.postgres import PostgresMemoryStore

    try:
        return PostgresMemoryStore(dsn=config.dsn)
    except Exception as err:
        raise AaestroError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message="failed to connect the postgres memory store",
            details={"backend": config.backend, "cause": type(err).__name__},
        ) from err


__all__ = ["create_store"]
