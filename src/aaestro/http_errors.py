"""Mapping structured errors onto HTTP status codes.

`AaestroError` 를 HTTP 상태코드로 옮기는 **단일 규칙**. 세 FastAPI 앱
(`agentd/server`, `memory/http`, `orchestrator/control`)이 각자 매핑을 재발명해
서로 다르게 답하고 있었다 (#234) — control plane 은 저장소 실패(`STOR_003`)를
`400 Bad Request` 로 내려, 서버 장애를 클라이언트 잘못으로 보고했다.

05 의 사고 대응은 4xx 와 5xx 로 버킷을 가른다. 그 경계를 여기서 한 번만 정한다:

- **4xx** — 호출자가 고칠 수 있다 (형식 오류, 없는 리소스, 권한)
- **5xx** — 서버가 고쳐야 한다 (저장소, 런타임, 내부 오류)
- **503** — 지금은 안 되지만 다시 걸어볼 만하다 (`retryable`)

앱마다 다른 것이 **의도된** 경우는 `overrides` 로 남긴다 — 없애는 것이 목적이
아니라 한 곳에서 보이게 하는 것이 목적이다.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

from aaestro.core.errors import ErrorCategory, ErrorCode

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aaestro.core.errors import AaestroError

CODE_STATUS: Mapping[str, int] = {
    # 없는 리소스를 400 으로 답하면 호출자가 "요청이 틀렸나" 를 먼저 의심한다
    ErrorCode.NF_001: HTTPStatus.NOT_FOUND,
    # 상태 충돌 — 카테고리(graph/runtime)는 5xx 지만 이 둘은 **호출자가 해소한다**:
    # 정지한 run 은 재개 대신 새로 제출하고, 겹친 배포는 먼저 해체하면 된다.
    # 5xx 로 답하면 운영자의 조치 가능한 실수가 서버 장애 알림으로 새어 나간다 (#256)
    ErrorCode.GRAPH_006: HTTPStatus.CONFLICT,
    ErrorCode.RT_010: HTTPStatus.CONFLICT,
    ErrorCode.RT_011: HTTPStatus.CONFLICT,
    ErrorCode.RT_012: HTTPStatus.CONFLICT,
}
"""코드 하나가 카테고리보다 정확한 경우 — 카테고리보다 먼저 본다."""

CATEGORY_STATUS: Mapping[ErrorCategory, int] = {
    ErrorCategory.VALIDATION: HTTPStatus.BAD_REQUEST,
    ErrorCategory.NOT_FOUND: HTTPStatus.NOT_FOUND,
    ErrorCategory.FORBIDDEN: HTTPStatus.FORBIDDEN,
    ErrorCategory.CONFIG: HTTPStatus.BAD_REQUEST,
    ErrorCategory.MODULE: HTTPStatus.BAD_REQUEST,
    # --- 여기부터는 서버가 고쳐야 하는 것들 ---
    ErrorCategory.STORAGE: HTTPStatus.INTERNAL_SERVER_ERROR,
    ErrorCategory.RUNTIME: HTTPStatus.INTERNAL_SERVER_ERROR,
    ErrorCategory.GRAPH: HTTPStatus.INTERNAL_SERVER_ERROR,
    ErrorCategory.MEMORY: HTTPStatus.INTERNAL_SERVER_ERROR,
    ErrorCategory.MODEL: HTTPStatus.INTERNAL_SERVER_ERROR,
    ErrorCategory.DECISION: HTTPStatus.INTERNAL_SERVER_ERROR,
    ErrorCategory.A2A: HTTPStatus.INTERNAL_SERVER_ERROR,
    ErrorCategory.MCP: HTTPStatus.INTERNAL_SERVER_ERROR,
    ErrorCategory.INTERNAL: HTTPStatus.INTERNAL_SERVER_ERROR,
}
"""카테고리별 기본 — 05 의 taxonomy 를 4xx/5xx 경계로 옮긴 것."""

DEFAULT_STATUS = HTTPStatus.INTERNAL_SERVER_ERROR
"""분류되지 않은 에러는 **서버 쪽**으로 본다.

모르는 것을 400 으로 내리면 서버 결함이 4xx 버킷에 숨어 알림이 울리지 않는다.
"""

RETRYABLE_STATUS = HTTPStatus.SERVICE_UNAVAILABLE
"""`retryable` 은 카테고리보다 강하다 — 다시 걸어보라는 신호가 우선이다."""


def status_for(err: AaestroError, *, overrides: Mapping[str, int] | None = None) -> int:
    """Pick the HTTP status for a structured error.

    구조화 에러에 맞는 HTTP 상태코드를 고릅니다.

    우선순위는 **좁은 것부터**입니다: 앱별 override → 공용 코드 매핑 →
    ``retryable`` → 카테고리 → 기본값.

    Args:
        err: The error to translate.
        overrides: App-specific code mappings that win over the shared rules.
            앱마다 다른 것이 의도된 경우에만 씁니다. ``AaestroError.code`` 가
            ``str`` 이므로 키도 ``str`` 이다 (``ErrorCode`` 는 ``StrEnum`` 이라
            그대로 넣을 수 있다).

    Returns:
        The HTTP status code.
    """
    if overrides and err.code in overrides:
        return overrides[err.code]
    if err.code in CODE_STATUS:
        return CODE_STATUS[err.code]
    if err.retryable:
        return RETRYABLE_STATUS
    return CATEGORY_STATUS.get(err.category, DEFAULT_STATUS)


__all__ = [
    "CATEGORY_STATUS",
    "CODE_STATUS",
    "DEFAULT_STATUS",
    "RETRYABLE_STATUS",
    "status_for",
]
