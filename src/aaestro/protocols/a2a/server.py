"""A2A server side — inbound peer calls.

에이전트 컨테이너가 노출하는 A2A 수신부. **allowlist 검증이 SDK 경로 위에서
실제로 일어나는 곳**이다: caller 가 자기 이름을 주장하는 것만으로는 부족하므로
runtime 이 발급한 per-edge token 을 함께 검증한다 (03 Enforcement 이중 방어).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import a2a_pb2 as pb
from a2a.utils.errors import A2AError

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

    async def admit(self, headers: Mapping[str, str]) -> str | None:
        """Authorize one inbound call by its ticket — before the SDK starts the task.

        SDK 가 태스크 실행을 시작한 뒤에 await 하고 거부하면 호출자는 거부 사유 대신 끝나지 않은
        스트림을 본다 — 그래서 레지스트리 확인은 실행 전에 한다.

        Returns:
            The verified caller name, or None when no verifier is wired (per-edge token mode).

        Raises:
            AaestroError: A2A/``A2A_004`` if the call is not allowed or cannot be decided.
        """
        if self.verifier is None:
            return None
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
        caller = headers.get(CALLER_HEADER, "")
        token = headers.get(TOKEN_HEADER, "")
        if not caller or not token:
            # 헤더가 없으면 방향을 확인할 방법이 없다 — 이름만 주장하는 호출과 같다
            raise not_allowed(caller or "unknown", self.server.agent)

        self.server.authorize(caller, token)
        self._check_depth(caller, task)

        log.debug(
            "a2a inbound authorized",
            a2a_caller=caller,
            a2a_callee=self.server.agent,
            a2a_task_id=task.task_id,
        )
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


class GuardedRequestHandler(DefaultRequestHandler):
    """The SDK request handler with the registry check in front of task execution."""

    def __init__(self, *, guard: InboundGuard, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._guard = guard

    async def on_message_send(self, params: Any, context: Any) -> Any:
        await self._admit(context)
        return await super().on_message_send(params, context)

    async def on_message_send_stream(self, params: Any, context: Any) -> AsyncGenerator[Any]:
        await self._admit(context)
        async for event in super().on_message_send_stream(params, context):
            yield event

    async def _admit(self, context: Any) -> None:
        """거부를 SDK 오류로 옮긴다 — 실행 전 오류는 SDK 가 오류 응답으로만 실어 보낸다.

        사유(``a2a:A2A_004`` 표기)를 메시지에 남겨야 호출자가 설정 문제와 peer 실패를 가른다.
        """
        try:
            caller = await self._guard.admit(headers_of(context))
        except AaestroError as err:
            raise A2AError(message=str(err)) from err
        state = getattr(context, "state", None)
        if caller is not None and state is not None:
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
    "encode_result",
    "headers_of",
    "read_task",
]
