"""Agent Control API server.

agentd 가 컨테이너 내부에서 서빙하는 표준 API. Runtime layer 외에는 직접
호출하지 않는다.

태스크 실패는 데몬을 죽이지 않는다 — 최상위 핸들러가 uncaught exception 을
``INTERNAL`` 로 변환해 구조화 응답으로 돌려준다.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from fastapi import APIRouter, FastAPI, Request, Response, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

from aaestro.core.agent import (
    HealthState,
    HealthStatus,
    TaskKey,
    TaskRequest,
    TaskResult,
    TaskStatus,
)
from aaestro.core.errors import AaestroError, AaestroErrorPayload, ErrorCategory, ErrorCode
from aaestro.core.events import DoneEvent, ErrorEvent, TaskEvent, ToolCallEvent, ToolResultEvent
from aaestro.core.identifiers import identifier_problem
from aaestro.core.tools import is_mcp_tool
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
        # 같은 키로 겹친 태스크는 등록 순서대로 쌓인다 — 취소는 전부에 닿고 (겹친 invoke 는 한
        # 실행을 기다린다, #416), 끝난 태스크는 자기 자리만 빠진다 (먼저 끝난 쪽이 남의 자리를
        # 지우지 않게)
        self._inflight: dict[TaskKey, list[asyncio.Task[Any]]] = {}

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
        self._inflight.setdefault(key, []).append(task)

        def forget(_done: asyncio.Task[Any]) -> None:
            tasks = self._inflight.get(key, [])
            if task in tasks:
                tasks.remove(task)
            if not tasks:
                self._inflight.pop(key, None)

        task.add_done_callback(forget)

    def cancel(self, task_id: str) -> bool:
        """Control API 로 들어온 진행 중 태스크를 취소한다. 없으면 False.

        취소를 부를 수 있는 것은 Control API 뿐이므로 대상도 그쪽 태스크뿐이다 — peer 가 만든
        동명 태스크는 이 id 로 가리켜지지 않는다.
        """
        tasks = self._inflight.get(TaskKey.from_control(task_id))
        if not tasks:
            return False
        for task in list(tasks):
            task.cancel()
        return True

    async def run(self, task: TaskRequest) -> TaskResult:
        """Run a task inside the shared concurrency cap, tracked for cancel and drain.

        동시 실행 상한 안에서 추적된 채 태스크를 실행합니다. Control API 의 ``/invoke`` 와 A2A
        수신 입구가 같이 쓴다 — 같은 큐 (02 Direct Request Rules 5). drain 중이면 실행하지 않고
        ``RT_005`` (retryable) 실패로 답한다: A2A 는 실패를 결과로 돌려준다.
        """
        if self.draining:
            return TaskResult.failed(task, _draining_error(self))
        async with self.semaphore:
            running = asyncio.create_task(self.executor.execute(task))
            self.track(task.key, running)
            try:
                result: TaskResult = await running
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise  # 호출자가 떠났다 — 끊긴 것은 이쪽이다
                return TaskResult.canceled(task)
            return result

    async def steer(self, task_id: str, text: str, *, by: str) -> bool:
        """진행 중 태스크에 운영자 지시를 대기시킨다 — steer 를 모르는 실행기면 False.

        진행 여부는 실행기가 안다: 서버의 추적표는 대기 중인 호출 · 스트림 생산자를 쥐지, 실행기의
        steer 창구가 열렸는지는 모른다.
        """
        steer = getattr(self.executor, "steer", None)
        if not callable(steer):
            return False
        await steer(task_id, text, by=by)
        return True

    async def drain(self) -> None:
        """새 태스크 수락을 멈추고 진행 중 태스크를 기다린다."""
        self.draining = True
        inflight = [task for tasks in self._inflight.values() for task in tasks]
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)


def _error_response(err: AaestroError, http_status: int) -> JSONResponse:
    """구조화 에러를 JSON 응답으로 만든다."""
    return JSONResponse(status_code=http_status, content=err.payload().model_dump(mode="json"))


log = structlog.get_logger(__name__)


_FALLBACK_ERROR_FRAME = (
    b'data: {"type":"error","task_id":"","error":{"category":"internal","code":"INTERNAL_001",'
    b'"message":"event not serializable","retryable":false,"details":{}}}\n\n'
)
"""대체 이벤트마저 직렬화에 실패할 때 — 사용자 문자열이 하나도 없는 고정 프레임."""


def _printable(text: str) -> str:
    """고립 surrogate 처럼 UTF-8 로 쓸 수 없는 글자를 치환한다 — 대체 이벤트가 다시 터지지 않게."""
    return text.encode("utf-8", "replace").decode()


def _frame(event: TaskEvent) -> bytes:
    return f"data: {event.model_dump_json()}\n\n".encode()


def _replacement(event: TaskEvent, agent: str) -> TaskEvent | None:
    """직렬화에 실패한 이벤트의 대체 — 종류별로 스트림의 뜻을 바꾸지 않는 모양을 고른다.

    ``None`` 은 건너뛴다는 뜻이다. ``ErrorEvent`` 는 종료 이벤트이므로
    종료 이벤트가 깨졌을 때만 쓴다.
    """
    task_id = _printable(event.task_id)
    update: dict[str, Any]
    if isinstance(event, ToolResultEvent):
        tool = _printable(event.tool)
        is_mcp = is_mcp_tool(tool)
        failure = AaestroError(
            category=ErrorCategory.MCP if is_mcp else ErrorCategory.MODULE,
            code=ErrorCode.MCP_003 if is_mcp else ErrorCode.SKILL_001,
            message=f"tool result not serializable: {tool}",
            agent=agent,
            task_id=task_id,
            details={"tool": tool},
        ).payload()
        update = {
            "task_id": task_id,
            "tool": tool,
            "result": None,
            "error": failure,
        }
        return event.model_copy(update=update)
    if isinstance(event, ToolCallEvent):
        update = {
            "task_id": task_id,
            "tool": _printable(event.tool),
            "arguments": {},
        }
        return event.model_copy(update=update)
    if isinstance(event, DoneEvent | ErrorEvent):
        failure = AaestroError(
            category=ErrorCategory.INTERNAL,
            code=ErrorCode.INTERNAL_001,
            message="event not serializable",
            agent=agent,
            task_id=task_id,
        ).payload()
        return ErrorEvent(task_id=task_id, error=failure, usage=event.usage)
    return None


def _sse_frame(event: TaskEvent, agent: str) -> bytes:
    """이벤트 하나를 SSE 프레임으로 — 직렬화 실패가 스트림을 끊지 않게 한다.

    JSON 왕복을 통과한 값도 깊은 중첩 · 고립 surrogate 처럼 pydantic 직렬화에서 터질 수 있다.
    값마다 막지 않고 부류를 막는다: 실패한 이벤트는 종류별 대체 (`_replacement`) 로 보낸다.
    건너뛴 이벤트는 빈 바이트다.
    """
    try:
        return _frame(event)
    except Exception as err:
        # 값은 로그에 싣지 않는다 — 도구 결과는 신뢰하지 않는 데이터다
        log.warning(
            "event not serializable",
            agent=agent,
            task_id=_printable(event.task_id),
            tool=_printable(getattr(event, "tool", "")),
            error_type=type(err).__name__,
        )
    try:
        replaced = _replacement(event, agent)
        return _frame(replaced) if replaced is not None else b""
    except Exception:
        log.warning("replacement event not serializable", agent=agent)
        # 비종료 이벤트 뒤에 error 를 보내면 실패를 알린 뒤에도 스트림이 이어진다
        return _FALLBACK_ERROR_FRAME if isinstance(event, DoneEvent | ErrorEvent) else b""


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
    # A2A 수신 입구가 같은 큐를 쓰도록 runtime 을 내보인다 (agentd ``_run``, #416)
    app.state.runtime = runtime
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
        _reject_bad_identifiers(task, runtime.agent)
        return await runtime.run(task)

    @router.post("/stream")
    async def stream(task: TaskRequest) -> StreamingResponse:
        """태스크 이벤트를 SSE 로 스트리밍한다."""
        _reject_when_draining(runtime)
        task = _unverified(task)
        _reject_bad_identifiers(task, runtime.agent)

        async def events() -> AsyncIterator[bytes]:
            async with runtime.semaphore:
                # 실행기 스트림은 태스크 하나가 끝까지 돈다 — 그것을 추적해야 cancel · drain 이
                # 스트림 전체를 보고, 응답은 취소된 실행을 종료 이벤트로 닫을 수 있다 (#395).
                # 한 태스크라 생성기 안의 ContextVar · asyncio.timeout 이 이벤트를 넘어 유지된다
                relay: asyncio.Queue[TaskEvent] = asyncio.Queue(maxsize=1)
                # 실행기가 만든 종료 이벤트 — 큐에 넣기 전에 남긴다: 그 뒤의 취소는 완료를 이기지
                # 못한다 (정리 중 · 느린 소비자로 put 에 막힌 중 온 cancel 이 결과를 지우지 않게)
                terminal: list[TaskEvent] = []

                async def produce() -> None:
                    source = runtime.executor.stream(task)
                    try:
                        async for produced in source:
                            # 응답은 첫 종료 이벤트만 쓴다 — 뒤의 것은 쌓지 않는다 (#639)
                            if isinstance(produced, DoneEvent | ErrorEvent) and not terminal:
                                terminal.append(produced)
                            await relay.put(produced)
                    finally:
                        # put 에서 끊기면 생성기는 yield 에 멈춘 채 남는다 — 닫아야 실행기의 정리
                        # (steer 창구 등) 가 GC 를 기다리지 않는다. aclose 없는 반복자도 받는다
                        close = getattr(source, "aclose", None)
                        if close is not None:
                            try:
                                await close()
                            except RuntimeError:
                                # GeneratorExit 을 무시한 생성기 (규약 위반) 만 프레임이 남은 채
                                # 실행 중이 아니다 (yield 에 멈춤) — 그때만 기록하고 넘긴다. 끝난
                                # 생성기의 정리 오류 · 'already running' 은 그대로 올린다 (#639)
                                if getattr(source, "ag_frame", None) is None or getattr(
                                    source, "ag_running", True
                                ):
                                    raise
                                log.warning(
                                    "executor stream ignored close",
                                    agent=runtime.agent,
                                    task_id=task.task_id,
                                )

                def reap(finished: asyncio.Task[None]) -> None:
                    # 응답이 거두지 않은 실패를 기록한다 — 안 읽으면 "never retrieved" 로 샌다
                    if not finished.cancelled() and (err := finished.exception()) is not None:
                        log.warning(
                            "executor stream failed while closing",
                            agent=runtime.agent,
                            task_id=task.task_id,
                            exc_info=err,
                        )

                producer = asyncio.create_task(produce())
                runtime.track(task.key, producer)
                ended = False  # 종료 이벤트를 이미 냈다 — 응답은 종료 이벤트를 하나만 낸다
                collected = False  # producer 의 예외를 응답이 이미 거뒀다
                dropped = False  # 종료 뒤 이벤트를 버린 사실을 이미 남겼다
                try:
                    while True:
                        getting = asyncio.ensure_future(relay.get())
                        try:
                            await asyncio.wait(
                                {getting, producer}, return_when=asyncio.FIRST_COMPLETED
                            )
                        finally:
                            getting.cancel()
                        if getting.done() and not getting.cancelled():
                            event = getting.result()
                        elif not relay.empty():
                            event = relay.get_nowait()
                        elif producer.cancelled():
                            if ended:
                                return
                            # canceled 합성은 실행기가 종료 이벤트를 만들기 전의 취소에만
                            event = (
                                terminal[0]
                                if terminal
                                else DoneEvent(task_id=task.task_id, status=TaskStatus.CANCELED)
                            )
                            if frame := _sse_frame(event, runtime.agent):
                                yield frame
                            return
                        else:
                            collected = True
                            if not ended:
                                producer.result()  # 실행기의 예외는 응답이 거둔다
                            elif (err := producer.exception()) is not None:
                                # 종료 이벤트를 이미 냈다 — 끝난 응답을 예외로 깨지 않는다 (#639)
                                log.warning(
                                    "executor stream failed after its end",
                                    agent=runtime.agent,
                                    task_id=task.task_id,
                                    exc_info=err,
                                )
                            return
                        if ended:
                            # 종료 이벤트는 하나다 — 그 뒤에 실행기가 더 낸 이벤트는 버린다 (#639)
                            if not dropped:
                                dropped = True
                                log.warning(
                                    "executor stream event after its end dropped",
                                    agent=runtime.agent,
                                    task_id=task.task_id,
                                )
                            continue
                        if isinstance(event, DoneEvent | ErrorEvent):
                            ended = True
                        if frame := _sse_frame(event, runtime.agent):
                            yield frame
                finally:
                    # 소비자가 떠나거나 응답이 끝나면 실행을 끊고 정리가 끝날 때까지 기다린다.
                    # 예외를 이미 거둔 producer 는 다시 다루지 않는다 — 실패를 두 번 남기지 않게
                    if not collected:
                        # 끊는 것은 한 번뿐이다 (/cancel 이 먼저 끊었으면 그것으로). 연결이 끊기면
                        # anyio 가 응답을 거듭 취소한다 — wait 는 그 취소를 producer 에 넘기지 않아
                        # 실행기 finally 의 await 가 잘리지 않는다 (#639)
                        if not producer.done() and not producer.cancelling():
                            producer.cancel()
                        try:
                            await asyncio.wait({producer})
                        except asyncio.CancelledError:
                            # 응답이 먼저 떠난다 — 정리는 추적된 채 끝까지 돌고 결과는 콜백이 거둔다
                            producer.add_done_callback(reap)
                            raise
                        reap(producer)

        return StreamingResponse(events(), media_type=SSE_MEDIA_TYPE)

    @router.get("/card")
    async def card() -> dict[str, Any]:
        """A2A AgentCard."""
        return runtime.card()

    @router.post("/cancel/{task_id}", response_model=Acknowledgement)
    async def cancel(task_id: str) -> Acknowledgement:
        """진행 중 태스크를 취소한다."""
        _reject_bad_identifier("task_id", task_id, runtime.agent)
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
        _reject_bad_identifier("task_id", task_id, runtime.agent)
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


def _draining_error(runtime: AgentRuntime) -> AaestroError:
    return AaestroError(
        category=ErrorCategory.RUNTIME,
        code=ErrorCode.RT_005,
        message="agent is draining and does not accept new tasks",
        agent=runtime.agent,
        retryable=True,
    )


def _reject_when_draining(runtime: AgentRuntime) -> None:
    """Drain 중에는 새 태스크를 받지 않는다."""
    if runtime.draining:
        raise _draining_error(runtime)


def _reject_bad_identifiers(task: TaskRequest, agent: str) -> None:
    """호출자가 고른 식별자를 A2A 본문과 같은 규칙으로 거른다 — 받은 값은 사유에 싣지 않는다."""
    fields = {
        "task_id": task.task_id,
        "run_id": task.run_id,
        "node_id": task.node_id,
        "trace.trace_id": task.trace.trace_id,
        "trace.span_id": task.trace.span_id,
        "trace.graph": task.trace.graph,
    }
    for name, value in fields.items():
        # node_id · span_id · graph 는 선택 필드 — 없는 것은 검사하지 않는다
        if value is not None:
            _reject_bad_identifier(name, value, agent)


def _reject_bad_identifier(name: str, value: str, agent: str) -> None:
    """식별자 하나를 거른다 — 본문 필드와 경로의 task_id 가 같은 거절 형식을 쓴다."""
    problem = identifier_problem(value)
    if problem is not None:
        raise AaestroError(
            category=ErrorCategory.VALIDATION,
            code=ErrorCode.VAL_002,
            message="task identifier is malformed",
            agent=agent,
            details={"field": name, "reason": problem.value},
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
