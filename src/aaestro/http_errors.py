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

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode, field_problems

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

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


def request_validation_error(
    err: RequestValidationError, *, distinguish_missing: bool = True
) -> AaestroError:
    """Turn FastAPI's request validation failure into a typed error without the input.

    FastAPI 기본 422 는 ``detail[].input`` 에 받은 값을 되비춘다 — 본문이 비밀이거나 큰 문자열이면
    그대로 나간다 (쿼리 · 경로 파라미터 실패도 같다). 필드 경로와 사유만 싣는 ``VAL_00x`` 로
    바꾼다 (03 Client Rules 8). 필수 누락만 있으면 ``VAL_001``, 하나라도 형식 오류면 ``VAL_002``
    이고, 오류 목록이 비어 있으면 형식 오류로 본다.
    ``distinguish_missing=False`` 는 항상 ``VAL_002`` 다.
    """
    errors = err.errors()
    # 빈 목록은 all() 이 참이므로 따로 거른다
    missing = (
        distinguish_missing and bool(errors) and all(e.get("type") == "missing" for e in errors)
    )
    return AaestroError(
        category=ErrorCategory.VALIDATION,
        code=ErrorCode.VAL_001 if missing else ErrorCode.VAL_002,
        message="request failed validation",
        details={"errors": field_problems(err)},
    )


FRAMEWORK_BODY_PARSE_DETAIL = "There was an error parsing the body"
"""FastAPI 가 본문 해독 실패 (JSON 이 아닌 예외 — 깨진 UTF-8 등) 에 싣는 ``detail``.

400 을 typed 로 옮기는 **판별 근거**다. 상태 코드만 보면 앱 코드가 일부러 낸 400 까지 "본문을
읽지 못했다" 로 바뀐다. FastAPI 가 문구를 바꾸면 깨진 UTF-8 테스트가 기본 모양을 보고 실패한다.
"""


def unroutable_problem(err: StarletteHTTPException) -> AaestroError | None:
    """The typed form of a framework refusal, or None to keep the default handling.

    프레임워크가 라우팅 · 본문 해독에서 내는 거절을 typed 에러로 옮깁니다 — 기본 ``{"detail"}`` 이
    아니게. 대상은 둘뿐입니다: 틀린 메서드 405 와, FastAPI 가 만든 본문 해독 실패 400
    (``FRAMEWORK_BODY_PARSE_DETAIL``). 메시지는 고정 문구이고 경로 · 본문을 싣지 않습니다.

    404 는 **없다** — 라우트 부재의 404 는 코드 없는 기본 응답으로 둔다. UI · CLI · runtime 이
    "코드 없는 404 = 그 표면이 없다" 로 읽고, ``NF_001`` 은 "그 리소스가 없다" 로 읽는다 (#462).
    그 밖의 400 · 401 등도 None — 호출자가 기본 처리에 맡깁니다.
    """
    if err.status_code == HTTPStatus.METHOD_NOT_ALLOWED:
        message = "method not allowed"
    elif err.status_code == HTTPStatus.BAD_REQUEST and err.detail == FRAMEWORK_BODY_PARSE_DETAIL:
        message = "request body could not be read"
    else:
        return None
    return AaestroError(category=ErrorCategory.VALIDATION, code=ErrorCode.VAL_002, message=message)


__all__ = [
    "CATEGORY_STATUS",
    "CODE_STATUS",
    "DEFAULT_STATUS",
    "FRAMEWORK_BODY_PARSE_DETAIL",
    "RETRYABLE_STATUS",
    "request_validation_error",
    "status_for",
    "unroutable_problem",
]
