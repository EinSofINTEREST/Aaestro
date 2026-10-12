"""A2A server side — inbound peer calls.

에이전트 컨테이너가 노출하는 A2A 수신부. **allowlist 검증이 SDK 경로 위에서
실제로 일어나는 곳**이다: caller 가 자기 이름을 주장하는 것만으로는 부족하므로
runtime 이 발급한 per-edge token 을 함께 검증한다 (03 Enforcement 이중 방어).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

import structlog
from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution.active_task import TERMINAL_TASK_STATES
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import TaskStore
from a2a.types import a2a_pb2 as pb
from a2a.utils.errors import A2AError, UnsupportedOperationError

from aaestro.core.agent import TaskConfig, TaskRequest, TaskStatus, TraceContext
from aaestro.core.errors import AaestroError
from aaestro.protocols.a2a.errors import depth_exceeded, not_allowed
from aaestro.protocols.a2a.sdk import CALLER_HEADER, TOKEN_HEADER

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping

    from aaestro.access.client import AccessClient
    from aaestro.core.agent import TaskResult
    from aaestro.protocols.a2a.client import A2AServer

    InboundHandler = Callable[[TaskRequest], Awaitable[TaskResult]]

_STATE_OF = {
    TaskStatus.COMPLETED: pb.TaskState.TASK_STATE_COMPLETED,
    TaskStatus.FAILED: pb.TaskState.TASK_STATE_FAILED,
    TaskStatus.CANCELED: pb.TaskState.TASK_STATE_CANCELED,
}
"""내부 상태 → SDK enum. ``sdk.STATE_NAMES`` 의 역방향이다."""

DEFAULT_MAX_DEPTH = 3
"""위임 체인 깊이 상한 — 초과 시 ``A2A_005`` (03 Rule 5)."""

log = structlog.get_logger(__name__)


VERIFIED_CALLER_STATE = "aaestro_verified_caller"
"""요청 입구가 확인한 호출자를 실행기에 넘기는 자리 — SDK 의 호출 컨텍스트 상태."""


def _verified_caller(context: Any) -> str | None:
    state = getattr(context, "state", None) or {}
    found = state.get(VERIFIED_CALLER_STATE)
    return found if isinstance(found, str) else None


def headers_of(context: Any) -> Mapping[str, str]:
    """수신 컨텍스트에서 헤더를 꺼낸다 — SDK 가 ``state`` 에 실어 준다."""
    state = getattr(context, "state", None) or {}
    headers = state.get("headers") or {}
    return {str(key).lower(): str(value) for key, value in headers.items()}


def read_task(text: str) -> TaskRequest:
    """peer 가 보낸 본문을 ``TaskRequest`` 로 되돌린다.

    본문은 **untrusted input** 이다 — 계약에 맞지 않으면 그대로 거절한다.
    """
    payload = json.loads(text)
    trace = TraceContext.model_validate(payload.get("trace") or {"trace_id": "unknown"})
    return TaskRequest(
        task_id=str(payload["task_id"]),
        run_id=str(payload["run_id"]),
        node_id=payload.get("node_id"),
        input=dict(payload.get("input") or {}),
        config=TaskConfig(),
        trace=trace,
    )


@dataclass
class InboundGuard:
    """Verifies inbound peer calls before they reach the executor.

    수신 호출을 실행 앞단에서 검증합니다. 검증을 통과하지 못한 호출은
    **에이전트 코드에 닿지 않습니다**.
    """

    server: A2AServer
    max_depth: int = DEFAULT_MAX_DEPTH
    verifier: AccessClient | None = None
    """레지스트리 모드 — 있으면 호출마다 받은 표를 **자기 신원으로** 레지스트리에 확인한다.
    per-edge token 은 그래프의 모든 에이전트가 같은 서명 키로 만들 수 있어 경계가 아니다."""

    def check_task(
        self, headers: Mapping[str, str], task: TaskRequest, verified: str | None = None
    ) -> str:
        """What the executor checks once the body is decoded — returns the verified caller.

        레지스트리 모드에서는 표를 이미 요청 입구에서 확인했다(``admit`` → ``verified``) — 여기서는
        깊이만 본다. 입구를 거치지 않은 레지스트리 모드 호출은 확인된 호출자가 없으니 거부한다.
        """
        if self.verifier is None:
            return self.check(headers, task)
        if verified is None:
            raise not_allowed(
                "unknown", self.server.agent, owner=self.server.agent, reason="no ticket"
            )
        self._check_depth(verified, task)
        return verified

    async def admit(self, headers: Mapping[str, str]) -> str:
        """Authorize one inbound call by its credential — before the SDK starts the task.

        SDK 가 태스크 실행을 시작한 뒤에 await 하고 거부하면 호출자는 거부 사유 대신 끝나지 않은
        스트림을 본다 — 그래서 레지스트리 확인은 실행 전에 한다. per-edge token 모드도 여기서
        가른다: 실행기에서야 거부하면 SDK 가 자격 없는 요청마다 태스크를 만들고 본문을 쥔다 (#414).

        Returns:
            The verified caller name.

        Raises:
            AaestroError: A2A/``A2A_004`` if the call is not allowed or cannot be decided.
        """
        if self.verifier is None:
            return self._token_caller(headers)
        from aaestro.protocols.a2a.tickets import TICKET_HEADER

        claimed = headers.get(CALLER_HEADER, "") or "unknown"
        ticket = headers.get(TICKET_HEADER, "")
        if not ticket:
            raise not_allowed(
                claimed, self.server.agent, owner=self.server.agent, reason="no ticket"
            )
        verdict = await self.verifier.verify_ticket(ticket)
        if not verdict.allowed or verdict.agent is None:
            log.info(
                "a2a inbound denied by registry",
                a2a_caller=claimed,
                a2a_callee=self.server.agent,
                decision="deny",
                decided_by=verdict.decided_by,
                decision_source=verdict.source.value,
            )
            raise not_allowed(
                claimed, self.server.agent, owner=self.server.agent, reason=verdict.decided_by
            )
        if claimed not in ("unknown", verdict.agent):
            # 표는 다른 호출자의 것이다 — 이름 주장이 표와 어긋나면 누구의 호출인지 믿을 수 없다
            raise not_allowed(
                claimed, self.server.agent, owner=self.server.agent, reason="caller mismatch"
            )
        return verdict.agent

    def _check_depth(self, caller: str, task: TaskRequest) -> None:
        # 깊이 검사는 **수신 측에서도** 한다: caller 가 자기 depth 를 정직하게
        # 실었다고 믿으면 순환 위임을 caller 하나가 뚫을 수 있다
        if task.trace.depth > self.max_depth:
            raise depth_exceeded(
                caller, self.server.agent, depth=task.trace.depth, limit=self.max_depth
            )

    def check(self, headers: Mapping[str, str], task: TaskRequest) -> str:
        """Authorize one inbound call.

        수신 호출 하나를 검증합니다.

        Args:
            headers: Inbound headers — caller 이름과 per-edge token 을 담습니다.
            task: The decoded task — its trace carries the delegation depth.

        Returns:
            The verified caller name.

        Raises:
            AaestroError: A2A/``A2A_004`` if the caller is undeclared or the
                token does not match, ``A2A_005`` if the chain is too deep.
        """
        caller = self._token_caller(headers)
        self._check_depth(caller, task)

        log.debug(
            "a2a inbound authorized",
            a2a_caller=caller,
            a2a_callee=self.server.agent,
            a2a_task_id=task.task_id,
        )
        return caller

    def _token_caller(self, headers: Mapping[str, str]) -> str:
        """per-edge token 으로 확인한 호출자 — 입구(``admit``)와 실행기(``check``)의 같은 판정."""
        caller = headers.get(CALLER_HEADER, "")
        token = headers.get(TOKEN_HEADER, "")
        if not caller or not token:
            # 헤더가 없으면 방향을 확인할 방법이 없다 — 이름만 주장하는 호출과 같다
            raise not_allowed(caller or "unknown", self.server.agent)
        self.server.authorize(caller, token)
        return caller


class GuardedExecutor(AgentExecutor):
    """The SDK-side executor that runs one inbound peer task.

    SDK 경로 위에 검증을 얹습니다 — allowlist·token·depth 를 통과하지 못한
    호출은 에이전트 코드에 닿지 않습니다.
    """

    def __init__(self, guard: InboundGuard, invoke: InboundHandler) -> None:
        self._guard = guard
        self._invoke = invoke

    async def execute(self, context: Any, event_queue: Any) -> None:
        """수신 태스크 하나를 검증하고 실행한다."""
        task = read_task(context.get_user_input())
        call = context.call_context
        caller = self._guard.check_task(headers_of(call), task, _verified_caller(call))
        # 확인된 호출자를 태스크에 싣는다 — 본문의 주장이 아니라 입구가 확인한 이름이다
        task = task.model_copy(update={"caller": caller})

        result = await self._invoke(task)
        await event_queue.enqueue_event(
            pb.Task(
                # id 는 **서버가 정한 것**을 쓴다 — 우리 내부 id 로 답하면
                # TaskManager 가 다른 task 의 이벤트로 보고 거절한다
                id=context.task_id,
                context_id=context.context_id,
                status=pb.TaskStatus(state=_STATE_OF[result.status]),
                artifacts=[pb.Artifact(parts=[pb.Part(text=encode_result(result))])],
            )
        )

    async def cancel(self, context: Any, event_queue: Any) -> None:
        """취소는 상위 계층이 관리한다 — 여기서는 no-op."""


class RunningTaskStore(TaskStore):
    """Holds a task only while it runs — a finished task is dropped instead of stored.

    조회·목록·구독·취소와 기존 태스크에 잇는 메시지를 전부 거절하므로 끝난 태스크를 다시 읽는
    경로가 없다. SDK 기본 store 는 그것을 지우지 않아 요청 수만큼 쌓인다 (#414, 02 Statelessness).
    SDK 는 실행 중에 자기 사본으로 일하고 store 를 다시 읽지 않는다 — 종결 상태를 저장하지 않아도
    응답은 같다. 응답 뒤에 지우는 방식은 호출자가 끊은 뒤 끝난 태스크를 놓친다.
    """

    def __init__(self) -> None:
        self._running: dict[str, pb.Task] = {}

    def __len__(self) -> int:
        return len(self._running)

    async def save(self, task: pb.Task, context: Any) -> None:
        if task.status.state in TERMINAL_TASK_STATES:
            self._running.pop(task.id, None)
            return
        self._running[task.id] = _copy_of(task)

    async def get(self, task_id: str, context: Any) -> pb.Task | None:
        held = self._running.get(task_id)
        return None if held is None else _copy_of(held)

    async def list(self, params: Any, context: Any) -> NoReturn:
        raise UnsupportedOperationError

    async def delete(self, task_id: str, context: Any) -> None:
        self._running.pop(task_id, None)


def _copy_of(task: pb.Task) -> pb.Task:
    # SDK 는 저장한 뒤에도 같은 객체를 고친다 — 사본이 아니면 종결 상태가 store 로 새어 들어온다
    copy = pb.Task()
    copy.CopyFrom(task)
    return copy


class GuardedRequestHandler(DefaultRequestHandler):
    """The SDK request handler — only new-task sends are admitted, everything else is refused.

    SDK 핸들러는 태스크 조회·목록·구독·취소·push 설정을 **호출자 확인 없이** 연다 (#414).
    프레임워크 클라이언트는 새 태스크를 보내는 두 요청만 쓰므로 그 둘만 검사를 거쳐 열고,
    나머지는 전부 거절합니다 — 태스크 저장소에 닿기 전에, 누가 물었든 같은 응답으로.
    SDK 의 ``on_*`` 는 **전부 여기서 재정의**합니다: 재정의가 빠진 메서드는 SDK 기본 구현으로
    열립니다 (구조 테스트가 전수 대조한다).

    태스크 store 는 주입받지 않습니다 — 읽는 창구를 닫았으므로 끝난 태스크를 쥐는 store 를
    끼우면 쌓이기만 합니다 (``RunningTaskStore``).
    """

    def __init__(self, *, guard: InboundGuard, **kwargs: Any) -> None:
        super().__init__(task_store=RunningTaskStore(), **kwargs)
        self._guard = guard

    async def on_message_send(self, params: Any, context: Any) -> Any:
        await self._admit(params, context)
        return await super().on_message_send(params, context)

    async def on_message_send_stream(self, params: Any, context: Any) -> AsyncGenerator[Any]:
        await self._admit(params, context)
        async for event in super().on_message_send_stream(params, context):
            yield event

    async def on_get_task(self, params: Any, context: Any) -> NoReturn:
        raise self._refused("get_task")

    async def on_list_tasks(self, params: Any, context: Any) -> NoReturn:
        raise self._refused("list_tasks")

    async def on_cancel_task(self, params: Any, context: Any) -> NoReturn:
        raise self._refused("cancel_task")

    async def on_subscribe_to_task(self, params: Any, context: Any) -> AsyncGenerator[Any]:
        raise self._refused("subscribe_to_task")
        # SDK 는 스트림의 첫 이벤트를 당겨 실행 전 오류를 받는다 — async generator 여야 한다
        yield

    async def on_create_task_push_notification_config(self, params: Any, context: Any) -> NoReturn:
        raise self._refused("create_task_push_notification_config")

    async def on_get_task_push_notification_config(self, params: Any, context: Any) -> NoReturn:
        raise self._refused("get_task_push_notification_config")

    async def on_list_task_push_notification_configs(self, params: Any, context: Any) -> NoReturn:
        raise self._refused("list_task_push_notification_configs")

    async def on_delete_task_push_notification_config(self, params: Any, context: Any) -> NoReturn:
        raise self._refused("delete_task_push_notification_config")

    async def on_get_extended_agent_card(self, params: Any, context: Any) -> NoReturn:
        raise self._refused("get_extended_agent_card")

    def _refused(self, operation: str) -> A2AError:
        """The one answer every unopened request gets.

        응답은 태스크가 있든 없든, 자격이 있든 없든 같다 — 다르면 그 차이가 존재 여부를 알려 준다.
        요청이 보낸 값(task id · 헤더 주장)은 메시지에도 로그에도 싣지 않는다.
        """
        # 호출자를 확인하지 않고 거절하므로 a2a_caller 는 없다 — 헤더의 주장을 적으면
        # 위조된 이름이 로그에 남는다
        log.info("a2a request refused", a2a_callee=self._guard.server.agent, operation=operation)
        return UnsupportedOperationError()

    async def _admit(self, params: Any, context: Any) -> None:
        """거부를 SDK 오류로 옮긴다 — 실행 전 오류는 SDK 가 오류 응답으로만 실어 보낸다.

        사유(``a2a:A2A_004`` 표기)를 메시지에 남겨야 호출자가 설정 문제와 peer 실패를 가른다.
        """
        if params.message.task_id:
            # 기존 태스크에 잇는 메시지는 SDK 가 호출자 확인 전에 그 태스크를 찾아 "없음" 과
            # "이미 끝남" 을 다르게 답한다 — 조회와 같은 창구다. 새 위임은 늘 새 태스크다
            raise self._refused("message_send_to_existing_task")
        try:
            caller = await self._guard.admit(headers_of(context))
        except AaestroError as err:
            raise A2AError(message=str(err)) from err
        state = getattr(context, "state", None)
        if state is not None:
            state[VERIFIED_CALLER_STATE] = caller


def encode_result(result: TaskResult) -> str:
    """결과를 peer 가 읽을 본문으로 — ``read_output`` 의 짝.

    실패면 구조화 에러를 싣는다 — 상태만 보내면 호출자는 "실패했다" 만 알고 **왜**(예: 권한
    에이전트의 ``ACC_003`` 거절)를 모른다. 에러 payload 는 호출자에게 보여도 되는 계약 표현이다.
    """
    if result.error is not None:
        return json.dumps({"error": result.error.model_dump(mode="json")}, ensure_ascii=False)
    return json.dumps(result.output, ensure_ascii=False)


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "GuardedExecutor",
    "GuardedRequestHandler",
    "InboundGuard",
    "RunningTaskStore",
    "encode_result",
    "headers_of",
    "read_task",
]
