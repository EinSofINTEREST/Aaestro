"""Bearer-token guard shared by every HTTP surface.

agentd 의 Control API 와 orchestrator 의 Control Plane 이 **같은** 검사를 쓴다 (#241).
같은 검사가 두 벌 있으면 한쪽만 고쳐진다 — 그리고 orchestrator 가 agentd 에서
import 하는 것은 07 의 의존 방향에 어긋나므로, 둘 다 여기서 가져간다.

검사는 **본문을 읽기 전에** 돈다 (``admit_first``, #462). FastAPI 는 의존성을 풀기 전에 본문을
읽고 파싱하므로, ``Depends`` 로 건 검사는 깨진 JSON · UTF-8 본문보다 늦다 — 무인증 호출자가 응답
차이로 라우트의 본문 처리를 알게 된다.

토큰 값은 로그에 남기지 않는다 — 05 의 마스킹은 키 이름으로 판정하므로 이 모듈은
값을 로그 필드에 싣지 않는 것으로 그 규칙을 지킨다.
"""

from __future__ import annotations

import hmac
import inspect
import ipaddress
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import HTTPException, Request, Response, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.routing import APIRoute

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.types import ASGIApp, Receive, Scope, Send

BEARER = "Bearer "

ALLOW_UNAUTHENTICATED_ENV = "AAESTRO_ALLOW_UNAUTHENTICATED"
"""무인증 기동 opt-in — 켜져 있어도 바인드 주소가 loopback 일 때만 유효하다 (#394)."""

_OPT_IN_VALUES = frozenset({"1", "true"})
_OPT_OUT_VALUES = frozenset({"", "0", "false"})

log = structlog.get_logger(__name__)

Admission = Callable[[Request], Awaitable[None] | None]
"""요청의 신원을 확인하는 검사 — 거절은 예외 (``HTTPException`` 401 · ``AaestroError``) 로 한다."""


def is_loopback(host: str) -> bool:
    """bind 주소가 loopback 인가 — 이것만이 무인증을 허용하는 유일한 근거다."""
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_required_token(
    environ: Mapping[str, str], *, token_env: str, bind_host: str, component: str
) -> str | None:
    """Decide the token a server must enforce, or refuse to start.

    서버가 강제할 토큰을 정하거나 기동을 거절합니다. 토큰이 없거나 공백뿐이면 CFG_001 —
    예외는 명시적 opt-in(``AAESTRO_ALLOW_UNAUTHENTICATED``) **이면서** loopback 바인드일 때뿐이고,
    그때 None(검사 없음)을 돌려주며 WARN 을 남깁니다. 토큰 값은 메시지·로그에 싣지 않습니다.

    Raises:
        AaestroError: CONFIG/``CFG_001`` if the token is missing and the opt-in is absent,
            malformed, or paired with a non-loopback bind.
    """
    raw = environ.get(token_env, "")
    token = raw.strip()
    raw_opt_in = environ.get(ALLOW_UNAUTHENTICATED_ENV, "").strip().lower()
    if raw_opt_in not in _OPT_IN_VALUES | _OPT_OUT_VALUES:
        # 모르는 값을 조용히 끄면 오설정이 "왜 안 열리지" 로 늦게 드러난다
        raise _refusal(
            f"{ALLOW_UNAUTHENTICATED_ENV} must be 1, true, 0 or false",
            component=component,
            setting=ALLOW_UNAUTHENTICATED_ENV,
        )
    if token and token != raw:
        # 조용히 strip 하면 클라이언트가 보낼 수 없는 값을 기대하거나 발급자와 다른 값을 강제한다
        raise _refusal(
            f"{token_env} must not have leading or trailing whitespace",
            component=component,
            setting=token_env,
        )
    if token:
        if raw_opt_in in _OPT_IN_VALUES:
            # 토큰이 우선이라 opt-in 은 무시된다 — 운영자가 무인증이라 믿고 있을 수 있다
            log.warning(
                "unauthenticated opt-in is ignored because a token is set",
                component=component,
                setting=ALLOW_UNAUTHENTICATED_ENV,
            )
        return raw
    if raw_opt_in not in _OPT_IN_VALUES:
        raise _refusal(
            f"{component} requires {token_env} (set it, or opt in to loopback-only "
            f"unauthenticated mode with {ALLOW_UNAUTHENTICATED_ENV}=1)",
            component=component,
            setting=token_env,
        )
    if not is_loopback(bind_host):
        raise _refusal(
            f"{ALLOW_UNAUTHENTICATED_ENV} is valid only on a loopback bind",
            component=component,
            setting=ALLOW_UNAUTHENTICATED_ENV,
        )
    log.warning(
        "starting without authentication — allowed only because it binds to loopback",
        component=component,
        host=bind_host,
        setting=ALLOW_UNAUTHENTICATED_ENV,
    )
    return None


def _refusal(message: str, **details: str) -> AaestroError:
    return AaestroError(
        category=ErrorCategory.CONFIG,
        code=ErrorCode.CFG_001,
        message=message,
        details=details,
    )


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


class AdmittingRoute(APIRoute):
    """Marker base of every route that checks the caller before the body (``admit_first``).

    구조 테스트가 "앱의 라우트 중 검사를 거치지 않는 것은 허용 목록뿐" 을 이 타입으로 단언한다.
    """


def admit_first(admit: Admission) -> type[AdmittingRoute]:
    """Build a route class that checks the caller before the body is read.

    본문을 읽고 파싱하기 **전에** ``admit`` 을 부르는 라우트 클래스를 만듭니다. 라우터의
    ``route_class`` 로 쓰면 그 라우터의 모든 라우트가 같은 검사를 거칩니다 — 신원이 없으면 본문이
    깨진 JSON 이든 깨진 UTF-8 이든 빈 본문이든 같은 거절입니다.

    무인증으로 열 라우트는 이 클래스를 쓰지 않는 라우터(앱 자체)에 **명시적으로** 둡니다.
    라우트를 고르기 전의 검사는 ``AdmitBeforeRouting`` 이 맡습니다 — 앱이 여러 청중을 가지면
    그쪽은 "어느 청중이든 자격이 있는가" 만 보고, 이 클래스가 라우트의 청중으로 좁힙니다.

    Args:
        admit: Raises to refuse. ``require_token(...)`` 처럼 동기여도, 비동기여도 됩니다.
    """

    class AdmitFirstRoute(AdmittingRoute):
        def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
            parse_and_run = super().get_route_handler()

            async def admit_then_run(request: Request) -> Response:
                outcome = admit(request)
                if inspect.isawaitable(outcome):
                    await outcome
                return await parse_and_run(request)

            return admit_then_run

    return AdmitFirstRoute


class AdmitBeforeRouting:
    """ASGI middleware that checks the caller before the request is routed (#462).

    라우팅 **전에** 자격을 확인하는 미들웨어입니다. 라우트를 고른 뒤의 검사(``admit_first``)만
    있으면 무인증 호출자가 응답 차이로 라우트 표를 열거합니다 — 있는 경로는 401, 없는 경로는
    404, 틀린 메서드는 405 와 ``Allow`` 헤더. 여기서 거절하면 경로 · 메서드와 무관하게
    같은 거절입니다.

    거절은 ``admit`` 이 던지는 ``HTTPException`` 이고, FastAPI 기본 모양 그대로 답합니다
    (``{"detail"}`` + 헤더). 검사가 ``AaestroError`` (신원 저장소 장애 등) 를 내면 ``render_error``
    로 앱과 같은 typed 응답을 냅니다 — 이 층은 앱의 예외 핸들러 바깥이라, 넘기지 않으면 plain
    500 이 됩니다. ``render_error`` 가 없으면 그대로 올립니다. 열린 경로는 정확히 일치하는
    ``open_paths`` 와 ``open_prefixes`` 아래뿐입니다. 자격이 있는 요청은 그대로 라우팅되어
    404 · 405 를 지금처럼 받습니다.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        admit: Admission,
        open_paths: frozenset[str],
        open_prefixes: tuple[str, ...] = (),
        render_error: Callable[[AaestroError], Response] | None = None,
    ) -> None:
        self.app = app
        self._admit = admit
        self._open_paths = open_paths
        self._open_prefixes = open_prefixes
        self._render_error = render_error

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._is_open(scope["path"]):
            await self.app(scope, receive, send)
            return
        # 헤더만 읽는다 — 본문(receive)은 건드리지 않는다
        request = Request(scope)
        try:
            outcome = self._admit(request)
            if inspect.isawaitable(outcome):
                await outcome
        except HTTPException as err:
            response = await http_exception_handler(request, err)
            await response(scope, receive, send)
            return
        except AaestroError as err:
            if self._render_error is None:
                raise
            await self._render_error(err)(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _is_open(self, path: str) -> bool:
        return path in self._open_paths or path.startswith(self._open_prefixes)


__all__ = [
    "ALLOW_UNAUTHENTICATED_ENV",
    "BEARER",
    "AdmitBeforeRouting",
    "Admission",
    "AdmittingRoute",
    "admit_first",
    "is_loopback",
    "presented_token",
    "require_token",
    "resolve_required_token",
]
