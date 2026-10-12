"""Request bodies to models — one error shape for every control plane route."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode


def parsed[T: BaseModel](body: Any, model: type[T]) -> T:
    """요청 본문을 모델로 — 스키마 위반은 **어느 필드가 왜** 인지 담아 400 으로.

    FastAPI 의 기본 422 는 카탈로그가 깨진 파일에 대해 내는 형식과 다르다 —
    UI 가 한 가지 모양만 다루게 같은 `VAL_002` details 로 맞춘다. 본문을 ``Any`` 로
    받는 이유도 같다: ``dict`` 로 받으면 배열·문자열 본문이 여기 오기 전에 422 로 샌다.
    """
    try:
        return model.model_validate(body)
    except ValidationError as err:
        raise AaestroError(
            category=ErrorCategory.VALIDATION,
            code=ErrorCode.VAL_002,
            message="request body failed schema validation",
            details={
                "errors": [
                    {"field": ".".join(str(loc) for loc in e["loc"]), "problem": e["msg"]}
                    for e in err.errors()
                ]
            },
        ) from err


__all__ = ["parsed"]
