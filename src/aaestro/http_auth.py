"""Bearer-token guard shared by every HTTP surface.

agentd 의 Control API 와 orchestrator 의 Control Plane 이 **같은** 검사를 쓴다 (#241).
같은 검사가 두 벌 있으면 한쪽만 고쳐진다 — 그리고 orchestrator 가 agentd 에서
import 하는 것은 07 의 의존 방향에 어긋나므로, 둘 다 여기서 가져간다.

토큰 값은 로그에 남기지 않는다 — 05 의 마스킹은 키 이름으로 판정하므로 이 모듈은
값을 로그 필드에 싣지 않는 것으로 그 규칙을 지킨다.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request, status

if TYPE_CHECKING:
    from collections.abc import Callable

BEARER = "Bearer "


def presented_token(request: Request) -> str | None:
    """요청이 제시한 Bearer 토큰 — 없거나 형식이 다르면 None."""
    header = request.headers.get("authorization", "")
    if not header.startswith(BEARER):
        return None
    return header[len(BEARER) :] or None


def require_token(expected: str | None, *, realm: str = "token") -> Callable[[Request], None]:
    """Build a dependency that rejects requests without the expected token.

    기대하는 토큰이 없는 요청을 401 로 거절하는 FastAPI 의존성을 만듭니다.

    ``expected`` 가 None 이면 검사하지 않습니다 — **켜는 것은 조립하는 쪽의
    결정**이고, 그 결정이 안전한지(loopback 인지)는 진입점이 판단합니다.

    Args:
        expected: The token every request must present, or None to disable.
        realm: 거절 사유에 붙는 이름 — "agent token" / "control plane token".
    """

    def check(request: Request) -> None:
        if expected is None:
            return
        presented = presented_token(request) or ""
        # 상수 시간 비교 — 일반 `!=` 는 첫 불일치 바이트에서 끝나 토큰을 한 바이트씩
        # 맞춰 볼 수 있다. 값이 없으면 빈 문자열과 비교해 분기 시간을 같게 둔다
        if not hmac.compare_digest(presented.encode(), expected.encode()):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"invalid {realm}",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return check


__all__ = ["BEARER", "presented_token", "require_token"]
