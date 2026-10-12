"""Startup failure handling shared by the process entry points.

진입점 (agentd · control plane · Memory Service) 이 기동 실패를 같은 모양으로 보고하게 한다:
traceback 대신 구조화 로그 한 줄 (``error_code`` · ``details`` 의 path 와 ``{field, problem}``) 과
비정상 종료. ``AaestroError.__str__`` 은 details 를 싣지 않으므로 처리되지 않은 예외로 두면
운영자는 어느 파일의 어느 필드인지 알 수 없다.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from aaestro.core.errors import AaestroError

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def startup_guard(log: Any, message: str) -> Iterator[None]:
    """Turn an :class:`AaestroError` raised in the block into one critical log line and exit 1.

    블록에서 ``AaestroError`` 가 나면 ``log.critical`` 한 줄을 남기고 ``SystemExit(1)`` 한다.
    ``details`` 는 예약 키 (``event``, ``level`` …) 와 겹치지 않게 한 필드로 묶는다.
    """
    try:
        yield
    except AaestroError as err:
        log.critical(message, error_code=err.code, error_message=err.message, details=err.details)
        raise SystemExit(1) from None


__all__ = ["startup_guard"]
