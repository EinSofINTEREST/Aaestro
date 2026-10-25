"""Agent Control API server.

agentd 가 컨테이너 내부에서 서빙하는 표준 API. Runtime layer 외에는 직접
호출하지 않는다.

태스크 실패는 데몬을 죽이지 않는다 — 최상위 핸들러가 uncaught exception 을
``INTERNAL`` 로 변환해 구조화 응답으로 돌려준다.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from fastapi import APIRouter, FastAPI, Request, Response, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

from aaestro.core.agent import HealthState, HealthStatus, TaskKey, TaskRequest, TaskResult
from aaestro.core.errors import AaestroError, AaestroErrorPayload, ErrorCategory, ErrorCode
from aaestro.http_auth import AdmitBeforeRouting, admit_first, require_token
from aaestro.http_errors import request_validation_error, status_for, unroutable_problem

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

DEFAULT_MAX_CONCURRENT_TASKS = 4
HEALTH_PATH = "/v1/health"
"""토큰 없이 닿는 유일한 경로 — Docker healthcheck 가 직접 부른다 (02 API Rules 4)."""
SSE_MEDIA_TYPE = "text/event-stream"


class Acknowledgement(BaseModel):
    """A minimal acknowledgement body.

    부수효과만 있는 엔드포인트의 응답 — ad-hoc dict 대신 모델을 쓴다.
    """

    model_config = ConfigDict(frozen=True)

    status: str = "accepted"


class SteerRequest(BaseModel):
    """`/v1/steer/{task_id}` 본문.

    길이·대기 수 상한은 대기열을 쥔 실행기가 ``VAL_002`` 로 판정한다 — 여기서 걸면 본문 검증
    실패 핸들러의 일반 ``VAL_00x`` 로 나가 상한 사유(``SteerProblem``)와 모양이 갈린다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    by: str


class AgentRuntime:
    """The agent state a Control API server exposes.

    Control API 가 노출하는 에이전트 상태. 서버는 이 계약만 알고, 실행 방식은
    executor 가 결정한다.
    """

    def __init__(
        self,
        *,
        agent: str,
        executor: Any,
        card: dict[str, Any] | None = None,
        health: Callable[[], HealthStatus] | None = None,
        reload: Callable[[], Awaitable[dict[str, Any] | None]] | None = None,
        max_concurrent_tasks: int = DEFAULT_MAX_CONCURRENT_TASKS,
    ) -> None:
        self.agent = agent
        self.executor = executor
        self._card = card or {"name": agent}
        self._health = health
        self._reload = reload
        # direct 요청과 그래프 태스크가 같은 큐를 공유한다 (02 Direct Request Rules 5)
        self.semaphore = asyncio.Semaphore(max_concurrent_tasks)
        self.draining = False
        self._inflight: dict[TaskKey, asyncio.Task[Any]] = {}

    def card(self) -> dict[str, Any]:
        """A2A AgentCard."""
        return dict(self._card)

    def health(self) -> HealthStatus:
        """에이전트 상태 — drain 중에는 degraded 로 보고한다."""
        status_ = self._health() if self._health else HealthStatus(status=HealthState.HEALTHY)
        if self.draining:
            return HealthStatus(
                status=HealthState.DEGRADED,
                components=status_.components,
            )
        return status_

    async def reload(self) -> bool:
        """promptset/skillset 무중단 리로드 — 리로드할 것이 없는 실행기면 False.

        훅이 새 AgentCard 를 돌려주면 그것으로 바꾼다: card 의 skill 목록은 실제로 로드된
        도구와 일치해야 한다 (03 AgentCard 1). 훅이 실패하면 예외가 그대로 올라가고 card 도
        실행기도 이전 상태로 남는다.
        """
        if self._reload is None:
            return False
        card = await self._reload()
        if card is not None:
            self._card = dict(card)
        return True

    def track(self, key: TaskKey, task: asyncio.Task[Any]) -> None:
        """진행 중 태스크를 등록한다 — 취소 대상 추적용.

        키는 멱등 캐시와 같은 ``TaskRequest.key`` 다: id 만으로 잡으면 peer 가 같은 id 의
        A2A 태스크로 진행 중 노드 태스크의 자리를 덮을 수 있다 (#400).
        """
        self._inflight[key] = task
        task.add_done_callback(lambda _t: self._inflight.pop(key, None))

    def cancel(self, task_id: str) -> bool:
        """Control API 로 들어온 진행 중 태스크를 취소한다. 없으면 False.

        취소를 부를 수 있는 것은 Control API 뿐이므로 대상도 그쪽 태스크뿐이다 — peer 가 만든
        동명 태스크는 이 id 로 가리켜지지 않는다.
        """
        task = self._inflight.get(TaskKey.from_control(task_id))
        if task is None:
            return False
        task.cancel()
        return True

    async def steer(self, task_id: str, text: str, *, by: str) -> bool:
        """진행 중 태스크에 운영자 지시를 대기시킨다 — steer 를 모르는 실행기면 False.

        진행 여부는 실행기가 안다: `/invoke` 뿐 아니라 `/stream` 과 A2A 로 받은 태스크도 같은
        루프를 돌기 때문에, 서버의 취소 추적표(`/invoke` 만 등록)로 판정하면 그 둘이 빠진다.
        """
        steer = getattr(self.executor, "steer", None)
        if not callable(steer):
            return False
        await steer(task_id, text, by=by)
        return True

    async def drain(self) -> None:
        """새 태스크 수락을 멈추고 진행 중 태스크를 기다린다."""
        self.draining = True
        inflight = list(self._inflight.values())
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)


def _error_response(err: AaestroError, http_status: int) -> JSONResponse:
    """구조화 에러를 JSON 응답으로 만든다."""
    return JSONResponse(status_code=http_status, content=err.payload().model_dump(mode="json"))


def create_app(runtime: AgentRuntime, *, token: str | None = None) -> FastAPI:
    """Build the Control API application.

    Control API 앱을 만듭니다.

    Args:
        runtime: The agent runtime being served.
        token: Per-agent token required on authenticated endpoints.

    Returns:
        The FastAPI application.
    """
    # 문서 경로를 닫는다 — 무인증 호출자가 스키마(필드 · 상한)를 열거하지 못하게.
    # redirect_slashes 도 닫는다 — 기본값이면 `/v1/invoke/` 가 307 로 정식 경로를 알려 준다
    # (라우트 매칭은 인증보다 먼저다, #462)
    app = FastAPI(
        title=f"aaestro-agentd:{runtime.agent}",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    # 토큰은 본문을 읽기 전에 본다 — `Depends` 는 본문 파싱보다 늦다 (#462)
    check = require_token(token, realm="agent token")
    guarded = admit_first(check)
    if token is not None:
        # 라우팅보다도 먼저 — 없는 경로 404 · 틀린 메서드 405 + `Allow` 로 라우트 표가 새지 않게.
        # 라우트별 검사(guarded)와 같은 검사다: 라우터에서 빠진 라우트도 이 층이 막는다
        open_health = frozenset({("GET", HEALTH_PATH)})
        app.add_middleware(AdmitBeforeRouting, admit=check, open_routes=open_health)

    @app.exception_handler(RequestValidationError)
    async def _bad_body(_request: Request, err: RequestValidationError) -> JSONResponse:
        """FastAPI 기본 422 는 받은 값을 응답에 되비춘다 — 필드와 사유만 남긴다 (#448)."""
        problem = request_validation_error(err)
        problem.agent = runtime.agent
        return _error_response(problem, status_for(problem))

    @app.exception_handler(StarletteHTTPException)
    async def _unroutable(request: Request, err: StarletteHTTPException) -> Response:
        """깨진 UTF-8 본문 · 틀린 메서드도 typed 모양으로 — 기본 ``{"detail"}`` 아님.

        401 은 기본 처리 그대로다 (`require_token` 의 사유 + ``WWW-Authenticate``). 라우트 부재의
        404 도 기본 처리다 — 코드 없는 404 가 "그 표면이 없다" 는 약속이다 (#462).
        """
        problem = unroutable_problem(err)
        if problem is None:
            return await http_exception_handler(request, err)
        # 에이전트 이름은 싣지 않는다 — 라우팅 거절은 태스크의 일이 아니다 (형제 401 · 404 와 같다)
        return JSONResponse(
            status_code=err.status_code,
            content=problem.payload().model_dump(mode="json"),
            headers=err.headers,
        )

    @app.exception_handler(AaestroError)
    async def _structured(_request: Request, err: AaestroError) -> JSONResponse:
        """구조화 에러는 payload 그대로 돌려준다 — 매핑은 공용 규칙을 따른다 (#234)."""
        return _error_response(err, status_for(err))

    @app.exception_handler(Exception)
    async def _uncaught(_request: Request, err: Exception) -> JSONResponse:
        """예상 못한 예외를 INTERNAL 로 변환한다 — 데몬을 죽이지 않는다."""
        return _error_response(
            AaestroError(
                category=ErrorCategory.INTERNAL,
                code=ErrorCode.INTERNAL_001,
                message="unhandled error in control api",
                agent=runtime.agent,
                details={"cause": type(err).__name__},
            ),
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # 무인증은 이 라우트 하나뿐이다 — 검사하는 라우터 밖(앱)에 둔 것이 의도다
    @app.get(HEALTH_PATH, response_model=HealthStatus)
    async def health() -> HealthStatus:
        """상태 조회 — 무인증 (Docker healthcheck 가 직접 호출)."""
        return runtime.health()

    router = APIRouter(prefix="/v1", route_class=guarded)

    @router.post("/invoke", response_model=TaskResult)
    async def invoke(task: TaskRequest) -> TaskResult:
        """태스크를 동기 실행한다 (202+polling 아님)."""
        _reject_when_draining(runtime)
        task = _unverified(task)
        async with runtime.semaphore:
            running = asyncio.create_task(runtime.executor.execute(task))
            runtime.track(task.key, running)
            try:
                result: TaskResult = await running
            except asyncio.CancelledError:
                return TaskResult.canceled(task)
            return result

    @router.post("/stream")
    async def stream(task: TaskRequest) -> StreamingResponse:
        """태스크 이벤트를 SSE 로 스트리밍한다."""
        _reject_when_draining(runtime)
        task = _unverified(task)

        async def events() -> AsyncIterator[bytes]:
            async with runtime.semaphore:
                async for event in runtime.executor.stream(task):
                    yield f"data: {event.model_dump_json()}\n\n".encode()

        return StreamingResponse(events(), media_type=SSE_MEDIA_TYPE)

    @router.get("/card")
    async def card() -> dict[str, Any]:
        """A2A AgentCard."""
        return runtime.card()

    @router.post("/cancel/{task_id}", response_model=Acknowledgement)
    async def cancel(task_id: str) -> Acknowledgement:
        """진행 중 태스크를 취소한다."""
        if not runtime.cancel(task_id):
            raise AaestroError(
                category=ErrorCategory.NOT_FOUND,
                code=ErrorCode.NF_001,
                message=f"no in-flight task: {task_id}",
                agent=runtime.agent,
                task_id=task_id,
            )
        return Acknowledgement(status="canceled")

    @router.post(
        "/steer/{task_id}",
        response_model=Acknowledgement,
        responses={
            status.HTTP_501_NOT_IMPLEMENTED: {
                "model": Acknowledgement,
                "description": "이 실행기는 steer 를 지원하지 않는다",
            }
        },
    )
    async def steer(task_id: str, body: SteerRequest) -> Acknowledgement | JSONResponse:
        """진행 중 태스크의 다음 턴 경계에 운영자 지시를 대기시킨다.

        ``queued`` 는 **접수**다 — 반영은 다음 턴 경계에서 일어나고(``SteerEvent``), 태스크가
        먼저 끝나면 일어나지 않는다. 끝났거나 모르는 태스크는 404.

        steer 를 모르는 실행기(커스텀 entrypoint, echo)는 501 로 답한다 — 200 으로 삼키면
        운영자가 방향이 바뀐 줄 안다. 본문은 `/reload` 와 같은 ``unsupported`` 다.
        """
        if not await runtime.steer(task_id, body.text, by=body.by):
            return JSONResponse(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                content=Acknowledgement(status="unsupported").model_dump(),
            )
        return Acknowledgement(status="queued")

    @router.post("/reload", response_model=Acknowledgement)
    async def reload() -> Acknowledgement:
        """promptset/skillset 을 리로드한다 (신규 태스크부터 적용).

        리로드할 모듈이 없는 실행기(커스텀 실행기, echo)는 ``unsupported`` 로 답한다 —
        아무것도 하지 않고 ``reloaded`` 로 답하면 호출자가 반영된 줄 안다 (#274).
        """
        reloaded = await runtime.reload()
        return Acknowledgement(status="reloaded" if reloaded else "unsupported")

    @router.post("/drain", response_model=Acknowledgement)
    async def drain() -> Acknowledgement:
        """새 태스크 수락을 멈추고 진행 중 태스크를 기다린다."""
        await runtime.drain()
        return Acknowledgement(status="drained")

    well_known = APIRouter(route_class=guarded)

    @well_known.get(AGENT_CARD_WELL_KNOWN_PATH)
    async def well_known_card() -> dict[str, Any]:
        """A2A well-known AgentCard.

        ``/v1/card`` 와 **같은 소스**를 돌려준다 — 두 곳을 따로 만들면 peer 가
        보는 계약이 조용히 갈라진다 (03 AgentCard 1).
        """
        return runtime.card()

    app.include_router(router)
    app.include_router(well_known)
    return app


def _reject_when_draining(runtime: AgentRuntime) -> None:
    """Drain 중에는 새 태스크를 받지 않는다."""
    if runtime.draining:
        raise AaestroError(
            category=ErrorCategory.RUNTIME,
            code=ErrorCode.RT_005,
            message="agent is draining and does not accept new tasks",
            agent=runtime.agent,
            retryable=True,
        )


def _unverified(task: TaskRequest) -> TaskRequest:
    """Control API 로 온 태스크의 호출자 주장은 버린다 — 확인된 호출자는 A2A 수신 입구만 채운다."""
    return task.model_copy(update={"caller": None}) if task.caller is not None else task


__all__ = [
    "DEFAULT_MAX_CONCURRENT_TASKS",
    "Acknowledgement",
    "AgentRuntime",
    "AaestroErrorPayload",
    "SteerRequest",
    "create_app",
    "require_token",
]
