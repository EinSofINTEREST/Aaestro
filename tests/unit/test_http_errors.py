"""One rule for turning structured errors into HTTP status codes.

세 FastAPI 앱이 각자 매핑을 재발명해 서로 다르게 답하고 있었다 (#234) —
control plane 은 저장소 실패를 400 으로 내려 **서버 장애를 클라이언트 잘못으로**
보고했다. 05 의 사고 대응은 4xx/5xx 로 버킷을 가르므로 그 분류가 어긋나면
알림이 엉뚱한 곳으로 간다.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.http_errors import request_validation_error, status_for


def error(category: ErrorCategory, code: str, *, retryable: bool = False) -> AaestroError:
    return AaestroError(category=category, code=code, message="x", retryable=retryable)


@pytest.mark.parametrize(
    ("category", "code"),
    [
        (ErrorCategory.STORAGE, ErrorCode.STOR_003),
        (ErrorCategory.RUNTIME, ErrorCode.RT_002),
        (ErrorCategory.GRAPH, ErrorCode.GRAPH_002),
        (ErrorCategory.MODEL, ErrorCode.LLM_005),
        (ErrorCategory.INTERNAL, ErrorCode.INTERNAL_001),
    ],
)
def test_server_side_failures_are_5xx(category, code):
    """서버가 고쳐야 하는 실패를 4xx 로 내리면 그 결함이 4xx 버킷에 숨는다."""
    assert status_for(error(category, code)) >= HTTPStatus.INTERNAL_SERVER_ERROR


@pytest.mark.parametrize(
    ("category", "code", "expected"),
    [
        (ErrorCategory.VALIDATION, ErrorCode.VAL_001, HTTPStatus.BAD_REQUEST),
        (ErrorCategory.NOT_FOUND, ErrorCode.NF_001, HTTPStatus.NOT_FOUND),
        (ErrorCategory.FORBIDDEN, ErrorCode.A2A_004, HTTPStatus.FORBIDDEN),
        (ErrorCategory.CONFIG, ErrorCode.CFG_001, HTTPStatus.BAD_REQUEST),
    ],
)
def test_caller_fixable_failures_are_4xx(category, code, expected):
    """호출자가 고칠 수 있는 것만 4xx 다."""
    assert status_for(error(category, code)) == expected


@pytest.mark.parametrize(
    ("category", "code"),
    [
        (ErrorCategory.GRAPH, ErrorCode.GRAPH_006),
        (ErrorCategory.RUNTIME, ErrorCode.RT_010),
        (ErrorCategory.RUNTIME, ErrorCode.RT_011),
        (ErrorCategory.RUNTIME, ErrorCode.RT_012),
    ],
)
def test_a_state_conflict_is_409_even_though_its_category_is_5xx(category, code):
    """#256 — 정지한 run 재개, 겹쳐 배포, 그리고 겹친 이미지 빌드(#265). 운영자가 해소한다.

    5xx 로 답하면 조치 가능한 실수가 서버 장애 알림 버킷으로 새어 나간다.
    """
    assert status_for(error(category, code)) == HTTPStatus.CONFLICT


@pytest.mark.parametrize(
    ("category", "code"),
    [
        # 같은 카테고리의 **진짜** 서버 실패는 500 그대로여야 한다 — 과잉 4xx 화 방지
        (ErrorCategory.GRAPH, ErrorCode.GRAPH_002),
        (ErrorCategory.RUNTIME, ErrorCode.RT_001),
        (ErrorCategory.RUNTIME, ErrorCode.RT_004),
    ],
)
def test_the_conflict_mapping_does_not_leak_to_sibling_codes(category, code):
    assert status_for(error(category, code)) == HTTPStatus.INTERNAL_SERVER_ERROR


def test_a_missing_resource_is_404_regardless_of_category():
    """`NF_001` 은 어느 앱에서든 404 다 — control plane 에서만 그랬다."""
    assert status_for(error(ErrorCategory.STORAGE, ErrorCode.NF_001)) == HTTPStatus.NOT_FOUND


def test_retryable_wins_over_the_category():
    """다시 걸어보라는 신호가 카테고리보다 강하다."""
    retryable = error(ErrorCategory.NETWORK, ErrorCode.NET_002, retryable=True)

    assert status_for(retryable) == HTTPStatus.SERVICE_UNAVAILABLE


def test_an_unclassified_error_is_treated_as_server_side():
    """모르는 것을 400 으로 내리면 서버 결함이 조용히 4xx 로 샌다."""
    unknown = error(ErrorCategory.RATE_LIMIT, "SOMETHING_NEW")

    assert status_for(unknown) >= HTTPStatus.INTERNAL_SERVER_ERROR


def test_an_override_wins_over_every_shared_rule():
    """앱마다 다른 것이 의도된 경우 — 없애지 않고 한 곳에서 보이게 한다."""
    denied = error(ErrorCategory.MEMORY, ErrorCode.MEM_001)
    overrides = {ErrorCode.MEM_001: HTTPStatus.UNAUTHORIZED}

    assert status_for(denied, overrides=overrides) == HTTPStatus.UNAUTHORIZED
    assert status_for(denied) != HTTPStatus.UNAUTHORIZED, (
        "override 없이도 401 이면 override 가 무의미하다"
    )


def test_an_override_beats_a_shared_code_mapping():
    """override 는 공용 코드 매핑보다도 우선한다 — 좁은 것이 이긴다."""
    missing = error(ErrorCategory.NOT_FOUND, ErrorCode.NF_001)

    assert status_for(missing, overrides={ErrorCode.NF_001: HTTPStatus.GONE}) == HTTPStatus.GONE


class Body(BaseModel):
    name: str
    count: int


def failure(payload: dict[str, object]) -> RequestValidationError:
    try:
        Body.model_validate(payload)
    except ValidationError as err:
        return RequestValidationError(err.errors())
    raise AssertionError("expected a validation failure")


def test_request_validation_error_picks_val_001_only_when_everything_is_missing():
    assert request_validation_error(failure({})).code == ErrorCode.VAL_001
    assert request_validation_error(failure({"count": "secret-text"})).code == ErrorCode.VAL_002


def test_request_validation_error_can_always_answer_val_002():
    err = request_validation_error(failure({}), distinguish_missing=False)

    assert err.code == ErrorCode.VAL_002


def test_request_validation_error_with_no_errors_is_a_format_error():
    assert request_validation_error(RequestValidationError([])).code == ErrorCode.VAL_002
