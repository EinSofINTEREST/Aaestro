"""Model call and tool execution loop.

agentd 의 실행 루프. 모델과 tool 사이를 오가며 태스크를 완수한다.

모델 provider 의 자체 재시도는 비활성화하고 이 계층이 단일 재시도 계층이 된다
(05 Retry Layering) — 두 계층이 겹치면 backoff 가 곱해진다.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final, Protocol, TypedDict

import structlog

from aaestro.agentd.health import ModelHealth
from aaestro.agentd.models import MODEL_RETRY_POLICIES, ModelChain, ModelLink
from aaestro.agentd.telemetry import (
    DIRECT_GRAPH,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RATE_LIMITED,
)
from aaestro.core.agent import (
    DEFAULT_MAX_TURNS,
    DEFAULT_TOOL_TIMEOUT_S,
    ModelUsage,
    SteerProblem,
    TaskKey,
    TaskRequest,
    TaskResult,
)
from aaestro.core.errors import (
    AaestroError,
    AaestroErrorPayload,
    ErrorCategory,
    ErrorCode,
)
from aaestro.core.events import (
    DoneEvent,
    ErrorEvent,
    SteerEvent,
    TaskEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from aaestro.core.identifiers import IdentifierProblem, identifier_problem
from aaestro.core.skill import SkillContext
from aaestro.core.tools import is_mcp_tool

if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
        AsyncIterator,
        Awaitable,
        Callable,
        Mapping,
        Sequence,
    )

    from aaestro.agentd.telemetry import ExecutorTelemetry
    from aaestro.core.errors import RetryPolicy
    from aaestro.core.manifest import ModelConfig
    from aaestro.core.skill import ArtifactStore

    TaskRecall = Callable[[TaskRequest], Awaitable[str]]
    """태스크 진입 시 1회 회상해 프롬프트에 붙일 텍스트를 만드는 콜러블."""

log = structlog.get_logger(__name__)


REMEMBERED_RESULTS: Final = 1024
"""멱등 캐시가 기억하는 완료 결과 수 — 넘치면 오래된 것부터 잊는다.

상주 컨테이너는 수명이 길어 상한이 없으면 모든 태스크 결과(output 포함)를 끝까지 쥔다 (#315).
재시도 창(05 NETWORK_RETRY: 최대 30s × 3)에 드는 태스크 수보다 넉넉하면 된다.
"""


MAX_PENDING_STEERS: Final = 8
"""한 태스크에 반영을 기다리는 steer 수의 상한 — 넘으면 ``VAL_002`` 로 거절한다.

턴 경계마다 비워지므로 정상 사용에서는 한두 건이다. 상한이 없으면 모델 호출 한 번이 길게
매달린 동안 대기열이 끝없이 자란다 (#315 와 같은 부류)."""

MAX_STEER_CHARS: Final = 4000
"""steer 한 건의 길이 상한 — 방향을 바꾸는 한마디이지 새 태스크 입력이 아니다."""

MAX_STEER_BY_CHARS: Final = 200
"""``by`` (감사용 신원) 의 길이 상한 — 로그와 이벤트에 그대로 실린다."""

STEER_HEADER: Final = "[operator steer]"
"""steer 가 대화에 들어갈 때 붙는 머리말 — 모델이 태스크 입력·도구 결과와 구분하게 한다.

회상 기억의 출처 표시(``[memory:…]``)와 같은 결이다. untrusted 표시는 붙이지 않는다:
steer 는 운영자 입력이다."""


def _model_status(err: BaseException) -> str:
    """실패를 05 의 status 어휘로 분류한다.

    rate limit 을 failed 로 뭉개면 ModelRateLimited 알림이 영원히 침묵한다 —
    05 status 표가 명시적으로 경고하는 상황이다.
    """
    if isinstance(err, AaestroError) and err.category is ErrorCategory.RATE_LIMIT:
        return STATUS_RATE_LIMITED
    return STATUS_FAILED


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation requested by the model.

    모델이 요청한 tool 호출.
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    """One model turn.

    모델의 한 턴. tool 호출이 없으면 태스크가 끝난 것으로 본다.
    """

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: ModelUsage = field(default_factory=ModelUsage)
    raw: Any = None
    """provider 가 돌려준 assistant 내용 그대로 — 다음 턴에 **바꾸지 않고** 되돌려 보낸다.

    텍스트와 도구 호출만 다시 조립하면 provider 고유 블록(thinking 등)이 빠져, 도구를 부른 턴을
    이어갈 때 provider 가 거부하거나 추론 맥락을 잃는다."""

    @property
    def is_final(self) -> bool:
        """더 이상 tool 을 부르지 않는 최종 응답인지."""
        return not self.tool_calls


@dataclass(frozen=True)
class UserTurn:
    """What the caller says — 태스크 입력, 그리고 그와 **따로 표시된** 회상 기억."""

    parts: tuple[str, ...]


@dataclass(frozen=True)
class AssistantTurn:
    """What the model said — 텍스트와 도구 호출, provider 원본."""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    raw: Any = None


@dataclass(frozen=True)
class ToolOutcome:
    """One tool's answer, bound to the call that asked for it."""

    call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class ToolTurn:
    """Every tool answer of one assistant turn — 한 메시지에 모아 돌려준다 (병렬 호출)."""

    outcomes: tuple[ToolOutcome, ...]


Message = UserTurn | AssistantTurn | ToolTurn
"""대화 한 칸. 역할이 번갈아 쌓인다: user → assistant → tool → assistant → …

운영자 steer 가 있으면 tool 뒤에 user 가 한 번 더 온다: … → tool → user(steer) → assistant → …"""


@dataclass(frozen=True)
class Steer:
    """An operator instruction waiting for the next turn boundary.

    턴 경계를 기다리는 운영자 지시 한 건. ``by`` 는 감사용이다 — 모델에는 가지 않는다.
    """

    text: str
    by: str


@dataclass
class _SteerQueue:
    """실행 하나의 steer 대기열 — 어느 run 의 태스크인지와 함께 쥔다 (로그의 ``run_id``)."""

    run_id: str
    pending: list[Steer] = field(default_factory=list)


class Model(Protocol):
    """Model provider contract.

    모델 provider 계약. 실제 SDK 는 이 뒤에 감춰지고, 테스트는 FakeModel 로 대체한다.

    ``system`` 은 운영자 지시(promptset 의 ``system`` 템플릿)이고 ``messages`` 는 태스크
    입력·도구 결과가 쌓이는 대화다. 둘을 한 문자열로 합치면 도구 결과(신뢰하지 않는 입력)와
    시스템 지시가 같은 자리에 놓여, 03/09 의 "도구 결과·기억 속 지시를 시스템 지시로 승격하지
    않는다" 경계가 모델 입력에서 사라진다 (#308).
    """

    async def run(
        self, system: str, messages: Sequence[Message], tools: Sequence[Any]
    ) -> ModelResponse:
        """시스템 지시와 대화, tool 목록으로 한 턴을 실행한다."""
        ...


class ToolRegistry(Protocol):
    """Resolves a tool name to its callable.

    tool 이름을 실행 가능한 함수로 해석하는 계약. skillset tool 과 MCP tool 을
    한 곳에서 조회하되, 실패 시 출처에 맞는 에러 코드로 변환할 수 있어야 한다.
    """

    def timeout_for(self, name: str) -> float:
        """해당 tool 의 실행 상한."""
        ...

    async def call(self, name: str, arguments: Mapping[str, Any], ctx: SkillContext) -> Any:
        """tool 을 실행한다."""
        ...


@dataclass
class ExecutorConfig:
    """How the loop behaves — limits and retry policy.

    루프의 행동 규칙. 태스크별 값이 있으면 그쪽이 우선한다.

    재시도가 여기 있는 이유: 05 Retry Layering 은 모델 호출의 재시도 주체를
    agentd 로 규정한다. 즉 재시도는 이 루프의 **행동**이지 협력자가 아니다.
    `retry_sleep` 은 그 행동을 테스트에서 실제로 자지 않고 검증하기 위한
    이음매라 정책과 함께 움직인다 (06 Async 2).
    """

    max_turns: int = DEFAULT_MAX_TURNS
    tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S
    retry_policies: tuple[RetryPolicy, ...] = ()
    """비어 있으면 재시도하지 않는다 — 조립하는 쪽이 켠다.

    ``Model`` 하나를 받았을 때만 쓴다. ``ModelChain`` 은 자기 정책을 들고 온다
    (선언의 ``retry``)."""
    retry_sleep: Callable[[float], Awaitable[None]] | None = None


@dataclass(frozen=True)
class ExecutorServices:
    """Optional collaborators — 미주입은 곧 "그 기능 없음" 이다.

    다섯 모두 같은 성질을 갖는다: 없으면 해당 기능이 조용히 꺼지고, 루프는
    그대로 돈다. 예컨대 `artifacts` 가 없으면 skill 이 `ctx.artifacts is None`
    을 받고, `telemetry` 가 없으면 집계가 무동작이다.

    기능이 붙을 때마다 생성자가 자라 13개까지 갔다 (#235). 함께 움직이는
    것들이라 한 묶음이다 — 조립하는 쪽은 켤 것만 고른다.
    """

    telemetry: ExecutorTelemetry | None = None
    recall: TaskRecall | None = None
    artifacts: ArtifactStore | None = None
    output_keys: Callable[[TaskRequest], Sequence[str]] | None = None
    """태스크마다 다르다 — 같은 에이전트가 노드마다 다른 템플릿을 쓰고,
    계약은 그 템플릿에 붙어 있다 (#150)."""
    on_cleanup: Callable[[], None] | None = None
    chains: Callable[[ModelConfig], ModelChain] | None = None
    """태스크가 실어 온 모델 선언(``TaskConfig.model``)으로 체인을 만든다.

    없으면 그 선언을 쓸 수 없다 — 기본 체인으로 돌고 WARN 을 남긴다."""


def _tool_error(name: str, task: TaskRequest, agent: str, err: BaseException) -> AaestroError:
    """tool 실패를 출처에 맞는 코드로 변환한다.

    skillset tool 은 ``SKILL_001``, MCP tool 은 ``MCP_003`` — 출처가 다르면
    재시도·알림 전략도 다르기 때문이다 (05 Layer Rules).
    """
    if isinstance(err, AaestroError):
        return err

    is_mcp = is_mcp_tool(name)
    return AaestroError(
        category=ErrorCategory.MCP if is_mcp else ErrorCategory.MODULE,
        code=ErrorCode.MCP_003 if is_mcp else ErrorCode.SKILL_001,
        message=f"tool call failed: {name}",
        agent=agent,
        task_id=task.task_id,
        details={"tool": name},
    )


@dataclass
class _ModelWatch:
    """태스크 하나의 모델 호출이 마감으로 잘렸는지 — 사용자 취소와 가르기 위해 execute 가 본다."""

    cut: bool = False


_MODEL_WATCH: ContextVar[_ModelWatch | None] = ContextVar("aaestro_model_watch", default=None)
"""wait_for 가 만든 자식 태스크도 같은 객체를 본다 — 컨텍스트는 복사돼도 값은 공유된다."""


@dataclass(frozen=True)
class ModuleBinding:
    """What the executor takes from modules — the unit a reload swaps (#274).

    모듈에서 온 것 네 가지를 한 묶음으로 둔다. 태스크는 시작할 때 이것을 **한 번** 잡고
    끝까지 쓴다: 도중에 갈아 끼우면 한 태스크 안에서 프롬프트는 옛 템플릿, 도구는 새 목록이
    섞인다 (02 Hot Reload — 신규 태스크부터 적용).
    """

    tools: ToolRegistry
    render: Callable[[TaskRequest], str]
    tool_schemas: tuple[Any, ...] = ()
    output_keys: Callable[[TaskRequest], Sequence[str]] | None = None
    system: str = ""
    """운영자 지시 — promptset 의 ``system`` 템플릿. 태스크 입력과 섞지 않고 따로 보낸다 (#308)."""
    degraded: tuple[str, ...] = ()
    """기동(또는 리로드) 때 뜨지 못한 optional MCP 서버 — health 가 degraded 로 보고한다."""


class Executor:
    """Runs a task through the model/tool loop.

    태스크를 모델·tool 루프로 실행한다.
    """

    def __init__(
        self,
        *,
        agent: str,
        model: Model | ModelChain,
        tools: ToolRegistry,
        render: Callable[[TaskRequest], str],
        tool_schemas: Sequence[Any] = (),
        config: ExecutorConfig | None = None,
        services: ExecutorServices | None = None,
        system: str = "",
    ) -> None:
        self._agent = agent
        self._config = config or ExecutorConfig()
        # 단일 Model 은 라벨 없는 링크 하나짜리 체인 — 텔레메트리는 생성자 기본 라벨을 쓴다
        self._chain = (
            model
            if isinstance(model, ModelChain)
            else ModelChain(
                links=(ModelLink(model=model, provider="", name=""),),
                retry_policies=self._config.retry_policies,
            )
        )
        self._services = services or ExecutorServices()
        # health 는 provider 를 부르지 않고 최근 호출 결과로 판정한다 (#311)
        self.model_health = ModelHealth()
        self._binding = ModuleBinding(
            tools=tools,
            render=render,
            tool_schemas=tuple(tool_schemas),
            output_keys=self._services.output_keys,
            system=system,
        )
        # 멱등성: 완료된 태스크는 같은 결과를 돌려준다 (재시도/재개 시나리오).
        # 키는 id 만이 아니라 보낸 쪽까지다 — id 는 보낸 쪽이 고르므로, peer 가 남의 id 로
        # 남의 결과를 받거나 남의 재시도가 받을 결과를 먼저 심으면 안 된다 (#400)
        self._completed: OrderedDict[TaskKey, TaskResult] = OrderedDict()
        # 진행 중 태스크의 steer 대기열. 키는 루프 진입 때 생기고 종료(완료·실패·취소·timeout) 때
        # 지워진다 — invoke 를 넘어 남는 상태가 아니다 (02 Statelessness). 락은 없다: 넣기와
        # 꺼내기 모두 await 없이 끝나므로 이벤트 루프 안에서 원자적이다.
        # 키는 _completed 와 같은 (호출자, task_id) — 운영자 steer 는 Control API 공간의 태스크에만
        # 닿고, peer 가 같은 id 로 보낸 태스크의 대기열과 섞이지 않는다 (03 A2A Client Rules 8)
        self._steers: dict[TaskKey, _SteerQueue] = {}

    @property
    def binding(self) -> ModuleBinding:
        """지금 새 태스크가 잡을 모듈 묶음."""
        return self._binding

    @property
    def tool_schemas(self) -> list[Any]:
        """모델과 peer 에게 광고하는 도구 — 실행할 수 있는 것만."""
        return list(self._binding.tool_schemas)

    def rebind(self, binding: ModuleBinding) -> None:
        """Swap the module binding for tasks that start from now on.

        진행 중인 태스크는 시작할 때 잡은 묶음으로 끝까지 간다. 교체는 참조 하나의 대입이라
        이벤트 루프 안에서 원자적이다.
        """
        self._binding = binding

    async def steer(self, task_id: str, text: str, *, by: str) -> None:
        """Queue an operator instruction for a running task's next turn boundary.

        진행 중인 태스크의 다음 턴 경계에 넣을 운영자 지시를 대기열에 올립니다.

        **접수이지 반영이 아닙니다.** 지시는 tool 결과가 붙은 직후에만 대화에 들어갑니다 —
        모델 호출 도중에는 끼어들지 않고, 태스크가 그 전에 끝나면 반영되지 않은 채 WARN 으로
        남습니다. 반영 시점은 ``SteerEvent`` 가 알립니다.

        Args:
            task_id: The running task to steer.
            text: The instruction. 본문은 로그에 남기지 않습니다 (길이만).
            by: Who issued it — 감사용으로 로그와 이벤트에 남습니다.

        Raises:
            AaestroError: NOT_FOUND/``NF_001`` if the task is not running here (끝났거나
                모르는 태스크), VALIDATION/``VAL_002`` if the text or ``by`` is empty or too
                long, or too many instructions are already waiting.
        """
        queue = self._steers.get(TaskKey.from_control(task_id))
        if queue is None:
            raise AaestroError(
                category=ErrorCategory.NOT_FOUND,
                code=ErrorCode.NF_001,
                message=f"no in-flight task: {task_id}",
                agent=self._agent,
                task_id=task_id,
            )
        pending = queue.pending
        problem = _steer_problem(text, by, waiting=len(pending))
        if problem is not None:
            raise AaestroError(
                category=ErrorCategory.VALIDATION,
                code=ErrorCode.VAL_002,
                message=str(problem),
                agent=self._agent,
                task_id=task_id,
                details={
                    "max_chars": MAX_STEER_CHARS,
                    "max_by_chars": MAX_STEER_BY_CHARS,
                    "max_pending": MAX_PENDING_STEERS,
                },
            )
        pending.append(Steer(text=text, by=by))
        log.info(
            "steer queued",
            agent=self._agent,
            task_id=task_id,
            run_id=queue.run_id,
            steered_by=by,
            text_chars=len(text),
            pending=len(pending),
        )

    async def execute(self, task: TaskRequest) -> TaskResult:
        """Run a task to completion.

        태스크를 완수합니다. 실패는 예외가 아니라 ``TaskResult`` 로 보고하므로
        데몬이 죽지 않습니다 — 단 취소는 그대로 전파됩니다.

        Args:
            task: The task to run.

        Returns:
            The task result, successful or failed.
        """
        cached = self._completed.get(task.key)
        if cached is not None:
            return cached

        started = time.perf_counter()
        watch = _ModelWatch()
        watching = _MODEL_WATCH.set(watch)
        try:
            result = await asyncio.wait_for(self._run(task), timeout=task.config.timeout_s)
        except TimeoutError:
            if watch.cut:
                # 모델이 태스크 마감까지 답하지 않았다 — health 가 그것을 봐야 한다
                self.model_health.failed(ErrorCode.TO_001)
            result = TaskResult.failed(
                task,
                AaestroError(
                    category=ErrorCategory.TIMEOUT,
                    code=ErrorCode.TO_001,
                    message="task timeout exceeded",
                    agent=self._agent,
                    task_id=task.task_id,
                    retryable=True,
                ),
            )
        except asyncio.CancelledError:
            # 취소는 협조적 종료다 — 결과로 삼키지 않고 전파한다
            self._cleanup()
            raise
        except AaestroError as err:
            result = TaskResult.failed(task, err)
        except Exception as err:
            # 모델 provider 예외가 새어나가면 데몬이 죽는다 — tool 쪽은 이미
            # _tool_error 가 변환하는데 모델 쪽만 빠져 있었다
            result = TaskResult.failed(
                task,
                AaestroError(
                    category=ErrorCategory.INTERNAL,
                    code=ErrorCode.INTERNAL_001,
                    message="unexpected error during task execution",
                    agent=self._agent,
                    task_id=task.task_id,
                    details={"cause": type(err).__name__},
                ),
            )
        finally:
            _MODEL_WATCH.reset(watching)

        duration_s = time.perf_counter() - started
        self._record_task(result, task=task, duration_s=duration_s)
        self._log_outcome(task, error=result.error, usage=result.usage, duration_s=duration_s)

        # 재시도 가능한 실패를 캐싱하면 이 계층이 유일한 재시도 계층인데도
        # 재시도가 영원히 무효화된다 — 성공과 영구 실패만 기억한다
        if result.error is None or not result.error.retryable:
            self._remember(task.key, result)
        return result

    def _remember(self, key: TaskKey, result: TaskResult) -> None:
        self._completed[key] = result
        while len(self._completed) > REMEMBERED_RESULTS:
            self._completed.popitem(last=False)

    def _log_outcome(
        self,
        task: TaskRequest,
        *,
        error: AaestroErrorPayload | None,
        usage: ModelUsage | None,
        duration_s: float,
    ) -> None:
        """태스크 종료를 로그로 남긴다 — 실패는 ERROR (05 Log Levels).

        실패를 ``TaskResult`` 로만 돌려주면 데몬은 살지만 원인이 컨테이너 로그에서 사라진다:
        호출자는 코드만 받고, 피호출자 쪽에는 무엇이 실패했는지 남지 않는다 (#290).
        """
        fields: dict[str, Any] = {
            "agent": self._agent,
            "task_id": task.task_id,
            "run_id": task.run_id,
            "graph": task.trace.graph or DIRECT_GRAPH,
            "duration_ms": int(duration_s * 1000),
        }
        if task.node_id is not None:
            fields["node_id"] = task.node_id
        if error is not None:
            log.error(
                "agent task failed",
                status=STATUS_FAILED,
                error_code=error.code,
                retryable=error.retryable,
                error_message=error.message,
                **fields,
            )
            return
        log.info(
            "agent task completed",
            status=STATUS_COMPLETED,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            **fields,
        )

    def _record_task(self, result: TaskResult, *, task: TaskRequest, duration_s: float) -> None:
        """태스크 종료를 메트릭에 남긴다 — telemetry 미주입 시 무동작."""
        if self._services.telemetry is None:
            return
        self._services.telemetry.task_finished(
            status=result.status.value, duration_s=duration_s, graph=task.trace.graph
        )

    def _shape_output(
        self, content: str, task: TaskRequest, binding: ModuleBinding
    ) -> dict[str, Any]:
        """Build the task output from the model's final response.

        선언된 키가 없으면 기존대로 ``{"content": ...}`` 하나입니다.

        선언이 있으면 응답을 JSON 으로 읽어 그 키들만 옮깁니다 — 여분 키는
        버립니다 (선언되지 않은 값이 state 로 흘러가면 02 Rule 5 의 출력
        규율이 깨집니다).

        Raises:
            AaestroError: MODEL/``LLM_004`` if the response is not JSON or a
                declared key is missing — 조용히 빈 출력으로 떨어지면 그래프가
                다음 노드에서야 GRAPH_003 으로 실패해 원인이 멀어집니다.
        """
        keys = tuple(binding.output_keys(task)) if binding.output_keys else ()
        if not keys:
            return {"content": content}

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as err:
            raise self._output_error("model response is not json", content, keys) from err

        if not isinstance(parsed, dict):
            raise self._output_error("model response is not a json object", content, keys)

        missing = [key for key in keys if key not in parsed]
        if missing:
            raise self._output_error(f"declared output keys missing: {missing}", content, keys)

        return {key: parsed[key] for key in keys}

    def _output_error(self, message: str, content: str, keys: Sequence[str]) -> AaestroError:
        """출력 계약 위반 — 응답 꼬리를 남겨 promptset 드리프트를 추적한다."""
        return AaestroError(
            category=ErrorCategory.MODEL,
            code=ErrorCode.LLM_004,
            message=message,
            agent=self._agent,
            details={"declared": ",".join(keys), "content": content[-300:]},
        )

    async def _opening(self, task: TaskRequest, binding: ModuleBinding) -> UserTurn:
        """Build the task-entry user turn, recalling memory once.

        09 Context Assembly 의 구성을 따릅니다: ``system(promptset)`` 은 따로 가고, 여기는
        ``task input + recalled memory`` 입니다. 회상 기억은 태스크 입력과 **다른 블록**으로
        싣습니다 — 과거 산출물이 현재 지시처럼 읽히지 않게 (09 Rule 6).

        회상은 **태스크당 1회**입니다 — tool loop 가 N 턴 돌아도 다시 검색하지 않습니다 (09 Rule 7).
        추가 탐색은 모델이 ``memory_search`` 를 명시 호출합니다.
        """
        prompt = binding.render(task)
        if self._services.recall is None:
            return UserTurn(parts=(prompt,))

        context = await self._services.recall(task)
        if not context:
            return UserTurn(parts=(prompt,))
        return UserTurn(parts=(context, prompt))

    async def _run(self, task: TaskRequest) -> TaskResult:
        """공용 루프를 끝까지 돌려 결과만 취한다.

        중간 이벤트는 버린다 — `execute` 의 소비자는 진행 과정이 아니라 결과를
        원한다. 실패는 루프가 예외로 올리고 `execute` 가 `TaskResult` 로 접는다
        (02 Rule 4).
        """
        async for event in self._events(task):
            if isinstance(event, DoneEvent):
                return TaskResult.completed(task, output=event.output, usage=event.usage)
        raise AssertionError("event loop ended without a terminal event")  # pragma: no cover

    async def _call_model(
        self, link: ModelLink, messages: Sequence[Message], binding: ModuleBinding
    ) -> ModelResponse:
        """모델 한 번 — 실패도 메트릭에 남겨야 하므로 여기서 감싼다.

        **재시도 안쪽**이라 시도마다 계수된다: 05 의 rate limit 알림은
        발생 빈도를 보므로, 재시도로 성공한 호출의 rate limit 이 지워지면
        provider 압박이 지표에서 사라진다. 라벨은 **실제로 부른** 모델이다.
        """
        tools = list(binding.tool_schemas)
        telemetry = self._services.telemetry
        try:
            response = await link.model.run(binding.system, list(messages), tools)
        except asyncio.CancelledError:
            # 마감에 잘린 것인지 사용자 취소인지는 execute 가 안다 — 여기서는 표시만 한다
            watch = _MODEL_WATCH.get()
            if watch is not None:
                watch.cut = True
            raise
        except Exception as err:
            self.model_health.failed(err.code if isinstance(err, AaestroError) else "INTERNAL_001")
            if telemetry is not None:
                telemetry.model_called(status=_model_status(err), **_labels(link))
            raise

        self.model_health.succeeded()
        response = _answered_by(response, link)
        if telemetry is not None:
            telemetry.model_called(status=STATUS_COMPLETED, usage=response.usage, **_labels(link))
        return response

    async def _model_turn(
        self, messages: Sequence[Message], binding: ModuleBinding, chain: ModelChain
    ) -> ModelResponse:
        """모델 한 턴 — 정책이 허용하는 실패는 재시도하고, 소진되면 fallback 으로 넘어간다.

        **재시도 주체는 agentd 다** (05 Retry Layering). provider SDK 재시도는
        어댑터가 꺼 두었으므로 backoff 가 곱해지지 않는다. 다음 모델을 고르는 것도
        재시도의 일부라 같은 곳이 맡는다 (#359).
        """

        async def call(link: ModelLink) -> ModelResponse:
            return await self._call_model(link, messages, binding)

        return await chain.turn(call, sleep=self._config.retry_sleep, agent=self._agent)

    def _chain_for(self, task: TaskRequest) -> ModelChain:
        """이 태스크가 끝까지 쓸 모델 체인 — 그래프가 해석한 선언이 있으면 그것.

        Raises:
            AaestroError: CONFIG/``CFG_001`` if the declaration names an unbound provider.
        """
        declared = task.config.model
        if declared is None:
            return self._chain
        if self._services.chains is None:
            # 조용히 기본 모델로 떨어지면 override 가 효과 없는 이유를 찾을 수 없다
            log.warning(
                "task model declaration ignored, no model factory bound",
                agent=self._agent,
                task_id=task.task_id,
                model=declared.name,
                provider=declared.provider,
            )
            return self._chain
        return self._services.chains(declared)

    def _tool_timeout(self, name: str, task: TaskRequest, binding: ModuleBinding) -> float:
        """tool 실행 상한 — per-tool 선언이 있으면 그것, 없으면 더 엄격한 쪽."""
        declared = binding.tools.timeout_for(name)
        if declared:
            return declared
        return min(task.config.tool_timeout_s, self._config.tool_timeout_s)

    async def _run_tool(
        self, call: ToolCall, task: TaskRequest, ctx: SkillContext, binding: ModuleBinding
    ) -> Any:
        """단일 tool 을 상한 안에서 실행하고 실패를 변환한다."""
        timeout = self._tool_timeout(call.name, task, binding)
        try:
            result = await asyncio.wait_for(
                binding.tools.call(call.name, call.arguments, ctx), timeout=timeout
            )
        except TimeoutError as err:
            self._record_tool(call.name, status=STATUS_FAILED)
            raise AaestroError(
                category=ErrorCategory.TIMEOUT,
                code=ErrorCode.TO_002,
                message=f"tool timeout: {call.name}",
                agent=self._agent,
                task_id=task.task_id,
                retryable=True,
                details={"tool": call.name},
            ) from err
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self._record_tool(call.name, status=STATUS_FAILED)
            raise _tool_error(call.name, task, self._agent, err) from err

        self._record_tool(call.name, status=STATUS_COMPLETED)
        return result

    def _record_tool(self, tool: str, *, status: str) -> None:
        """tool 호출을 메트릭에 남긴다 — telemetry 미주입 시 무동작."""
        if self._services.telemetry is not None:
            self._services.telemetry.tool_called(tool=tool, status=status)

    def _cleanup(self) -> None:
        """취소 시 진행 중 자원을 정리한다."""
        if self._services.on_cleanup is not None:
            self._services.on_cleanup()

    async def stream(self, task: TaskRequest) -> AsyncIterator[TaskEvent]:
        """Run a task, emitting events as it progresses.

        태스크를 실행하며 진행 이벤트를 발행합니다 — 장시간 태스크의 소비 경로입니다.

        ``execute`` 와 동일한 상한을 적용합니다: 태스크 timeout 을 넘기면
        ``TO_001`` 이벤트로 끝냅니다. 스트리밍은 장시간 태스크의 경로라
        멈춘 provider 를 만날 가능성이 가장 높습니다.

        Args:
            task: The task to run.

        Yields:
            Token, tool call/result, and terminal done/error events.
        """
        deadline = asyncio.get_running_loop().time() + task.config.timeout_s
        started = time.perf_counter()
        events = self._stream(task)

        def finished(event: TaskEvent) -> TaskEvent:
            if isinstance(event, ErrorEvent):
                self._log_outcome(
                    task, error=event.error, usage=None, duration_s=time.perf_counter() - started
                )
            elif isinstance(event, DoneEvent):
                self._log_outcome(
                    task, error=None, usage=event.usage, duration_s=time.perf_counter() - started
                )
            return event

        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    # 종료 이벤트보다 먼저 닫는다 — 끝났다고 알린 태스크가 steer 를 받지 않게
                    await events.aclose()
                    yield finished(self._timeout_event(task))
                    return
                try:
                    event = await asyncio.wait_for(anext(events), timeout=remaining)
                except StopAsyncIteration:
                    return
                except TimeoutError:
                    yield finished(self._timeout_event(task))
                    return
                yield finished(event)
        finally:
            # 마감으로 끊거나 소비자가 떠나도 루프를 바로 닫는다 — steer 대기열이 남지 않게
            await events.aclose()

    def _recall_failure(self, err: BaseException) -> AaestroError:
        """회상 실패를 구조화 에러로 만든다 — 기억이 없다고 태스크가 죽지는 않는다."""
        if isinstance(err, AaestroError):
            return err
        return AaestroError(
            category=ErrorCategory.MEMORY,
            code=ErrorCode.MEM_004,
            message="auto-recall failed",
            agent=self._agent,
            details={"cause": type(err).__name__},
        )

    def _timeout_event(self, task: TaskRequest) -> ErrorEvent:
        """태스크 상한 초과를 종료 이벤트로 만든다."""
        return ErrorEvent(
            task_id=task.task_id,
            error=AaestroError(
                category=ErrorCategory.TIMEOUT,
                code=ErrorCode.TO_001,
                message="task timeout exceeded",
                agent=self._agent,
                task_id=task.task_id,
                retryable=True,
            ).payload(),
        )

    async def _stream(self, task: TaskRequest) -> AsyncGenerator[TaskEvent]:
        """공용 루프의 이벤트를 그대로 흘리고, 실패만 이벤트로 접는다.

        루프는 실패를 **예외로** 올린다 — 그것이 `execute` 와 공유하는 계약이다.
        스트리밍 소비자에게는 예외가 아니라 종료 이벤트여야 하므로 여기서 바꾼다:
        같은 실패가 한쪽에서는 `TaskResult`, 다른 쪽에서는 예외로 새어나가면
        소비자가 두 경로를 다르게 다뤄야 한다.
        """
        try:
            # 소비자가 도중에 떠나도(연결 끊김) 루프와 steer 대기열이 바로 정리되게 닫는다
            async with aclosing(self._events(task)) as events:
                async for event in events:
                    yield event
        except asyncio.CancelledError:
            raise
        except AaestroError as err:
            yield ErrorEvent(task_id=task.task_id, error=err.payload())
        except Exception as err:
            yield ErrorEvent(task_id=task.task_id, error=self._recall_failure(err).payload())

    async def _events(self, task: TaskRequest) -> AsyncGenerator[TaskEvent]:
        """The tool loop with the task's steer queue open around it.

        steer 대기열의 수명을 루프에 묶는다: 루프가 도는 동안만 ``steer`` 가 접수되고,
        어떤 식으로 끝나든(완료·실패·취소·timeout·소비자 이탈) ``finally`` 가 지운다.

        종료 이벤트는 대기열을 **닫은 뒤에** 낸다 — 소비자가 ``done`` 을 본 뒤에 보낸 steer 가
        접수됐다가 조용히 사라지지 않고 404 를 받게.
        """
        queue = self._open_steers(task)
        try:
            async with aclosing(self._turns(task, queue.pending)) as events:
                async for event in events:
                    if isinstance(event, DoneEvent):
                        self._close_steers(task, queue)
                    yield event
        finally:
            self._close_steers(task, queue)

    def _open_steers(self, task: TaskRequest) -> _SteerQueue:
        """이 실행의 steer 대기열을 연다 — **먼저 시작한 실행이 접수 창구를 유지한다**.

        같은 ``task_id`` 로 겹쳐 시작한 나중 실행은 창구를 얻지 못한다 (자기만의 빈 대기열로
        돌고, steer 를 받지 않는다). 나중 것이 덮어쓰면 A2A 로 태스크를 보내는 peer 가 진행 중인
        ``task_id`` 를 골라 운영자 지시를 자기 태스크의 대화로 가로챌 수 있고, 악의 없는
        재시도도 먼저 끝나면서 아직 도는 실행의 창구를 지운다.
        """
        queue = _SteerQueue(run_id=task.run_id)
        self._steers.setdefault(task.key, queue)
        return queue

    def _close_steers(self, task: TaskRequest, queue: _SteerQueue) -> None:
        """대기열을 닫는다 — 반영되지 못한 지시는 버리되 **조용히 버리지 않는다**.

        "queued" 로 답한 지시가 흔적 없이 사라지면 운영자는 반영됐다고 믿는다. 두 번 불려도
        안전하다 (종료 이벤트 직전과 ``finally``). 창구가 자기 것일 때만 지운다 — 겹쳐 돈
        다른 실행의 창구를 건드리지 않는다.
        """
        if self._steers.get(task.key) is queue:
            del self._steers[task.key]
        if not queue.pending:
            return
        log.warning(
            "steer dropped, task ended before the next turn boundary",
            agent=self._agent,
            task_id=task.task_id,
            run_id=task.run_id,
            dropped=len(queue.pending),
            issuers=sorted({steer.by for steer in queue.pending}),
        )
        queue.pending.clear()

    async def _turns(self, task: TaskRequest, pending: list[Steer]) -> AsyncGenerator[TaskEvent]:
        """The one tool loop — model turns, tool calls, and the terminal event.

        **이 루프는 한 곳에만 존재한다** (#233). 이전에는 `_run` 과 `_stream` 이
        각자 구현해, `_shape_output` 이 execute 경로에서만 불리는 발산이 생겼다 —
        같은 태스크가 엔드포인트에 따라 다른 output 계약을 냈다.

        실패는 예외로 올린다. 소비자가 그것을 `TaskResult` 로 접거나
        (`_run`) 종료 이벤트로 바꾼다 (`_stream`).

        Yields:
            Token, tool call/result, and a terminal done event.

        Raises:
            AaestroError: When a turn fails or the turn ceiling is reached.
        """
        # 이 태스크가 끝까지 쓸 모듈 묶음과 모델 체인 — 리로드가 도중에 갈아 끼워도 섞이지 않는다
        binding = self._binding
        chain = self._chain_for(task)
        messages: list[Message] = [await self._opening(task, binding)]
        usage = ModelUsage()
        max_turns = self._max_turns(task)
        ctx = self._skill_context(task)

        try:
            for turn in range(max_turns):
                response = await self._model_turn(messages, binding, chain)
                usage = usage.merge(response.usage)

                if response.content:
                    yield TokenEvent(task_id=task.task_id, text=response.content)

                if response.is_final:
                    yield DoneEvent(
                        task_id=task.task_id,
                        output=self._shape_output(response.content, task, binding),
                        usage=usage,
                    )
                    return

                for call in response.tool_calls:
                    yield ToolCallEvent(
                        task_id=task.task_id,
                        tool=call.name,
                        arguments=dict(call.arguments),
                        turn=turn,
                    )

                outcomes = []
                failure: AaestroError | None = None
                for event, outcome in await self._invoke_tools(
                    response.tool_calls, task, ctx, turn, binding
                ):
                    yield event
                    if isinstance(outcome, AaestroError):
                        failure = failure or outcome
                    else:
                        outcomes.append(outcome)

                if failure is not None:
                    raise failure

                # 모델이 보낸 그대로(raw) 되돌리고, 모든 결과를 **한** 메시지에 모은다 —
                # 나눠 보내면 모델이 병렬 호출을 멈추도록 학습된다
                messages.append(
                    AssistantTurn(response.content, response.tool_calls, raw=response.raw)
                )
                messages.append(ToolTurn(outcomes=tuple(outcomes)))

                # 턴 경계 — tool 결과가 붙은 직후에만 steer 를 넣는다 (assistant → tool → user).
                # 모델 호출 도중에는 끼어들지 않고, 이미 보낸 메시지는 고치지 않는다.
                # 다음 모델 호출이 없는 마지막 회차에서는 꺼내지 않는다 — 반영된 척하지 않게
                if pending and turn + 1 < max_turns:
                    steers = tuple(pending)
                    pending.clear()
                    # 여러 건은 user 턴 **하나**로. tool 결과 뒤에 user 턴이 오는 것은 프레임워크
                    # 대화 타입에서의 순서다 — provider 의 wire 역할 교대는 어댑터가 맞춘다
                    messages.append(UserTurn(parts=tuple(_steer_part(s) for s in steers)))
                    log.info(
                        "steer applied",
                        agent=self._agent,
                        task_id=task.task_id,
                        run_id=task.run_id,
                        turn=turn,
                        applied=len(steers),
                        issuers=sorted({steer.by for steer in steers}),
                    )
                    for steer in steers:
                        yield SteerEvent(
                            task_id=task.task_id, text=steer.text, by=steer.by, turn=turn
                        )
        except asyncio.CancelledError:
            self._cleanup()
            raise

        raise self._turn_ceiling(task, max_turns)

    def _max_turns(self, task: TaskRequest) -> int:
        """이번 태스크의 turn 상한.

        태스크가 기본값을 그대로 쓰면 executor 설정을 따른다 — `or` 로 고르면
        `TaskConfig` 의 기본값(20)이 항상 이겨 설정이 무시된다.
        """
        return min(task.config.max_turns, self._config.max_turns)

    def _skill_context(self, task: TaskRequest) -> SkillContext:
        """skill 이 받는 실행 컨텍스트."""
        return SkillContext(
            agent=self._agent,
            task_id=task.task_id,
            run_id=task.run_id,
            artifacts=self._services.artifacts,
            # 위임이 깊이를 이어받아야 순환이 상한에 걸린다 (03 Rule 5)
            trace=task.trace,
        )

    def _turn_ceiling(self, task: TaskRequest, max_turns: int) -> AaestroError:
        """turn 상한 초과 — 무한 루프를 막는 마지막 방어선 (02 Loop Rules 1)."""
        return AaestroError(
            category=ErrorCategory.MODEL,
            code=ErrorCode.LLM_005,
            message="max turns exceeded",
            agent=self._agent,
            task_id=task.task_id,
            details={"max_turns": max_turns},
        )

    async def _invoke_tools(
        self,
        calls: Sequence[ToolCall],
        task: TaskRequest,
        ctx: SkillContext,
        turn: int,
        binding: ModuleBinding,
    ) -> list[tuple[ToolResultEvent, Any]]:
        """독립 tool 호출을 병렬 실행하고, 각 결과를 이벤트와 짝지어 돌려준다.

        하나가 실패해도 형제 호출을 중도 취소하지 않는다 — 취소된 호출은 결과도
        메트릭도 남기지 못해, 다중 tool 턴의 집계가 조용히 비게 된다.

        Returns:
            ``(이벤트, 결과)`` 쌍. 실패한 호출의 결과 자리에는 `AaestroError` 가 온다.
        """
        started = time.monotonic()
        outcomes = await asyncio.gather(
            *(self._run_tool(call, task, ctx, binding) for call in calls),
            return_exceptions=True,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)

        paired: list[tuple[ToolResultEvent, Any]] = []
        for call, outcome in zip(calls, outcomes, strict=True):
            if isinstance(outcome, asyncio.CancelledError):
                self._cleanup()
                raise outcome
            if isinstance(outcome, BaseException):
                error = (
                    outcome
                    if isinstance(outcome, AaestroError)
                    else _tool_error(call.name, task, self._agent, outcome)
                )
                paired.append(
                    (
                        ToolResultEvent(
                            task_id=task.task_id,
                            tool=call.name,
                            turn=turn,
                            duration_ms=elapsed_ms,
                            error=error.payload(),
                        ),
                        error,
                    )
                )
                continue
            paired.append(
                (
                    ToolResultEvent(
                        task_id=task.task_id,
                        tool=call.name,
                        result=outcome,
                        turn=turn,
                        duration_ms=elapsed_ms,
                    ),
                    tool_outcome(call, outcome),
                )
            )
        return paired


_ISSUER_PROBLEMS: Final = {
    IdentifierProblem.NOT_STRING: SteerProblem.ISSUER_MISSING,
    IdentifierProblem.EMPTY: SteerProblem.ISSUER_MISSING,
    IdentifierProblem.TOO_LONG: SteerProblem.ISSUER_TOO_LONG,
    IdentifierProblem.UNPRINTABLE: SteerProblem.ISSUER_NOT_PRINTABLE,
    IdentifierProblem.SURROUNDING_WHITESPACE: SteerProblem.ISSUER_NOT_PRINTABLE,
}
"""식별자 사유 → steer 의 닫힌 거절 문구. ``trimmed=False`` 라 앞뒤 공백 사유는 나오지 않는다."""


def _steer_problem(text: str, by: str, *, waiting: int) -> SteerProblem | None:
    """steer 를 받을 수 없는 이유 — 받을 수 있으면 None.

    사유는 닫힌 집합(``SteerProblem``)에서만 고른다: runtime 이 그 집합에 있는 문구만 운영자에게
    옮기므로, 여기서 자유 문구를 지어내면 운영자는 뭉뚱그린 사유를 본다.
    """
    if not text.strip():
        return SteerProblem.TEXT_EMPTY
    if len(text) > MAX_STEER_CHARS:
        return SteerProblem.TEXT_TOO_LONG
    # 발행자는 로그와 이벤트에 그대로 실린다 — 규칙은 식별자와 하나 (core.identifiers).
    # 표시용 이름이라 앞뒤 공백은 받는다 ("김 운영자 <ops@example.com>")
    issuer = identifier_problem(by, max_chars=MAX_STEER_BY_CHARS, trimmed=False)
    if issuer is not None:
        return _ISSUER_PROBLEMS[issuer]
    if waiting >= MAX_PENDING_STEERS:
        return SteerProblem.TOO_MANY_PENDING
    return None


def _steer_part(steer: Steer) -> str:
    """steer 한 건이 user 턴에 실리는 모양 — 머리말로 태스크 입력과 구분한다."""
    return f"{STEER_HEADER}\n{steer.text}"


class _ModelLabels(TypedDict):
    provider: str | None
    model: str | None
    fallback_index: int


def _labels(link: ModelLink) -> _ModelLabels:
    """메트릭 라벨 — 빈 문자열 링크는 None 이라 텔레메트리가 생성자 기본 라벨을 쓴다."""
    return {
        "provider": link.provider or None,
        "model": link.name or None,
        "fallback_index": link.index,
    }


def _answered_by(response: ModelResponse, link: ModelLink) -> ModelResponse:
    """응답의 usage 에 실제로 답한 모델을 적는다 — 어댑터가 이미 적었으면 그대로 둔다.

    라벨을 모르는 단일 모델(빈 문자열 링크)은 적을 것이 없다.
    """
    usage = response.usage
    model = usage.model or link.name or None
    provider = usage.provider or link.provider or None
    if model == usage.model and provider == usage.provider:
        return response
    return replace(response, usage=usage.model_copy(update={"model": model, "provider": provider}))


def tool_outcome(call: ToolCall, result: Any) -> ToolOutcome:
    """Turn a tool's return value into what the model reads back.

    문자열은 그대로, 그 밖은 JSON 으로 싣는다 — ``repr`` 을 실으면 모델이 파이썬 객체 표기를 읽는다.
    MCP 도구가 **실행은 됐지만 오류를 보고한** 결과(``is_error``)는 오류로 표시해 돌려준다: 태스크를
    실패시킬 일은 아니지만 모델이 성공으로 읽어서도 안 된다.
    """
    is_error = bool(getattr(result, "is_error", False))
    payload = getattr(result, "content", result) if hasattr(result, "is_error") else result
    if isinstance(payload, str):
        content = payload
    else:
        content = json.dumps(payload, ensure_ascii=False, default=str)
    return ToolOutcome(call_id=call.id, content=content, is_error=is_error)


__all__ = [
    "MAX_PENDING_STEERS",
    "MAX_STEER_BY_CHARS",
    "MAX_STEER_CHARS",
    "MODEL_RETRY_POLICIES",
    "STEER_HEADER",
    "AssistantTurn",
    "Executor",
    "ExecutorConfig",
    "ExecutorServices",
    "Model",
    "Message",
    "ModelResponse",
    "Steer",
    "SteerProblem",
    "ToolCall",
    "ToolOutcome",
    "ToolRegistry",
    "ToolTurn",
    "UserTurn",
    "tool_outcome",
]
