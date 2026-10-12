"""Structured error taxonomy for the framework.

시스템 전반의 구조화 에러 타입과 재시도/서킷브레이커 정책.
카테고리·코드 기반으로 재시도/라우팅/알림 전략을 분기할 수 있게 한다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ErrorCategory(StrEnum):
    """에러 카테고리 — 처리 전략(재시도/라우팅/알림)의 1차 분기 기준."""

    # Temporary — retry 가능
    NETWORK = "network"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"

    # Permanent — retry 무의미
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"

    # Protocol
    A2A = "a2a"
    MCP = "mcp"

    # Model
    MODEL = "model"
    DECISION = "decision"  # 결정 모델 provider (01 Decision Models)

    # System
    RUNTIME = "runtime"
    GRAPH = "graph"
    MODULE = "module"
    MEMORY = "memory"
    STORAGE = "storage"
    CONFIG = "config"
    INTERNAL = "internal"


class ErrorCode(StrEnum):
    """시스템 전반에서 일관되게 사용하는 에러 코드 상수.

    코드는 기계 판독 가능한 식별자다 — 로그의 ``error_code`` 필드와
    ``AaestroErrorPayload.code`` 에 그대로 실린다.
    """

    # Network
    NET_001 = "NET_001"  # Connection refused / DNS 실패
    NET_002 = "NET_002"  # Connection timeout

    # Timeout
    TO_001 = "TO_001"  # Task timeout (TaskConfig.timeout_s 초과)
    TO_002 = "TO_002"  # Tool timeout
    TO_003 = "TO_003"  # Node timeout (orchestrator 기준)
    TO_004 = "TO_004"  # Decision timeout (decision.timeout_s 초과)

    # Model
    LLM_001 = "LLM_001"  # Provider rate limited
    LLM_002 = "LLM_002"  # Context length exceeded
    LLM_003 = "LLM_003"  # Provider server error
    LLM_004 = "LLM_004"  # Invalid/unparseable model response
    LLM_005 = "LLM_005"  # Max turns exceeded

    # A2A
    A2A_001 = "A2A_001"  # Task 제출 실패
    A2A_002 = "A2A_002"  # Peer 도달 불가
    A2A_003 = "A2A_003"  # Task 거부/실패 (callee 측)
    A2A_004 = "A2A_004"  # Connection allowlist 위반
    A2A_005 = "A2A_005"  # 호출 깊이 초과

    # MCP
    MCP_001 = "MCP_001"  # 서버 기동/initialize 실패
    MCP_002 = "MCP_002"  # Tool 미존재
    MCP_003 = "MCP_003"  # Tool 실행 실패
    MCP_004 = "MCP_004"  # Transport 단절

    # Skillset
    SKILL_001 = "SKILL_001"  # Skillset tool 실행 실패 (skill 도메인 예외 wrapping)

    # Decision model (01 Decision Models) — 쓰임 안에서는 전부 unavailable 로 원래 경로
    DEC_001 = "DEC_001"  # 결정 provider 호출 실패 (네트워크/서버) — retryable
    DEC_002 = "DEC_002"  # 결정 provider rate limited — retryable
    DEC_003 = "DEC_003"  # 질문/state 가 provider 한도 초과 — 영구
    DEC_004 = "DEC_004"  # 응답이 선언한 값 집합 밖 / 파싱 불가 — 영구

    # Runtime
    RT_001 = "RT_001"  # 컨테이너 기동 실패
    RT_002 = "RT_002"  # 컨테이너 unhealthy
    RT_003 = "RT_003"  # OOM killed
    RT_004 = "RT_004"  # 이미지 빌드/풀 실패
    RT_005 = "RT_005"  # Drain timeout
    RT_006 = "RT_006"  # 그룹 리소스 quota 초과 — 기동 거부
    RT_007 = "RT_007"  # 불법 lifecycle 상태 전이 (프로그래밍 오류)
    RT_008 = "RT_008"  # 재시작 상한 초과 — Failed 전환
    RT_009 = "RT_009"  # 라우팅 가능한 레플리카 없음
    RT_010 = "RT_010"  # 이미 기동된 레플리카 — 중복 기동 거부 (상태 충돌)
    RT_011 = "RT_011"  # 이미 진행 중인 이미지 빌드 — 중복 요청 거부 (상태 충돌)
    RT_012 = "RT_012"  # 굽지 않았거나 실패했거나 다른 태그로 구워진 이미지 — 배포 거부 (상태 충돌)

    # Graph
    GRAPH_001 = "GRAPH_001"  # 토폴로지 검증 실패
    GRAPH_002 = "GRAPH_002"  # Node 실행 실패 (에이전트 에러 wrapping)
    GRAPH_003 = "GRAPH_003"  # State schema 불일치 / 병합 실패
    GRAPH_004 = "GRAPH_004"  # Max iterations 초과 (mission)
    GRAPH_005 = "GRAPH_005"  # Service iteration 연속 실패 임계 초과 — run 정지
    GRAPH_006 = "GRAPH_006"  # Run 상태가 요청한 조작과 맞지 않음 (상태 충돌)

    # Module
    MOD_001 = "MOD_001"  # 모듈 ref 해석 실패
    MOD_002 = "MOD_002"  # 모듈 버전/의존성 충돌
    MOD_003 = "MOD_003"  # 모듈 스키마(yaml) 검증 실패 / 게시된 모듈의 파일 문제 (로드 시점)
    # Promptset 변수 검증 실패 / 렌더 시점의 템플릿 문제 / decisionset 질문의 state 조각 불일치
    MOD_004 = "MOD_004"

    # Memory
    MEM_001 = "MEM_001"  # Memory space 미선언 / access 거부
    MEM_002 = "MEM_002"  # Memory 저장 실패
    MEM_003 = "MEM_003"  # 인덱싱 실패 누적 / 재인덱싱 필요
    MEM_004 = "MEM_004"  # 검색 실패 / 인덱스 손상

    # Not found
    NF_001 = "NF_001"  # 대상 리소스 미존재 (run, 에이전트 등)

    # Validation
    VAL_001 = "VAL_001"  # 필수 필드 누락
    VAL_002 = "VAL_002"  # 필드 형식 오류 / 렌더할 수 없는 태스크 입력

    # Storage
    ART_001 = "ART_001"  # Artifact 스코프 미선언 / 접근 거부
    ART_002 = "ART_002"  # Artifact quota 초과 — 저장 거부

    STOR_001 = "STOR_001"  # Checkpoint 저장 실패
    STOR_002 = "STOR_002"  # Checkpoint 복원 실패
    STOR_003 = "STOR_003"  # Registry 저장소 오류

    # Config
    CFG_001 = "CFG_001"  # 설정 파싱/검증 실패
    CFG_002 = "CFG_002"  # 그룹 정의 오류 / 스코프 해석 실패

    # Access control — 01 Access Control. 메모리·A2A 거부는 MEM_001·A2A_004 를 쓴다
    ACC_001 = "ACC_001"  # 권한 판정 거부 — 이그레스 목적지·원격 MCP 도구 (forbidden)
    ACC_002 = "ACC_002"  # 판정 불가 — 레지스트리 도달 불가 + 캐시 없음 → 거부 (network, retryable)
    ACC_003 = "ACC_003"  # 부여 거절 — 상한 초과·자기 부여·작업 에이전트의 부여 호출 (forbidden)

    # Internal — 분류되지 않은 프레임워크 내부 실패 (최상위 핸들러 변환 대상)
    INTERNAL_001 = "INTERNAL_001"


class AaestroErrorPayload(BaseModel):
    """Serialized error representation carried by TaskResult and API responses.

    TaskResult / API 응답에 실리는 직렬화 표현.
    """

    model_config = ConfigDict(frozen=True)

    category: ErrorCategory
    code: str
    message: str
    agent: str | None = None
    task_id: str | None = None
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class AaestroError(Exception):
    """시스템 전반의 구조화 에러 타입."""

    def __init__(
        self,
        category: ErrorCategory,
        code: str,
        message: str,
        *,
        agent: str | None = None,
        task_id: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"[{category}:{code}] {message}")
        self.category = category
        self.code = code
        self.message = message
        self.agent = agent
        self.task_id = task_id
        self.retryable = retryable
        self.details = {} if details is None else details

    def payload(self) -> AaestroErrorPayload:
        """Serialize for TaskResult / API responses.

        TaskResult / API 응답에 실리는 직렬화 표현으로 변환합니다.
        """
        return AaestroErrorPayload(
            category=self.category,
            code=self.code,
            message=self.message,
            agent=self.agent,
            task_id=self.task_id,
            retryable=self.retryable,
            details=dict(self.details),
        )

    @classmethod
    def from_payload(cls, payload: AaestroErrorPayload) -> Self:
        """Reconstruct from a serialized payload.

        직렬화된 payload 로부터 에러를 복원합니다 (원격 호출 경계에서 사용).
        """
        return cls(
            category=payload.category,
            code=payload.code,
            message=payload.message,
            agent=payload.agent,
            task_id=payload.task_id,
            retryable=payload.retryable,
            details=dict(payload.details),
        )


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff retry policy.

    지수 백오프 재시도 정책. ``retryable_categories`` 에 속하고
    ``AaestroError.retryable`` 이 True 인 에러만 재시도 대상이다.
    """

    max_attempts: int
    initial_delay_s: float
    max_delay_s: float
    multiplier: float = 2.0
    jitter: bool = True
    retryable_categories: tuple[ErrorCategory, ...] = ()

    def should_retry(self, err: BaseException) -> bool:
        """Decide whether an exception is worth retrying.

        재시도 여부를 판정합니다. ``retryable == False`` 면 카테고리가 목록에
        있어도 즉시 중단합니다 (05-error-handling.md Retry Rules 2).
        """
        if not isinstance(err, AaestroError):
            return False
        if not err.retryable:
            return False
        return err.category in self.retryable_categories

    def delay_for(self, attempt: int) -> float:
        """Compute the backoff delay for a 1-based attempt number.

        1-based 시도 횟수에 대한 백오프 대기 시간(초)을 계산합니다.
        Jitter 는 호출자가 적용합니다 — 계산 자체는 결정적으로 유지해
        테스트가 clock/random 주입 없이 검증할 수 있게 합니다.
        """
        if attempt < 1:
            raise ValueError("attempt must be >= 1")
        delay = self.initial_delay_s * (self.multiplier ** (attempt - 1))
        return min(delay, self.max_delay_s)


NETWORK_RETRY = RetryPolicy(
    max_attempts=3,
    initial_delay_s=1,
    max_delay_s=30,
    retryable_categories=(ErrorCategory.NETWORK, ErrorCategory.TIMEOUT),
)

RATE_LIMIT_RETRY = RetryPolicy(
    max_attempts=5,
    initial_delay_s=10,
    max_delay_s=300,
    retryable_categories=(ErrorCategory.RATE_LIMIT,),
)


class CircuitState(StrEnum):
    """서킷브레이커 상태 — 메트릭 ``aaestro_circuit_state`` 와 대응."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Per-target circuit breaker.

    외부 의존 대상(모델 provider, MCP 서버, A2A peer, Agent Control API)별로
    적용한다. 시간 판정은 ``clock`` 주입으로 테스트에서 결정적으로 만든다.

    Open 상태에서 던질 에러의 카테고리/코드는 **소유자가 주입한다** —
    같은 브레이커가 MCP·A2A·runtime 어디에도 붙기 때문에 브레이커가 임의로
    코드를 정하면 코드 기반 라우팅이 어긋난다.
    """

    max_failures: int = 5
    reset_timeout_s: float = 60.0
    target: str = "unknown"
    open_category: ErrorCategory = ErrorCategory.INTERNAL
    open_code: str = ErrorCode.INTERNAL_001
    clock: Callable[[], float] = field(default_factory=lambda: _monotonic)
    # 상태 전이 관찰자 — core 는 관측 계층에 의존하지 않으므로 소유자가 주입한다
    on_transition: Callable[[CircuitState], None] | None = None

    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)

    @property
    def state(self) -> CircuitState:
        """현재 상태 — reset timeout 경과 시 half-open 으로 전이한다."""
        if (
            self._state is CircuitState.OPEN
            and self._opened_at is not None
            and self.clock() - self._opened_at >= self.reset_timeout_s
        ):
            self._transition(CircuitState.HALF_OPEN)
        return self._state

    def can_attempt(self) -> bool:
        """호출을 시도해도 되는지 — open 상태에서만 False."""
        return self.state is not CircuitState.OPEN

    def record_success(self) -> None:
        """성공 기록 — 실패 카운터를 리셋하고 closed 로 복귀한다."""
        self._failures = 0
        self._opened_at = None
        self._transition(CircuitState.CLOSED)

    def record_failure(self) -> None:
        """실패 기록 — 임계 도달 시 open 으로 전이한다."""
        self._failures += 1
        if self._failures >= self.max_failures:
            self._opened_at = self.clock()
            self._transition(CircuitState.OPEN)

    def _transition(self, state: CircuitState) -> None:
        """상태를 바꾸고, 실제로 바뀐 경우에만 관찰자에게 알린다."""
        if self._state is state:
            return
        self._state = state
        if self.on_transition is not None:
            self.on_transition(state)

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Run a call under the breaker.

        서킷브레이커 보호 하에 호출을 실행합니다.

        Raises:
            AaestroError: ``open_category``/``open_code`` if the circuit is open.
        """
        if not self.can_attempt():
            raise AaestroError(
                category=self.open_category,
                code=self.open_code,
                message="circuit open",
                retryable=True,
                details={"target": self.target},
            )
        try:
            result = await fn()
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result


def _monotonic() -> float:
    """단조 시계 — 기본 clock. 테스트는 ``clock`` 주입으로 대체한다."""
    import time

    return time.monotonic()
