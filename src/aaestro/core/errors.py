"""Structured error taxonomy for the framework.

시스템 전반의 구조화 에러 타입과 재시도/서킷브레이커 정책.
카테고리·코드 기반으로 재시도/라우팅/알림 전략을 분기할 수 있게 한다.
"""

from __future__ import annotations

import unicodedata
from bisect import bisect_left
from collections.abc import Awaitable, Callable, Hashable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import accumulate
from typing import TYPE_CHECKING, Any, NoReturn, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from fastapi.exceptions import RequestValidationError

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
    MOD_003 = "MOD_003"  # 모듈 스키마(yaml) 검증 실패
    MOD_004 = "MOD_004"  # Promptset 변수 검증 실패 / decisionset 질문의 state 조각 불일치

    # Memory
    MEM_001 = "MEM_001"  # Memory space 미선언 / access 거부
    MEM_002 = "MEM_002"  # Memory 저장 실패
    MEM_003 = "MEM_003"  # 인덱싱 실패 누적 / 재인덱싱 필요
    MEM_004 = "MEM_004"  # 검색 실패 / 인덱스 손상

    # Not found
    NF_001 = "NF_001"  # 대상 리소스 미존재 (run, 에이전트 등)

    # Validation
    VAL_001 = "VAL_001"  # 필수 필드 누락
    VAL_002 = "VAL_002"  # 필드 형식 오류

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


REDACTED = "<redacted>"
MIN_REDACT_LENGTH = 3
"""이보다 짧은 잎 값 (``0``, ``a``) 과 bool · None 은 가리지 않는다 — 고정 문구가 누더기가 된다."""
MIN_SHARED_LENGTH = 8
"""메시지와 입력이 이 길이 이상 겹치면 값의 **조각** 이 되비친 것으로 보고 가린다.

잎 값 전체만 치환하면 조각은 남는다: ``urlsplit`` 은 URL 의 netloc 만, ``ipaddress`` 는 대괄호
안만 메시지에 싣는다. 8자 미만의 겹침은 건드리지 않는다 — 영어 고정 문구와 값이 우연히 나눠
갖는 짧은 조각 (``must``, ``http``) 까지 가리면 메시지를 읽을 수 없다."""
MAX_LABEL_LENGTH = 64
"""필드 경로의 구성 요소 · 키 이름 하나의 상한."""
MAX_FIELD_LENGTH = 512
"""필드 경로 전체의 상한."""
MAX_PROBLEM_LENGTH = 512
"""사유 한 건의 상한 — validator 가 키 이름 등을 보간해도 한 줄이 무한히 길어지지 않는다."""
TRUNCATION_MARK = "…"
_UNPRINTABLE = "?"
MAX_SCANNED_LENGTH = 8 * MAX_PROBLEM_LENGTH
"""가림이 훑는 메시지 길이의 상한 — 거의 전부가 가려져 결과가 짧은 메시지도 여기서 끝난다."""
_LEAF_SEPARATOR = "\x00\x1f\x00"
"""잎들을 한 덩어리로 이어 찾을 때의 구분자 — 메시지에 나올 일이 없는 제어문자열."""


def printable_label(text: str, limit: int = MAX_LABEL_LENGTH) -> str:
    """Make a key name safe to print: printable characters only, bounded length.

    키 이름 (모르는 키 · 매핑 키 · 중복 키) 은 선언의 구조라 **의도적으로** 내보낸다. 다만 그대로
    내보내면 1만 자 키가 로그 한 줄을 채우고, 제어문자 · ANSI 이스케이프가 터미널을 조작한다.
    """
    cleaned = "".join(char if char.isprintable() else _UNPRINTABLE for char in text[: limit + 1])
    if len(cleaned) > limit:
        return cleaned[:limit] + TRUNCATION_MARK
    return cleaned


def repeated_positions(values: Sequence[Hashable]) -> str | None:
    """Where a sequence repeats a value — ``"1 and 3"``, ``"0, 2 and 5; 1 and 4"`` — or ``None``.

    "중복" 을 거절하는 메시지가 **어느 것** 인지 말하게 한다. 값은 선언의 것이라 싣지 않지만
    (#386) 위치는 입력이 아니다 — 노드 수십 개 그래프에서 "duplicate node id" 만으로는 운영자가
    전부 눈으로 대조해야 한다.
    """
    positions: dict[Hashable, list[int]] = {}
    for index, value in enumerate(values):
        positions.setdefault(value, []).append(index)
    groups = [found for found in positions.values() if len(found) > 1]
    if not groups:
        return None
    return "; ".join(
        ", ".join(map(str, found[:-1])) + f" and {found[-1]}" for found in sorted(groups)
    )


def _field_path(loc: Any) -> str:
    """에러 위치를 점 경로로 — 구성 요소마다, 그리고 전체에 길이 상한을 둔다."""
    return printable_label(
        ".".join(printable_label(str(part)) for part in loc or ()), MAX_FIELD_LENGTH
    )


def _leaves(value: Any) -> Iterator[str]:
    """입력에서 메시지에 되비칠 수 있는 문자열들 — 스칼라와 컨테이너의 잎 값 (매핑 키 제외)."""
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        yield value
        escaped = repr(value)[1:-1]  # 이스케이프된 형태로 보간되는 경우
        if escaped != value:
            yield escaped
        return
    if isinstance(value, Mapping):
        # 키는 선언의 구조 (필드 이름) 라 가리지 않는다 — 가리면 "orchestrator.<redacted>" 가 된다
        for item in value.values():
            yield from _leaves(item)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _leaves(item)
        return
    yield str(value)


def _fold(text: str) -> str:
    """비교용 정규화 — 호환 분해 (NFKD) 와 대소문자 접기.

    validator 는 값을 정규화해 되비춘다: 소문자로 바꾸거나, 파이썬 식별자처럼 NFKC 로 (전각
    ``ｓｋ`` 가 ``sk`` 로 나온다). 정규화한 꼴끼리 비교해야 그 되비침이 입력과 겹친다. 합성형
    (NFKC) 이 아니라 분해형을 쓰는 이유: 분해는 글자 단위로 해도 문자열 전체로 해도 결과가 같아
    (결합 문자의 재배열만 예외) 메시지를 글자 단위로 접으면서 원문 위치를 유지할 수 있다.
    """
    return unicodedata.normalize("NFKD", unicodedata.normalize("NFKD", text).casefold())


def _shared_prefix_length(left: str, left_at: int, right: str, right_at: int) -> int:
    """``left[left_at:]`` 와 ``right[right_at:]`` 의 공통 접두 길이 — 조각 비교로 이분 탐색한다.

    글자 단위 파이썬 루프는 값 전체가 되비친 긴 메시지 (1만 자 값) 에서 느리다.
    """
    low, high = 0, min(len(left) - left_at, len(right) - right_at)
    while low < high:
        middle = (low + high + 1) // 2
        if left[left_at : left_at + middle] == right[right_at : right_at + middle]:
            low = middle
        else:
            high = middle - 1
    return low


def _find_shared(haystack: str, piece: str) -> int:
    """``haystack`` 에서 조각의 위치 — 가림 비용의 단위다 (호출 횟수 × 입력 크기)."""
    return haystack.find(piece)


def _short_leaf_length(message: str, at: int, short_leaves: frozenset[str]) -> int:
    """``at`` 에서 시작하는 짧은 잎 값 전체의 길이 — 없으면 0. 긴 것부터 본다."""
    for length in range(MIN_SHARED_LENGTH - 1, MIN_REDACT_LENGTH - 1, -1):
        if message[at : at + length] in short_leaves:
            return length
    return 0


def _redact(message: str, rejected: Any) -> str:
    """메시지에 되비친 거절된 입력을 ``<redacted>`` 로 — 값 전체와, 값의 긴 조각 모두.

    두 겹이다. (1) 짧은 잎 값 (:data:`MIN_REDACT_LENGTH` 자 이상) 이 통째로 나타난 자리.
    (2) 메시지와 잎이 :data:`MIN_SHARED_LENGTH` 자 이상 나눠 갖는 조각 — 정규화한 꼴로 본다
    (:func:`_fold`). 남는 겹침은 언제나 :data:`MIN_SHARED_LENGTH` 자 미만이다: 그 이상인 조각은
    그 안의 모든 고정 길이 조각이 입력에 있으므로 다음 위치에서 다시 잡힌다.

    **비용은 입력 크기에 선형으로 묶인다.** 메시지를 왼쪽부터 한 번 훑으며 결과를 만들고, 결과가
    :data:`MAX_PROBLEM_LENGTH` 자를 넘으면 멈춘다 — 그 뒤는 어차피 잘린다. 겹치지 않는 자리는
    결과에 한 글자씩 보태므로 입력을 찾는 횟수가 그 상한으로 묶인다 (긴 메시지 × 큰 입력의 2차
    비용이 없다). 가린 뒤에 자르는 순서라서 절단 지점에서 값 조각이 짧게 남아 가림을 피하지도
    않는다.
    """
    leaves = {leaf for leaf in _leaves(rejected) if len(leaf) >= MIN_REDACT_LENGTH}
    if not leaves or not message:
        return message
    short_leaves = frozenset(leaf for leaf in leaves if len(leaf) < MIN_SHARED_LENGTH)
    long_leaves = (leaf for leaf in leaves if len(leaf) >= MIN_SHARED_LENGTH)
    haystack = _fold(_LEAF_SEPARATOR.join(long_leaves))

    # 메시지는 글자 단위로 접는다 — 접힌 꼴의 위치와 원문 위치를 서로 되짚기 위해서다
    window = message[:MAX_SCANNED_LENGTH]
    folded_chars = [_fold(char) for char in window]
    starts = list(accumulate((len(piece) for piece in folded_chars), initial=0))
    folded = "".join(folded_chars)

    pieces: list[str] = []
    emitted = at = 0
    hidden = False
    while at < len(window) and emitted <= MAX_PROBLEM_LENGTH:
        end = at
        probe = folded[starts[at] : starts[at] + MIN_SHARED_LENGTH]
        if haystack and len(probe) == MIN_SHARED_LENGTH:
            found = _find_shared(haystack, probe)
            if found >= 0:
                shared = _shared_prefix_length(folded, starts[at], haystack, found)
                # 겹침이 원문 글자의 중간에서 끝나면 그 글자까지 가린다 (과잉은 허용, 미가림은 불가)
                end = bisect_left(starts, starts[at] + shared)
        if end == at:
            end = at + _short_leaf_length(window, at, short_leaves)
        if end > at:
            if not hidden:  # 잇닿은 가림은 표시 하나로
                pieces.append(REDACTED)
                emitted += len(REDACTED)
            hidden = True
            at = end
        else:
            pieces.append(window[at])
            emitted += 1
            hidden = False
            at += 1
    return "".join(pieces)


def field_problems(err: ValidationError | RequestValidationError) -> list[dict[str, str]]:
    """Reduce a validation error to ``{field, problem}`` entries.

    검증 에러를 필드 경로와 사유만으로 줄인다. ``errors()`` 의 ``input`` / ``ctx`` 는 **입력 값
    자체**를 싣는데, 모르는 키의 값이 비밀(``env: {KEY: 값}``)인 경우가 흔해서 내보내면 안 된다.
    메시지도 믿지 않는다: 커스텀 validator 가 ``f"bad: {value!r}"`` 처럼 값을 보간하면 ``msg`` 로
    나간다. validator 하나씩 고치면 다음에 추가되는 validator 에서 되돌아오므로, 에러가 가진
    **입력 값이 (전체든 조각이든) 메시지에 나타나면 가리는** 성질로 막는다 (입력은 가리는 데만
    쓰고 싣지 않는다). 키 이름은 ``field`` 로 나간다 — 구조이므로 의도된 노출이고, 길이와 문자만
    정제한다 (:func:`printable_label`).
    """
    entries = (
        err.errors(include_url=False)
        if isinstance(err, ValidationError)
        else err.errors()  # FastAPI 의 RequestValidationError — pydantic 에러 dict 목록
    )
    return [
        {
            "field": _field_path(e.get("loc")),
            "problem": printable_label(
                _redact(str(e.get("msg", "")), e.get("input")), MAX_PROBLEM_LENGTH
            ),
        }
        for e in entries
    ]


def raise_detached(error: AaestroError) -> NoReturn:
    """Raise a structured error converted from a cause that echoes its input.

    입력을 되비추는 원인 예외 (pydantic ``ValidationError``, YAML 의 ``ScannerError`` 등) 를
    구조화 에러로 바꿔 던진다. 07 의 "원인 체인 보존" 의 **의도된 예외**다:
    ``ValidationError`` 의 ``str()`` / repr 은 ``input_value=…`` 로 거절된 **입력 값** 을 싣는데,
    그것이 비밀(``env: {KEY: 값}``)일 수 있고, 체인이 이어져 있으면 ``exc_info`` 로깅이나 처리되지
    않은 traceback 이 값을 그대로 출력한다. 원인 정보는 ``details.errors`` 의 ``{field, problem}``
    으로 이미 옮겨졌다. 호출부가 ``from err`` 를 쓰지 못하도록 ``from None`` 을 이 한 곳에 둔다.

    ``from None`` 은 출력만 억제하고 ``__context__`` 에는 원인이 남는다 — 예외 수집기가
    ``__context__`` 를 직접 따라가면 값이 나간다. 그래서 던진 직후 잡아 ``__context__`` 를 지우고
    다시 던진다 (bare ``raise`` 는 문맥을 다시 잇지 않는다).
    """
    try:
        raise error from None
    except AaestroError as detached:
        detached.__context__ = None
        raise
