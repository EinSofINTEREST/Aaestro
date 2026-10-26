"""Which routes of an app skip the caller check (#462).

검사는 라우트 클래스 (``AdmittingRoute``) 에 있다. 앱에 ``@app.post`` 로 직접 붙인 라우트나 기본
``APIRouter`` 로 넣은 라우트는 조용히 무인증으로 열린다 — 구조 테스트가 그 목록을 허용 목록과
대조한다.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from aaestro.http_auth import AdmittingRoute


def unguarded_paths(app: FastAPI) -> set[str]:
    """검사를 거치지 않는 라우트의 경로 — include 된 라우터 안까지 따라간다."""
    return _unguarded(app.router.routes, prefix="")


def _unguarded(routes: list[Any], *, prefix: str) -> set[str]:
    found: set[str] = set()
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            # include 된 라우터 — 그 안의 라우트가 각자의 라우트 클래스를 갖는다
            nested = prefix + route.include_context.prefix
            found |= _unguarded(original.routes, prefix=nested)
            continue
        if isinstance(route, AdmittingRoute):
            continue
        found.add(prefix + route.path)
    return found
