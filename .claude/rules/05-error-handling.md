# Error Handling and Monitoring Rules

## Error Handling Principles

### Core Principles

1. **Fail Gracefully**
   - 태스크 실패가 데몬/오케스트레이터를 죽이지 않는다 — 최상위 핸들러에서 수습
   - 에이전트 실패가 graph state 를 오염시키지 않는다 — checkpoint 경계에서 격리
   - 에러를 삼키지 않는다 — 처리하거나 전파하거나, 둘 중 하나

2. **Error Context**
   - 예외는 `raise ... from err` 로 원인 체인 보존
   - 관련 정보 포함 (agent, task_id, run_id, tool, mcp_server 등)

3. **Typed Errors**
   - 카테고리 기반 처리(재시도/라우팅/알림)가 가능하도록 구조화 에러 사용
   - 기계 판독 가능한 error code 포함

## The `AaestroError` Type

```python
class ErrorCategory(StrEnum):
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
    DECISION = "decision"      # 결정 모델 provider (01 Decision Models)

    # System
    RUNTIME = "runtime"        # 컨테이너/Docker
    GRAPH = "graph"            # 토폴로지/state
    MODULE = "module"          # skillset/promptset/memoryset/registry
    MEMORY = "memory"          # Memory Service / 인덱스
    STORAGE = "storage"
    CONFIG = "config"
    INTERNAL = "internal"


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
        self.details = details or {}

    def payload(self) -> AaestroErrorPayload:
        """TaskResult / API 응답에 실리는 직렬화 표현."""
        ...
```

- 원인 예외는 `raise AaestroError(...) from err` 로 연결 — `err` 필드 별도 보관 불필요
- 에러 매칭은 `except AaestroError as e: if e.category is ...` 또는 code 비교로

## Layer Rules — When to Use `AaestroError` vs Plain Exceptions

모든 에러 생성을 일률적으로 구조화하지 않고, 레이어별 규칙을 분리한다.

### MUST — `AaestroError` 로 변환해야 하는 boundary

재시도/라우팅/알림 전략이 카테고리·코드에 의존하는 경계에서는 반드시 `AaestroError` 로
변환해 전파한다.

| Boundary | Categories |
|---|---|
| `runtime/` 의 컨테이너 제어 결과 (start/stop/health) | `RUNTIME`, `TIMEOUT` |
| `runtime/control.py` 의 Agent Control API 호출 결과 | `NETWORK`, `TIMEOUT`, `RUNTIME` |
| `protocols/a2a/` 의 원격 호출 결과 | `A2A`, `NETWORK`, `TIMEOUT` |
| `protocols/mcp/` 의 세션/tool 호출 결과 | `MCP`, `TIMEOUT` |
| `agentd/executor.py` 의 모델 호출 결과 | `MODEL`, `RATE_LIMIT`, `TIMEOUT`, `NETWORK` |
| `decision/providers/` 의 결정 호출 결과 | `DECISION`, `RATE_LIMIT`, `TIMEOUT` |
| `orchestrator/` 의 node 실행/state 병합 결과 | `GRAPH` |
| `modules/registry.py` 의 ref 해석/로드 결과 | `MODULE`, `CONFIG` |
| `orchestrator/checkpoint.py` 의 저장/복원 결과 | `STORAGE` |
| `memory/` 의 append/search/recall 결과 | `MEMORY`, `STORAGE` |

### MAY — plain exception 유지가 적절한 레이어

- **순수 유틸/헬퍼 함수**: `ValueError`, `KeyError` 등 표준 예외로 충분.
  boundary 를 통과할 때 상위에서 변환된다
- **pydantic 검증**: `ValidationError` 그대로 두고, boundary(배포 검증, API 입력)에서
  `VALIDATION` 카테고리로 변환
- **skill 구현 내부**: skill 은 도메인 예외를 그대로 던진다 —
  agentd 의 tool 실행 boundary 가 skillset tool 은 `SKILL_001`, MCP tool 은
  `MCP_003` 으로 변환·기록

### 예시 — boundary 변환 패턴

```python
# protocols/mcp/client.py (boundary — AaestroError 로 변환)
async def call_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
    try:
        return await self._session.call_tool(name, args)
    except McpError as err:
        raise AaestroError(
            category=ErrorCategory.MCP,
            code="MCP_003",
            message=f"tool call failed: {name}",
            agent=self.agent_name,
            retryable=False,
        ) from err
    except ConnectionError as err:
        raise AaestroError(
            category=ErrorCategory.MCP,
            code="MCP_004",
            message="mcp transport disconnected",
            agent=self.agent_name,
            retryable=True,
        ) from err
```

## Error Codes

시스템 전반에서 일관된 코드를 사용한다:

```
NET_001: Connection refused / DNS 실패
NET_002: Connection timeout
TO_001:  Task timeout (TaskConfig.timeout_s 초과)
TO_002:  Tool timeout
TO_003:  Node timeout (orchestrator 기준)
TO_004:  Decision timeout (decision.timeout_s 초과)

LLM_001: Provider rate limited
LLM_002: Context length exceeded
LLM_003: Provider server error
LLM_004: Invalid/unparseable model response
LLM_005: Max turns exceeded

A2A_001: Task 제출 실패
A2A_002: Peer 도달 불가
A2A_003: Task 거부/실패 (callee 측)
A2A_004: Connection allowlist 위반
A2A_005: 호출 깊이 초과

MCP_001: 서버 기동/initialize 실패
MCP_002: Tool 미존재
MCP_003: Tool 실행 실패
MCP_004: Transport 단절

SKILL_001: Skillset tool 실행 실패 (category: module — skill 도메인 예외 wrapping.
           timeout 은 TO_002 사용)

DEC_001: 결정 provider 호출 실패 (네트워크/서버 오류) — retryable
DEC_002: 결정 provider rate limited (category: rate_limit) — retryable
DEC_003: 질문/state 가 provider 한도 초과 (입력 길이, 보기·등급 수) — 영구
DEC_004: 응답이 선언한 값 집합 밖이거나 파싱 불가 — 영구 (provider 어댑터 boundary 가 변환)
         결정 호출의 timeout 은 TO_004. 쓰임 안에서는 DEC_*/TO_004 모두 "unavailable" 로 취급해
         원래 경로로 간다 (01 Decision Models 2) — 결정 실패가 태스크를 실패시키지 않는다

RT_001:  컨테이너 기동 실패 / 재부착 시 격리 · 경화 불일치 (details.kind: network | hardening)
RT_002:  컨테이너 unhealthy
RT_003:  OOM killed
RT_004:  이미지 빌드/풀 실패
RT_005:  Drain timeout
RT_006:  그룹 리소스 quota 초과 — 기동 거부
RT_007:  불법 lifecycle 상태 전이 (프로그래밍 오류)
RT_008:  재시작 상한 초과 — Failed 전환
RT_009:  라우팅 가능한 레플리카 없음
RT_010:  이미 기동된 레플리카 — 중복 기동 거부 (상태 충돌, HTTP 409)
RT_011:  이미 진행 중인 이미지 빌드 — 중복 요청 거부 (상태 충돌, HTTP 409)
RT_012:  굽지 않았거나 빌드가 실패했거나 기록의 태그가 띄울 태그와 다른 커스텀 이미지 — 배포 거부 (상태 충돌, HTTP 409)

GRAPH_001: 토폴로지 검증 실패
GRAPH_002: Node 실행 실패 (에이전트 에러 wrapping)
GRAPH_003: State schema 불일치 / 병합 실패
GRAPH_004: Max iterations 초과 (mission)
GRAPH_005: Service iteration 연속 실패 임계 초과 — run 정지
GRAPH_006: Run 상태가 요청한 조작과 맞지 않음 (상태 충돌, HTTP 409)

MOD_001: 모듈 ref 해석 실패
MOD_002: 모듈 버전/의존성 충돌 / 에이전트 ↔ promptset 조합 불일치 (노드 · `default` · `system` 템플릿 부재)
MOD_003: 모듈 스키마(yaml) 검증 실패 / 게시된 모듈의 파일 문제 — 선언된 템플릿 파일이 없거나
         읽히지 않거나, 문법이 틀리거나, 경계를 우회하는 구문을 쓴다 (로드 시점, 04 Promptset Rules 2) / `system` 템플릿의 변수 선언 · 빈 렌더 (agentd 기동, 04 Promptset Rules 6)
MOD_004: Promptset 변수 검증 실패 / 렌더 시점의 템플릿 문제 (거절되는 구문, 없는 필터 · 전역,
         입력이 잘리지 않은 렌더의 표현식 평가 실패 — 예외 종류 이름만 싣는다) / decisionset 질문의 state 조각 불일치

MEM_001: Memory space 미선언 / access 거부
MEM_002: Memory 저장 실패 — 저장소의 쓰기 쿼리 실패 포함. 연결 · 잠금류 일시 장애는 retryable
MEM_003: 인덱싱 실패 누적 / 재인덱싱 필요 / 임베딩 실패 (색인 · 질의 — provider 응답을 해석하지 못함 포함)
MEM_004: 조회 · 검색 실패 / 인덱스 손상 — 저장소의 읽기 쿼리 실패와 손상된 행 포함 (쓰기 실패는 MEM_002).
         연결 · 잠금류 일시 장애는 retryable, 손상된 행 · 무결성 · 프로그래밍 오류는 아니다

VAL_001: 필수 필드 누락
VAL_002: 필드 형식 오류 / 렌더할 수 없는 태스크 입력 — 너무 깊은 중첩, 출력할 수 없는 수,
         결과가 상한을 넘을 연산, 템플릿의 글이 반복문으로 부풀어 렌더 상한을 넘은 경우, 입력이 잘린
         렌더에서의 표현식 실패 (04 Promptset Rules 2 — 값 출력이 예산을 넘는 것은 에러가 아니라
         생략이다). 입력 없는 상수 연산이 한도를 넘는 경우도 이 코드로 나간다 (계산 전에 가로채는
         자리가 같다)

NF_001: 대상 리소스 미존재 (run, 에이전트 등). 어떤 라우트에도 맞지 않는 경로의 404 는 NF_001 이
        아니다 — 코드 없는 기본 응답 그대로다 ("그 표면이 없다", UI · CLI · runtime 이 이것으로 가른다).
        같은 프레임워크 거절 중 틀린 메서드 405 와 FastAPI 가 해독하지 못한 본문 400 (깨진 UTF-8 —
        `FRAMEWORK_BODY_PARSE_DETAIL` 로 판별, 앱이 낸 다른 400 은 그대로) 은 VAL_002
        (`http_errors.unroutable_problem`, #462). 셋 다 자격이 있는 호출자만 본다 — 자격이 없으면
        라우팅 전에 401 이다

ART_001: Artifact 스코프 미선언 / 접근 거부
ART_002: Artifact quota 초과 — 저장 거부

STOR_001: Checkpoint 저장 실패
STOR_002: Checkpoint 복원 실패
STOR_003: Registry 저장소 오류

CFG_001: 설정 파싱/검증 실패
CFG_002: 그룹 정의 오류 / 스코프 해석 실패 (secrets 미해석 포함)

ACC_001: 권한 판정 거부 — 이그레스 목적지·원격 MCP 도구 (category: forbidden, HTTP 403).
         메모리와 A2A 거부는 각 도메인 코드(MEM_001, A2A_004)를 쓴다
ACC_002: 권한 판정 불가 — 레지스트리 도달 불가 + 캐시 없음 → 거부 (category: network, retryable, HTTP 503)
ACC_003: 부여 거절 — 확장 상한 초과, 권한 에이전트의 자기 부여·자기 상한 변경, 작업 에이전트의
         부여 API 호출 (category: forbidden, HTTP 403)

INTERNAL_001: 예상치 못한 내부 오류 — 최상위 boundary 가 변환
```

## Retry Logic

### Retry Policy

```python
@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    initial_delay_s: float
    max_delay_s: float
    multiplier: float = 2.0
    jitter: bool = True
    retryable_categories: tuple[ErrorCategory, ...] = ()


NETWORK_RETRY = RetryPolicy(
    max_attempts=3, initial_delay_s=1, max_delay_s=30,
    retryable_categories=(ErrorCategory.NETWORK, ErrorCategory.TIMEOUT),
)

RATE_LIMIT_RETRY = RetryPolicy(
    max_attempts=5, initial_delay_s=10, max_delay_s=300,
    retryable_categories=(ErrorCategory.RATE_LIMIT,),
)

# 결정 호출 전용 — 총 대기가 decision.timeout_s(기본 5초) 안에 들어와야 한다.
# rate limit 도 여기서 한 번만 다시 두드리고, 소진하면 unavailable 로 원래 경로 (01 Decision Models 2)
DECISION_RETRY = RetryPolicy(
    max_attempts=2, initial_delay_s=0.5, max_delay_s=1,
    retryable_categories=(ErrorCategory.NETWORK, ErrorCategory.TIMEOUT, ErrorCategory.RATE_LIMIT),
)
```

### Retry Rules

1. 구현은 `tenacity` 사용 — 수제 재시도 루프 금지
2. `AaestroError.retryable == False` 면 즉시 중단 — 카테고리가 retryable 목록에 있어도
3. 재시도 전 대기 중에도 cancellation 존중 (`asyncio.CancelledError` 즉시 전파)
4. 재시도 시 WARN 로그: `attempt`, `max_attempts`, `delay_ms` 필드 필수
5. **부수효과가 있는 호출**(external tool)은 멱등성이 확보된 경우에만 자동 재시도

### Retry Layering — 이중 재시도 방지

재시도는 **한 계층에서만** 수행한다:

| 호출 | 재시도 주체 |
|---|---|
| 모델 API 호출 | agentd (provider SDK 재시도는 비활성화) — 재시도 소진 뒤 선언된 fallback 모델로의 전환도 agentd (02 Model Resilience Declaration) |
| 결정 모델 호출 | agentd — `DECISION_RETRY` (network·timeout·rate limit 모두 1회 재시도, 총 대기 < `decision.timeout_s`). 소진하면 `unavailable` 로 원래 경로 — RATE_LIMIT_RETRY 의 수십 초 백오프는 판정에 맞지 않는다 |
| MCP tool 호출 | agentd — 단 `MCP_004` (transport) 는 재연결 후 1회만 |
| A2A 호출 | caller 에이전트 |
| Node 실행 전체 | orchestrator — node 별 `retry` 설정 시에만, 에이전트 내부 재시도와 중복 주의 |
| 컨테이너 재시작 | runtime (lifecycle 정책) |

### Circuit Breaker

외부 의존 대상별로 circuit breaker 를 적용한다:

- 모델 (provider/name 별 — fallback 포함)
- 결정 provider (에이전트별) — open 이면 호출 없이 `unavailable`: 장애 중 판정마다 timeout 을
  기다리면 태스크가 결정 수만큼 느려진다
- MCP external/sidecar 서버 (서버별)
- A2A peer (edge 별)
- Agent Control API (runtime → 에이전트, 에이전트별)

```python
breaker = CircuitBreaker(max_failures=5, reset_timeout_s=60)

async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
    if not breaker.can_attempt():
        raise AaestroError(category=..., code=..., message="circuit open",
                           retryable=True)
    ...
```

Open 상태 전환/복구 시 WARN 로그 + `aaestro_circuit_state` 메트릭 갱신.

## Logging

### Structured Logging

`structlog` 기반 구조화 로깅을 사용한다.
로그 메시지는 **반드시 영어**로 작성한다. (주석·문서는 한국어 허용, 로그 msg 문자열은 영어)

```python
# Good
log.info(
    "agent task completed",
    agent="researcher",
    task_id=task.task_id,
    run_id=task.run_id,
    duration_ms=elapsed_ms,
)

log.error(
    "mcp server initialization failed",
    agent="researcher",
    mcp_server="filesystem",
    error_code="MCP_001",
    exc_info=err,
)

# Bad — 한국어 로그 메시지 금지
log.error("MCP 서버 기동 실패", ...)
# Bad — f-string 보간 금지, 필드로 분리
log.info(f"task {task_id} done in {elapsed}ms")
```

### Log Levels

| Level | 사용 시점 | 예시 시나리오 |
|-------|-----------|--------------|
| **DEBUG** | 개발/트러블슈팅용 내부 상세 | tool 호출 인자, 템플릿 렌더 결과 크기, edge 조건 평가값 |
| **INFO**  | 운영자가 확인하고 싶은 정상 마일스톤 | run 시작/완료, 에이전트 Ready, 그래프 배포 완료, checkpoint 저장 |
| **WARN**  | 예상 외지만 gracefully 처리됨 | 재시도, MCP 재연결, optional 서버 기동 실패, drain timeout 임박 |
| **ERROR** | 작업 실패 — 다른 run 은 계속 가능 | 태스크 실패, node 실패, tool 실패, checkpoint 저장 실패 |
| **FATAL** | 복구 불가 — 프로세스 종료 | 설정 파싱 실패, Docker daemon 접속 불가 (기동 시) |

**빠른 판단 기준:**
- 운영자가 알림을 받아야 하면: **ERROR** 이상
- 처리됐지만 운영자가 알아야 하면: **WARN**
- 정상 동작 확인: **INFO**
- 개발자 트레이싱: **DEBUG**

### Field Naming Conventions

모든 구조화 필드 키는 **snake_case**. 아래 표준 이름을 반드시 사용한다.

| Field Key        | Type   | 설명 |
|-----------------|--------|------|
| `agent`         | str    | 에이전트 이름 (manifest metadata.name) |
| `agent_version` | str    | 에이전트 버전 |
| `group`         | str    | 에이전트 소속 그룹 (미소속 시 생략) |
| `graph`         | str    | 그래프 이름 |
| `run_id`        | str    | Graph run UUID |
| `task_id`       | str    | 태스크 UUID |
| `node_id`       | str    | 그래프 노드 id |
| `a2a_caller`    | str    | A2A 호출자 에이전트 |
| `a2a_callee`    | str    | A2A 피호출자 에이전트 |
| `a2a_task_id`   | str    | A2A task id |
| `mcp_server`    | str    | MCP 서버 이름 |
| `tool`          | str    | tool 이름 (네임스페이스 포함) |
| `skillset`      | str    | 스킬셋 ref |
| `promptset`     | str    | 프롬프트셋 ref |
| `module_ref`    | str    | 모듈 참조 문자열 (`type/name@version`) |
| `memory_space`  | str    | 메모리 space 별칭/이름 |
| `model`         | str    | 모델 이름 |
| `provider`      | str    | 모델 provider |
| `container_id`  | str    | Docker 컨테이너 id (short) |
| `image`         | str    | 이미지 태그 |
| `error_code`    | str    | 에러 코드 상수 (예: `"MCP_003"`) |
| `status`        | str    | 결과 상태 (`completed`/`failed`/...) |
| `duration_ms`   | int    | 경과 시간 (밀리초) |
| `delay_ms`      | int    | 재시도 backoff 대기 (밀리초) |
| `attempt`       | int    | 현재 시도 횟수 (1-based) |
| `max_attempts`  | int    | 최대 시도 횟수 |
| `input_tokens`  | int    | 입력 토큰 수 |
| `output_tokens` | int    | 출력 토큰 수 |
| `turn`          | int    | tool loop 회차 |
| `iteration`     | int    | Service run 의 iteration 회차 |
| `mode`          | str    | Run 모드 (`mission`/`service`/`direct`) |
| `port`          | int    | 포트 |
| `resource`      | str    | 권한 자원 종류 (`memory`/`egress`/`mcp_tool`/`a2a`, 부여 거절만 `unknown` — 아래 메트릭) |
| `target`        | str    | 권한 대상 (space id, 목적지 host, `server/tool`, 피호출자, 본문을 읽기 전의 부여 거절은 `unknown` — `resource` 와 같다) |
| `decision`      | str    | 판정 결과 (`allow`/`deny`) |
| `decision_source` | str  | 판정 출처 (`fresh`/`cache`/`unreachable`, 레지스트리에 묻지 않는 토큰 모드의 메모리 접근은 `token`) |
| `grant_id`      | str    | 부여 기록 id |
| `decided_by`    | str    | 결정 주체 (`declaration`/`operator`/권한 에이전트 이름) |
| `decision_use`  | str    | 결정 모델 쓰임 (`recall_filter`/`input_screen`/`tool_gate`/`node`/`compaction`) |
| `question`      | str    | decisionset 질문 참조 (`alias.question`) |
| `band`          | str    | 판정 구간 (`act`/`uncertain`/`reject`) |
| `probability`   | float  | 판정 확률 (0.0~1.0) |

**규칙:**
- 표에 없는 컴포넌트 특화 키는 snake_case 로 추가 가능 (예: `checkpoint_id`, `edge`)
- 의미 중복 키 생성 금지 (예: `"agent_name"` 대신 `"agent"`)
- 시간 값은 밀리초 int, 키는 `duration_ms` / `delay_ms`
- 불리언 플래그는 긍정형 (예: `retryable`, `is_replica`)
- secrets/토큰 값은 절대 로그 금지 — structlog processor 마스킹 적용

### Per-Component Required Fields

| 컴포넌트 | 모든 로그에 필수 |
|---|---|
| `orchestrator/` | `graph`, `run_id` (+node 실행 로그는 `node_id`, `agent` / service run 로그는 `iteration`) |
| `runtime/` | `agent` (+컨테이너 조작 로그는 `container_id`, `image`) |
| `protocols/a2a/` | `a2a_caller`, `a2a_callee` (+task 로그는 `a2a_task_id`) |
| `protocols/mcp/` | `agent`, `mcp_server` (+tool 로그는 `tool`, `duration_ms`) |
| `agentd/` | `agent`, `task_id` (+모델 호출은 `model`, `input_tokens`, `output_tokens`) |
| `decision/` | `agent`, `task_id`, `decision_use`, `question` (+판정 로그는 `band`, `probability`, `duration_ms` / 게이트 차단은 `tool`) |
| `modules/` | `module_ref` |
| `access/`, `egress/` | `agent`, `resource`, `target` (+판정 로그는 `decision`, `decision_source` / 부여 로그는 `grant_id`, `decided_by`) |

### Log Context Binding

컴포넌트 초기화/태스크 진입 시 bound logger 로 반복 필드 중복을 방지한다:

```python
log = structlog.get_logger().bind(
    agent=self.name,
    run_id=task.run_id,
    task_id=task.task_id,
)
```

### Log Storage

1. **Console**: 개발 환경 pretty 모드
2. **File/Stdout JSON**: 컨테이너 stdout 으로 JSON — Docker logging driver 가 수집
3. **Centralized**: 프로덕션은 Loki 등으로 집계, `run_id` 검색 가능해야 함. 30일 보존

## Monitoring

### Metrics Collection

Prometheus (`prometheus-client`) 사용. 표준 메트릭:

**`status` 라벨의 허용 값** — 알림이 특정 값으로 필터하므로, 코드가 내지 않는
값을 알림이 보면 **영원히 침묵한다**. 실패는 한 이름으로만 부른다:

| 값 | 의미 |
|---|---|
| `completed` | 정상 완료 |
| `ok` | 정상 완료 (memory 연산 — 감사 로그와 같은 어휘) |
| `failed` | 실패 — 모든 계층 공통 (`error` 같은 동의어 금지) |
| `denied` | 권한 거부 (memory ACL) |
| `rate_limited` | provider rate limit — 재시도 전략이 다르므로 분리 |
| `halted` | service run 이 연속 실패 임계로 정지 (`GRAPH_005`) |
| `stopped` | service run 이 drain 요청으로 정상 정지 |


**`graph` 라벨의 출처** — 에이전트는 자신의 배선을 **가정하지 않지만**
(02 Rule 6), 오케스트레이터가 `TraceContext.graph` 로 **전달**해 준다.
전달받는 것과 가정하는 것은 다르다: 에이전트는 이 값을 메트릭 라벨로만 쓰고
동작을 바꾸지 않으며, A2A 위임 시에도 원래 그래프가 유지된다
(`TraceContext.child()` 가 물려준다). 그래프 밖 태스크(direct 요청)는
`"direct"` 로 기록한다 — 빈 문자열이면 "그래프 없음"과 "라벨을 못 채움"이
구분되지 않는다.

```python
# Task metrics — 에이전트 단위 메트릭은 group 라벨 포함 (그룹별 집계/quota 감시용)
aaestro_agent_tasks_total{agent, group, graph, status}   # Counter — graph: 그래프명 | "direct"
aaestro_agent_task_duration_seconds{agent, group, graph} # Histogram

# Model metrics
aaestro_model_requests_total{agent, provider, model, status, fallback_index}
aaestro_model_tokens_total{agent, model, direction}      # direction: input|output

# Decision metrics (01 Decision Models)
aaestro_decision_calls_total{agent, provider, model, use, status}   # use: recall_filter|input_screen|tool_gate|node|compaction
aaestro_decision_bands_total{agent, use, question, band}            # band: act|uncertain|reject — uncertain 비율이 구간 건강도
aaestro_decision_duration_seconds{agent, provider}
aaestro_decision_unavailable_total{agent, provider, reason}         # reason: timeout|error|circuit_open — 원래 경로로 간 횟수

# Tool / protocol metrics
aaestro_tool_calls_total{agent, source, tool, status}    # source: skillset|mcp
aaestro_mcp_tool_calls_total{agent, server, tool, status}
aaestro_a2a_calls_total{caller, callee, status}

# Runtime metrics
aaestro_containers_running{agent}                        # Gauge
aaestro_container_restarts_total{agent, reason}
aaestro_agent_health{agent}                              # Gauge: 1 healthy / 0 unhealthy

# Orchestrator metrics
aaestro_runs_active{graph, mode}                         # Gauge — mode: mission|service
aaestro_runs_total{graph, mode, status}
aaestro_node_duration_seconds{graph, node_id}
aaestro_checkpoint_operations_total{operation, status}

# Service run metrics
aaestro_service_iterations_total{graph, status}          # Counter — iteration 단위
aaestro_service_idle_delay_seconds{graph}                # Gauge — 현재 idle backoff

# Graph state compaction ([04-module-system.md](04-module-system.md) State Compaction)
aaestro_state_compactions_total{graph, field, strategy, status}  # strategy: keep_last|keep_first|dedupe|summarize, status: completed|failed
aaestro_state_field_tokens{graph, field}                 # Gauge — 압축을 선언한 필드의 추정 토큰 (경계마다)

# Memory metrics ([09-memory-context.md](09-memory-context.md))
aaestro_memory_operations_total{space, op, status}       # op: append|read|latest|search|recall — space: space id | "undeclared"
aaestro_memory_search_duration_seconds{space}
aaestro_memory_entries{space}                            # Gauge
aaestro_memory_index_lag_seconds{space}

# Circuit breaker
aaestro_circuit_state{target}                            # Gauge: 0 closed / 1 open / 2 half

# Access control ([01-architecture.md](01-architecture.md) Access Control)
aaestro_access_decisions_total{resource, decision, source}  # decision: allow|deny, source: fresh|cache|unreachable
aaestro_access_grants_total{resource, op, decided_by}       # op: grant|revoke|refuse
                                                            # resource: 자원 종류 | "unknown" — 본문을 읽기 전에
                                                            #   거절해 (권한 에이전트가 아닌 호출자, #462) 자원 종류를 알 수 없는 부여
                                                            #   거절. 고정값이다 (09 의 memory `undeclared` 와 같은 결 — 호출자가 고른
                                                            #   값을 라벨로 쓰지 않는다). AccessGrantRefusalsSpike 는 `sum without (op)`
                                                            #   라 이 시계열도 그대로 잡는다
aaestro_access_registry_reachable{component}                # Gauge: 1 / 0 — 강제 지점별
aaestro_access_cache_invalidations_total{component}
```

### Health Checks

```python
class HealthStatus(BaseModel):
    status: Literal["healthy", "degraded", "unhealthy"]
    components: dict[str, ComponentHealth]   # model, mcp:{server}, a2a, modules, memory
    checked_at: datetime
```

Health check 대상:
- 에이전트: 모델 연결(경량 ping 또는 최근 성공 기록), MCP 세션별 상태, A2A 서버 liveness
- 오케스트레이터: checkpointer 연결, Docker daemon 연결
- Degraded 기준: 지연 임계 초과 또는 optional 컴포넌트 실패

### Alerting Rules

```yaml
groups:
  - name: aaestro
    interval: 30s
    rules:
      - alert: AgentHighFailureRate
        expr: |
          rate(aaestro_agent_tasks_total{status="failed"}[5m]) /
          rate(aaestro_agent_tasks_total[5m]) > 0.1
        for: 5m
        labels: {severity: warning}
        annotations:
          summary: "Agent {{ $labels.agent }} failure rate > 10%"

      - alert: AgentDown
        expr: aaestro_agent_health == 0
        for: 3m
        labels: {severity: critical}
        annotations:
          summary: "Agent {{ $labels.agent }} unhealthy for 3m"

      - alert: ContainerRestartLoop
        expr: increase(aaestro_container_restarts_total[10m]) > 5
        labels: {severity: critical}
        annotations:
          summary: "Agent {{ $labels.agent }} restart loop"

      - alert: ModelRateLimited
        expr: |
          rate(aaestro_model_requests_total{status="rate_limited"}[5m]) > 1
        for: 5m
        labels: {severity: warning}

      - alert: DecisionModelMostlyUncertain
        expr: |
          sum without (band) (rate(aaestro_decision_bands_total{band="uncertain"}[30m])) /
          sum without (band) (rate(aaestro_decision_bands_total[30m])) > 0.5
        for: 30m
        labels: {severity: warning}
        annotations:
          summary: "{{ $labels.question }} on {{ $labels.agent }} is uncertain more than half the time — the bands or the wording need recalibration"

      - alert: DecisionModelUnavailable
        expr: |
          sum by (agent, provider) (rate(aaestro_decision_unavailable_total[10m])) > 0.5
        for: 10m
        labels: {severity: warning}
        annotations:
          summary: "Agent {{ $labels.agent }} is running without its decision model — tasks fall back to the LLM path"

      - alert: CheckpointFailures
        expr: |
          rate(aaestro_checkpoint_operations_total{status="failed"}[5m]) > 0
        for: 5m
        labels: {severity: critical}
        annotations:
          summary: "Checkpoint failures — run recovery at risk"

      - alert: ServiceRunStalled
        expr: |
          aaestro_runs_active{mode="service"} > 0
          and increase(aaestro_service_iterations_total[30m]) == 0
        labels: {severity: critical}
        annotations:
          summary: "Service graph {{ $labels.graph }} made no progress for 30m"

      - alert: ServiceRunHalted
        expr: increase(aaestro_service_iterations_total{status="halted"}[10m]) > 0
        labels: {severity: critical}
        annotations:
          summary: "Service graph {{ $labels.graph }} halted (GRAPH_005 failure streak)"

      - alert: AccessRegistryUnreachable
        expr: aaestro_access_registry_reachable == 0
        for: 1m
        labels: {severity: critical}
        annotations:
          summary: "{{ $labels.component }} cannot reach the access registry — new decisions are denied, revocations are not applied"

      - alert: AccessGrantRefusalsSpike
        expr: sum without (op) (increase(aaestro_access_grants_total{op="refuse"}[10m])) > 20
        labels: {severity: warning}
        annotations:
          summary: "Many refused grants — a worker agent may be trying to talk the permission agent past its ceiling"
```

### Dashboards

Grafana 대시보드 구성:

1. **Overview**: active runs, 에이전트별 태스크 성공/실패율, 토큰 사용량, 컨테이너 상태
2. **Agent Detail**: 태스크 latency 분포, tool 호출 breakdown, 모델 사용량, 재시작 이력
3. **Protocol**: A2A 호출 매트릭스 (caller×callee), MCP 서버별 tool 성공률/지연
4. **Graph**: node 별 latency, run 소요 시간 분포, 실패 node 랭킹
5. **System**: 호스트 CPU/Memory, 컨테이너 리소스 사용률, Docker 디스크

## Incident Response

### On-Call Procedures

1. **Alert Response**
   - Grafana 대시보드 확인 → 영향 범위 파악 (특정 에이전트 / 그래프 전체 / 인프라)
   - `run_id` 로 실패 run 의 로그 추적
   - 에러 코드 분포 확인 (LLM_* 인지 RT_* 인지에 따라 대응이 다름)

2. **Mitigation Steps**
   - 특정 에이전트 장애: 해당 에이전트만 재시작 (그래프는 checkpoint 에서 재개)
   - Provider rate limit: 세마포어 축소, 필요 시 fallback 모델 전환
   - MCP 서버 불안정: `optional` 전환 또는 해당 skillset 비활성 버전으로 롤백
   - 토폴로지 문제: 이전 그래프 버전으로 롤백 (모듈 버저닝 덕에 즉시 가능)
   - Service run 정지 (GRAPH_005): 에러 코드 분포로 원인 해소 확인 후
     `aaestro run resume <run_id>` 로 마지막 iteration checkpoint 에서 재개

3. **Escalation**
   - P0 (Critical): 전체 run 실패, checkpoint 유실
   - P1 (High): 핵심 에이전트 다운, >50% 실패율
   - P2 (Medium): 단일 에이전트 성능 저하, 특정 tool 실패
   - P3 (Low): 경미한 품질 저하

### Debugging Tools

1. **Run Tracing**
   - `run_id` 하나로 orchestrator → runtime → agentd → protocol 로그 전체 연결
   - TraceContext 를 A2A 호출까지 전파

2. **Replay Capability**
   - Checkpoint 기반 특정 node 부터 재실행
   - 태스크 입력(TaskRequest) 저장 → 동일 입력으로 에이전트 단독 재현 (`aaestro replay`)

3. **Debug Commands**
   ```
   aaestro status                        # 그래프/에이전트 상태 요약
   aaestro agent logs <agent>            # 에이전트 컨테이너 로그
   aaestro agent inspect <agent>         # manifest + 실제 로드 상태 비교
   aaestro run trace <run_id>            # run 의 node 실행 타임라인
   aaestro run resume <run_id>           # 마지막 checkpoint 에서 재개
   ```

## Data Integrity

### Consistency Checks

1. Graph run 기록 ↔ checkpoint 존재 여부 대조 (orphan 감지)
2. 배포된 그래프의 모듈 ref ↔ registry 실재 여부 정기 검증
3. 컨테이너 실재 ↔ runtime 이 아는 에이전트 목록 대조 (유령 컨테이너 정리)

### Backup and Recovery

1. **Backup**
   - Checkpointer DB (Postgres/Redis): 일 단위 백업
   - Module registry: git 으로 버전 관리 (modules/, graphs/, agents/ 전부 커밋 대상)
   - Config: git 버전 관리

2. **Recovery**
   - 복구 절차 runbook 문서화 (`docs/en/runbooks/` + `docs/ko/runbooks/`)
   - 분기별 복구 리허설

3. **Retention**
   - Run 기록/usage 집계: 1년
   - Checkpoint: run 완료 후 30일 (재현 필요 기간)
   - 로그: 30일
