"""Agent contract — the interface every agent implementation must satisfy.

에이전트 구현의 기본 계약. agentd 가 이 인터페이스를 Control API 로 서빙하며,
오케스트레이터는 오직 Control API 를 통해서만 에이전트를 호출한다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from aaestro.core.errors import AaestroError, AaestroErrorPayload
from aaestro.core.manifest import ModelConfig

if TYPE_CHECKING:
    from aaestro.core.events import TaskEvent

AGENT_TOKEN_ENV = "AAESTRO_AGENT_TOKEN"  # noqa: S105 — 값이 아니라 키 이름이다
DEFAULT_TASK_TIMEOUT_S = 300.0
DEFAULT_MAX_TURNS = 20
DEFAULT_TOOL_TIMEOUT_S = 60.0
DEFAULT_A2A_DEPTH_LIMIT = 3
CONTROL_OVERHEAD_S = 5.0
"""태스크 timeout 위에 호출자가 더 기다리는 여유 — Control API 왕복과 결과 직렬화 몫.

호출자(orchestrator, Control API 클라이언트)가 ``timeout_s`` 에 정확히 끊으면 agentd 의
``TO_001`` 결과보다 호출자의 timeout 이 먼저 나서, 에이전트가 이미 보고한 실패를 버리게 된다
(#314). 강제는 agentd 가 하고, 호출자는 그 결과가 돌아올 틈을 준다.
"""


class SteerProblem(StrEnum):
    """Why an agent refuses a steer with ``VAL_002`` — the closed set of reasons.

    에이전트가 steer 를 ``VAL_002`` 로 거절하는 사유의 **닫힌 집합**. 값이 곧 거절 문구다.

    양쪽이 함께 본다: agentd 는 이 문구로 거절하고, runtime 은 에이전트 응답의 문구가 이 집합의
    원소와 **정확히 같을 때만** 운영자에게 옮긴다. 에이전트 컨테이너는 신뢰하지 않으므로, 여기
    없는 문구는 운영자 화면에 닿지 않는다 (#364).
    """

    TEXT_EMPTY = "steer text is empty"
    TEXT_TOO_LONG = "steer text is too long"
    ISSUER_MISSING = "steer needs who issued it"
    ISSUER_TOO_LONG = "steer issuer is too long"
    ISSUER_NOT_PRINTABLE = "steer issuer has control characters"
    TOO_MANY_PENDING = "too many steers are waiting for the next turn boundary"


class TaskStatus(StrEnum):
    """태스크 종료 상태."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class HealthState(StrEnum):
    """헬스 상태 — degraded 는 optional 컴포넌트 실패 또는 지연 임계 초과."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class TraceContext(BaseModel):
    """Distributed tracing context propagated across A2A calls.

    분산 추적 컨텍스트. A2A 호출을 통해 전파되어 run 전체를 단일 trace 로 묶는다.
    ``depth`` 는 위임 체인 깊이로, 상한 초과 시 ``A2A_005`` 로 차단된다.
    """

    model_config = ConfigDict(frozen=True)

    trace_id: str
    span_id: str | None = None
    depth: int = 0
    graph: str | None = None
    """이 태스크를 낳은 그래프 — direct 요청은 ``None``.

    에이전트는 이 값을 **메트릭 라벨로만** 쓰고 동작을 바꾸지 않는다:
    전달받는 것과 가정하는 것은 다르며, 02 Rule 6 이 금지하는 것은 후자다.
    """

    def child(self, span_id: str) -> TraceContext:
        """Derive a child context with incremented depth.

        깊이를 1 증가시킨 자식 컨텍스트를 만듭니다 (A2A 위임 시 사용).
        ``graph`` 는 그대로 물려줍니다 — 위임된 태스크도 원래 어느 그래프의
        일이었는지 알아야 메트릭이 갈라지지 않습니다.
        """
        return TraceContext(
            trace_id=self.trace_id,
            span_id=span_id,
            depth=self.depth + 1,
            graph=self.graph,
        )


class TaskConfig(BaseModel):
    """Per-task execution limits.

    태스크 실행 상한. 모델 호출/tool 호출/A2A 호출에 각각 개별 timeout 이 적용된다.
    """

    model_config = ConfigDict(frozen=True)

    timeout_s: float = DEFAULT_TASK_TIMEOUT_S
    max_turns: int = DEFAULT_MAX_TURNS
    tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S
    locale: str | None = None
    model: ModelConfig | None = None
    """그래프가 해석한 이 태스크의 모델.

    None 이면 agentd 가 manifest 기본값을 쓴다 (direct 요청).
    """


class ModelUsage(BaseModel):
    """Token accounting for a task.

    태스크 단위 토큰 사용량 집계.
    """

    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    model: str | None = None
    provider: str | None = None

    def merge(self, other: ModelUsage) -> ModelUsage:
        """Accumulate another usage record.

        여러 번의 모델 호출 사용량을 누적합니다 (tool loop 의 turn 마다 호출).
        """
        return ModelUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            model=other.model or self.model,
            provider=other.provider or self.provider,
        )


class TaskKey(NamedTuple):
    """Who sent a task, and the id they chose — the key for per-task state in an agent process.

    에이전트 프로세스가 태스크별 상태(멱등 캐시, 진행 중 태스크 표)를 쥘 때 쓰는 키.
    ``task_id`` 는 **보낸 쪽이 고른다.** 프레임워크는 그 값이 다른 호출자에게 추측 불가능하다고
    가정하지 않는다 — id 하나로 키를 잡으면 같은 id 를 보낸 쪽이 남의 결과를 받거나 남의 상태를
    덮는다 (#400).

    보장은 ``caller`` 가 얼마나 확인됐느냐만큼이다:

    - 두 수신 모드 공통: peer 는 Control API 공간(``caller=None``)의 결과 · 상태에 닿지 못한다.
      A2A 수신 입구는 ``caller`` 를 비워 두지 않고, Control API 는 본문의 ``caller`` 를 지운다.
    - peer 와 peer 사이: 레지스트리 모드에서만. 토큰 모드의 ``caller`` 는 배포가 공유하는 서명
      키로 확인한 헤더 주장이라 그 키를 쥔 peer 가 다른 peer 의 이름을 댈 수 있다 (#415).

    밖으로 나가는 id (로그 · 메트릭 · 응답) 는 ``task_id`` 그대로다. 갈라지는 것은 이 키뿐이다.
    """

    caller: str | None
    """A2A 수신 입구가 실은 호출자. Control API 로 온 태스크(그래프 노드 · direct)는 ``None``."""
    task_id: str

    @classmethod
    def from_control(cls, task_id: str) -> TaskKey:
        """Control API 가 id 만으로 가리키는 태스크 (취소 등) — peer 의 동명 태스크는 아니다."""
        return cls(caller=None, task_id=task_id)


class TaskRequest(BaseModel):
    """A unit of work handed to an agent.

    에이전트에게 전달되는 단일 태스크. 그래프 노드 태스크 / peer 위임 태스크 /
    direct 요청 태스크가 모두 동일한 계약을 사용한다.
    """

    model_config = ConfigDict(frozen=True)

    task_id: str
    run_id: str
    node_id: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    config: TaskConfig = Field(default_factory=TaskConfig)
    trace: TraceContext
    caller: str | None = None
    """A2A 로 받은 태스크의 **확인된** 호출자 — 피호출자의 수신 입구만 채운다 (#279).

    그래프·direct 태스크는 ``None``. Control API 본문이나 A2A 본문에 실려 온 값은 버린다: 확인하지
    않은 이름으로 권한 에이전트가 부여 대상을 정하면 다른 에이전트 행세가 된다.
    """

    @property
    def key(self) -> TaskKey:
        """태스크별 상태의 키 — ``caller`` 는 본문이 아니라 수신 입구가 싣는다 (``TaskKey``)."""
        return TaskKey(caller=self.caller, task_id=self.task_id)

    @property
    def is_direct(self) -> bool:
        """그래프 run 과 무관한 direct 요청인지 — ``node_id`` 부재로 판별."""
        return self.node_id is None

    @property
    def template_name(self) -> str:
        """렌더할 promptset 템플릿 이름 — direct 요청은 ``default``."""
        return self.node_id if self.node_id is not None else "default"


class TaskResult(BaseModel):
    """The outcome of a task, merged back into graph state.

    태스크 실행 결과. ``output`` 은 그래프 state schema 와 호환되는 키만 포함한다.
    """

    model_config = ConfigDict(frozen=True)

    task_id: str
    status: TaskStatus
    output: dict[str, Any] = Field(default_factory=dict)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    error: AaestroErrorPayload | None = None
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def completed(
        cls,
        task: TaskRequest,
        *,
        output: dict[str, Any] | None = None,
        usage: ModelUsage | None = None,
    ) -> TaskResult:
        """Build a successful result for a task.

        성공 결과를 생성합니다.
        """
        return cls(
            task_id=task.task_id,
            status=TaskStatus.COMPLETED,
            output=output or {},
            usage=usage or ModelUsage(),
        )

    @classmethod
    def failed(
        cls,
        task: TaskRequest,
        error: AaestroError | AaestroErrorPayload,
        *,
        usage: ModelUsage | None = None,
    ) -> TaskResult:
        """Build a failed result carrying a typed error payload.

        타입 있는 에러 payload 를 담은 실패 결과를 생성합니다 —
        태스크 실패는 예외가 아니라 결과로 보고한다 (데몬을 죽이지 않는다).
        """
        payload = error.payload() if isinstance(error, AaestroError) else error
        return cls(
            task_id=task.task_id,
            status=TaskStatus.FAILED,
            usage=usage or ModelUsage(),
            error=payload,
        )

    @classmethod
    def canceled(cls, task: TaskRequest, *, usage: ModelUsage | None = None) -> TaskResult:
        """Build a canceled result.

        취소 결과를 생성합니다.
        """
        return cls(
            task_id=task.task_id,
            status=TaskStatus.CANCELED,
            usage=usage or ModelUsage(),
        )


class ComponentHealth(BaseModel):
    """Health of a single dependency (model, mcp:{server}, a2a, modules, memory).

    개별 의존 컴포넌트의 상태.
    """

    model_config = ConfigDict(frozen=True)

    state: HealthState
    detail: str | None = None
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class HealthStatus(BaseModel):
    """Aggregate agent health.

    모델 연결, MCP 세션, 의존 모듈 상태를 종합한 결과.
    """

    model_config = ConfigDict(frozen=True)

    status: HealthState
    components: dict[str, ComponentHealth] = Field(default_factory=dict)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def aggregate(cls, components: dict[str, ComponentHealth]) -> HealthStatus:
        """Derive overall status from component states.

        컴포넌트 상태로부터 전체 상태를 도출합니다 — 하나라도 unhealthy 면
        unhealthy, degraded 가 있으면 degraded.
        """
        states = {c.state for c in components.values()}
        if HealthState.UNHEALTHY in states:
            overall = HealthState.UNHEALTHY
        elif HealthState.DEGRADED in states:
            overall = HealthState.DEGRADED
        else:
            overall = HealthState.HEALTHY
        return cls(status=overall, components=components)


@runtime_checkable
class SecretsProvider(Protocol):
    """Scoped secret access (local > group > global).

    에이전트 코드는 ``os.environ`` 대신 이 계약을 통해 secret 에 접근한다.
    """

    def get(self, key: str) -> str | None:
        """선언된 ``env_allowlist`` 범위 내에서 키를 해석한다."""
        ...


@runtime_checkable
class MemoryAccess(Protocol):
    """Declared memory space access.

    선언된 memory space 에 대한 접근 계약 (구현은 memory 레이어).
    """

    async def search(self, query: str, **kwargs: Any) -> list[Any]:
        """하이브리드 검색 — 접근 가능한 space 로 한정된다."""
        ...

    async def append(self, space: str, **kwargs: Any) -> Any:
        """항목 추가 — rw 권한이 있는 space 만 허용된다.

        ``kwargs`` 는 내용만 싣는다 (``kind`` · ``content`` · ``tags`` · ``importance`` ·
        ``supersedes`` · ``source`` 의 run · task · node). id 와 출처의 신원은 서비스가 발급하고,
        돌려준 항목에 담긴다 (#456).
        """
        ...


@dataclass(frozen=True)
class AgentContext:
    """Runtime context injected into an agent at initialization.

    초기화 시 에이전트에 주입되는 실행 컨텍스트. peers 는 allowlist 기반으로
    주입되며, 에이전트는 peer 주소를 직접 알지 못한다.

    주입된 협력자(secrets/memory/logger)를 담는 컨테이너이므로 검증 대상
    pydantic 계약이 아니라 dataclass 로 둔다.
    """

    agent: str
    agent_version: str
    group: str | None = None
    a2a_port: int | None = None
    peers: tuple[str, ...] = ()
    secrets: SecretsProvider | None = None
    memory: MemoryAccess | None = None
    logger: Any | None = None


class BaseAgent(ABC):
    """에이전트 구현의 기본 계약. agentd 가 이 인터페이스를 Control API 로 서빙한다."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Manifest 의 metadata.name 과 일치해야 한다."""

    @abstractmethod
    def card(self) -> Any:
        """A2A AgentCard. manifest 로부터 자동 생성이 기본."""

    @abstractmethod
    async def initialize(self, ctx: AgentContext) -> None:
        """모듈(promptset/skillset)과 프로토콜(MCP/A2A) 초기화. 실패 시 컨테이너 unhealthy."""

    @abstractmethod
    async def invoke(self, task: TaskRequest) -> TaskResult:
        """단일 태스크 실행. 멱등성 보장 필수 (동일 task_id 재호출 안전)."""

    @abstractmethod
    def stream(self, task: TaskRequest) -> AsyncIterator[TaskEvent]:
        """스트리밍 실행 — 구현은 async generator (``async def`` + ``yield``) 로 작성한다.

        호출자는 ``async for event in agent.stream(task)`` 로 소비한다 (별도 await 없음).
        이벤트 단위: token / tool_call / tool_result / done / error.
        """

    @abstractmethod
    async def health(self) -> HealthStatus:
        """모델 연결, MCP 세션, 의존 모듈 상태 종합."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Graceful shutdown. 진행 중 태스크 drain 후 MCP/A2A 세션 정리."""
