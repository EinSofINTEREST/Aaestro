"""Request bodies to models — one error shape for every control plane route."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from aaestro.core.errors import (
    AaestroError,
    ErrorCategory,
    ErrorCode,
    field_problems,
    printable_label,
    raise_detached,
)
from aaestro.materials import MAX_FILE_BYTES, MAX_FILES, MAX_PATH_CHARS

DEFAULT_BODY_LIMIT = 1024 * 1024
"""JSON 본문 상한 (바이트) — 재료 업로드 외의 본문은 선언 · 요청이라 1 MiB 면 넉넉하다."""

MATERIALS_BODY_LIMIT = MAX_FILES * (6 * MAX_FILE_BYTES + 12 * MAX_PATH_CHARS) + 1024 * 1024
"""재료 업로드의 상한 — 내용은 UTF-8 바이트 기준이라 JSON 이스케이프 (`\\u00XX`) 가 바이트당 최대 6.
경로는 문자 기준이라 BMP 밖 문자 하나가 surrogate pair (`\\uXXXX\\uXXXX`) 로 최대 12.
구조 (괄호 · 따옴표) 용 1 MiB 를 더한다."""

MATERIALS_ROUTE = "/v1/agents/{name}/materials"


def _body_limit(request: Request) -> int:
    # 라우트 매칭 뒤에 도는 가드라 매칭된 템플릿으로 가른다 — root_path 접두사에 영향받지 않는다
    route = request.scope.get("route")
    is_materials = request.method == "PUT" and getattr(route, "path", None) == MATERIALS_ROUTE
    return MATERIALS_BODY_LIMIT if is_materials else DEFAULT_BODY_LIMIT


def _too_large() -> StarletteHTTPException:
    return StarletteHTTPException(status_code=413, detail="request body too large")


class _DuplicateKey(Exception):  # noqa: N818 — 파싱을 끊는 내부 신호
    """hook 이 첫 중복을 만나면 키 이름만 들고 빠져나온다 — 값은 들지 않는다."""

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise _DuplicateKey(key)
        obj[key] = value
    return obj


async def guard_body(request: Request) -> None:
    """Cap the body size and refuse duplicate JSON keys, before FastAPI parses it.

    파싱 · 검증 **전에** 본문을 읽어 상한과 중복 키를 본다.
    상한을 넘는 청크를 받는 즉시 멈춘다. 읽은 본문은
    FastAPI 가 같은 ``request.body()`` 로 다시 쓰도록 되돌려 둔다 — 깨진 JSON · UTF-8 은 여기서
    건드리지 않고 기존 거절 경로에 맡긴다.
    """
    limit = _body_limit(request)
    declared = request.headers.get("content-length", "")
    # 자리 수를 먼저 본다 — 아주 긴 숫자를 int() 로 바꾸다 예외가 나지 않게
    digits = declared.lstrip("0")  # 앞의 0 은 자리 수에 세지 않는다
    if declared.isdigit() and (len(digits) > len(str(limit)) or int(digits or "0") > limit):
        raise _too_large()
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _too_large()
        chunks.append(chunk)
    data = b"".join(chunks)
    # starlette 가 body() 의 캐시로 쓰는 속성 — 공개된 되돌림 수단이 없다
    request._body = data  # noqa: SLF001
    if not data:
        return
    try:
        json.loads(data, object_pairs_hook=_reject_duplicates)
    except _DuplicateKey as dup:
        raise AaestroError(
            category=ErrorCategory.VALIDATION,
            code=ErrorCode.VAL_002,
            message="request body has a duplicate key",
            # 키는 호출자가 고른 문자열이다 — 짝 없는 surrogate · 제어 문자 · 긴 값을 정제해 싣는다
            details={"key": printable_label(dup.key)},
        ) from None
    except RecursionError:
        # FastAPI 가 같은 본문을 다시 파싱하기 전에 여기서 끝낸다 — 500 이 되지 않게
        raise AaestroError(
            category=ErrorCategory.VALIDATION,
            code=ErrorCode.VAL_002,
            message="request body is nested too deeply",
        ) from None
    except ValueError:  # 깨진 JSON · UTF-8 은 FastAPI 의 기존 거절이 맡는다
        return


def then_guard(admit: Callable[[Request], Any]) -> Callable[[Request], Awaitable[None]]:
    """Run the caller check (sync or async) first, then the body guard.

    자격 검사 **뒤에** 본문 가드를 건다 — 자격 없는 큰 본문은 읽지 않고 거절된다.
    """

    async def admit_then_guard(request: Request) -> None:
        outcome = admit(request)
        if inspect.isawaitable(outcome):
            await outcome
        await guard_body(request)

    return admit_then_guard


def parsed[T: BaseModel](body: Any, model: type[T]) -> T:
    """요청 본문을 모델로 — 스키마 위반은 **어느 필드가 왜** 인지 담아 400 으로.

    FastAPI 의 기본 422 는 카탈로그가 깨진 파일에 대해 내는 형식과 다르다 —
    UI 가 한 가지 모양만 다루게 같은 `VAL_002` details 로 맞춘다. 본문을 ``Any`` 로
    받는 이유도 같다: ``dict`` 로 받으면 배열·문자열 본문이 여기 오기 전에 422 로 샌다.
    """
    try:
        return model.model_validate(body)
    except ValidationError as err:
        raise_detached(
            AaestroError(
                category=ErrorCategory.VALIDATION,
                code=ErrorCode.VAL_002,
                message="request body failed schema validation",
                details={"errors": field_problems(err)},
            )
        )


__all__ = ["DEFAULT_BODY_LIMIT", "MATERIALS_BODY_LIMIT", "guard_body", "parsed", "then_guard"]
