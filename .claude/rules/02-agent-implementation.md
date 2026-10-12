# Agent Implementation Rules

## Core Agent Interface

### Base Agent Contract

모든 에이전트는 다음 계약을 만족해야 한다. 에이전트 컨테이너 내부의 `agentd` 가 이 인터페이스를
Control API 로 노출하고, 오케스트레이터는 오직 Control API 를 통해서만 에이전트를 호출한다.

```python
class BaseAgent(ABC):
    """에이전트 구현의 기본 계약. agentd 가 이 인터페이스를 Control API 로 서빙한다."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Manifest 의 metadata.name 과 일치해야 한다."""

    @abstractmethod
    def card(self) -> AgentCard:
        """A2A AgentCard. manifest 로부터 자동 생성이 기본."""

    @abstractmethod
    async def initialize(self, ctx: AgentContext) -> None:
        """모듈(promptset/skillset)과 프로토콜(MCP/A2A) 초기화. 실패 시 컨테이너 unhealthy."""

    @abstractmethod
    async def invoke(self, task: TaskRequest) -> TaskResult:
        """단일 태스크 실행. 멱등성 보장 필수 (동일 task_id 재호출 안전)."""

    @abstractmethod
    def stream(self, task: TaskRequest) -> AsyncIterator[TaskEvent]:
        """스트리밍 실행 — 구현은 async generator (`async def` + `yield`) 로 작성한다.

        호출자는 `async for event in agent.stream(task)` 로 소비한다 (별도 await 없음).
        07 의 async-first 규칙을 async generator 형태로 적용한 계약이다.
        이벤트 단위: token / tool_call / tool_result / done / error.
        """

    @abstractmethod
    async def health(self) -> HealthStatus:
        """모델 연결, MCP 세션, 의존 모듈 상태 종합."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Graceful shutdown. 진행 중 태스크 drain 후 MCP/A2A 세션 정리."""
```

### Task Models

```python
class TaskRequest(BaseModel):
    task_id: str                          # UUID — 멱등성 키
    run_id: str                           # 소속 graph run (direct 요청은 "direct-" prefix 의 ad-hoc id)
    node_id: str | None                   # 그래프 상 노드 id — direct 요청이면 None
    input: dict[str, Any]                 # state 에서 추출된 입력 / direct 요청 본문
    config: TaskConfig                    # timeout, max_turns 등
    trace: TraceContext                   # 분산 추적 컨텍스트
    decision: DecisionAsk | None = None   # 그래프 decision 노드 — 질문 참조(alias.question).
                                          # 있으면 LLM 루프 대신 결정 한 번으로 답한다 (Decision Tasks 절)


class TaskResult(BaseModel):
    task_id: str
    status: TaskStatus                    # completed | failed | canceled
    output: dict[str, Any]                # state 로 병합될 출력
    usage: ModelUsage                     # 토큰/비용 집계
    error: AaestroErrorPayload | None
```

### Rules for All Agents

1. **Cancellation & Timeout**
   - MUST respect `asyncio.CancelledError` — 취소 시 진행 중 tool 호출 정리 후 전파
   - MUST enforce `TaskConfig.timeout_s` (기본 300초) — 초과 시 `TO_001` 로 실패 처리
   - 모델 호출, tool 호출, A2A 호출 각각에 개별 timeout 적용

2. **Statelessness**
   - invoke 간 in-memory 상태 유지 금지 — 모든 지속 데이터는 graph state 로 반환
   - 지속 기억이 필요하면 `AgentContext.memory` 로 선언된 space 에 저장
     ([09-memory-context.md](09-memory-context.md)) — 프로세스 메모리/로컬 파일 금지
   - 캐시는 허용하되 correctness 에 영향을 주지 않아야 함 (컨테이너 재시작 시 소실 전제)

3. **Idempotency**
   - 동일 `task_id` 재호출은 안전해야 함 (재시도/재개 시나리오)
   - 외부 부수효과가 있는 tool 은 task_id 기반 중복 방지 적용

4. **Error Handling**
   - MUST return typed errors (`AaestroError` payload) — [05-error-handling.md](05-error-handling.md)
   - MUST NOT crash the daemon on task failure — 태스크 실패는 TaskResult 로 보고
   - Uncaught exception 은 agentd 최상위 핸들러에서 `INTERNAL` 카테고리로 변환

5. **Output Discipline**
   - `output` 은 그래프 state schema 와 호환되는 키만 포함
   - 대용량 산출물(파일, 원문)은 output 에 직접 넣지 않고 artifact 저장소 참조로 전달

6. **Peer Equality & Wiring Agnosticism**
   - 에이전트 코드는 자신이 어떤 그래프의 어느 위치에 배선되는지 가정하지 않는다 —
     연결 구조는 배선이 결정하며, 에이전트 간 우열 관계는 존재하지 않는다
   - 그래프 노드 태스크 / peer 위임 태스크 / direct 요청 태스크는 모두 동일한
     TaskRequest 계약으로 처리
   - Mission/service 모드도 가정 금지 — iteration 간 지속 데이터는 graph state 로만

## Agent Manifest

### Manifest Specification

에이전트는 `agents/<name>/manifest.yaml` 로 선언한다. Manifest 는 에이전트의 **유일한 계약
소스**이며, 코드에 하드코딩된 모델명/프롬프트/tool 목록은 금지한다.

```yaml
apiVersion: aaestro/v1
kind: Agent
metadata:
  name: researcher              # 소문자 + 하이픈, 그래프에서 참조하는 id
  version: 0.1.0                # semver — 계약 변경 시 반드시 bump
  group: research               # 소속 그룹 (선택, 최대 1개) — 미선언 시 global 만
  description: 웹 리서치 전담 에이전트

spec:
  model:
    provider: anthropic
    name: claude-sonnet-5
    max_tokens: 8192
    effort: high              # 품질/비용 조절 — sampling 파라미터는 현재 API 가 받지 않는다
    # 아래는 전부 선택 — Manifest Rules 9번 (Model Resilience Declaration)
    # fallbacks: [{provider: anthropic, name: claude-haiku-4-5, on: [rate_limit, server_error]}]
    # retry: {rate_limit: {start_s: 10, attempts: 5}}
    # circuit: {max_failures: 5, reset_s: 60}

  promptset:
    ref: promptsets/researcher@0.1.0

  skillsets:
    - ref: skillsets/web-search@0.2.0
    - ref: skillsets/summarize@0.1.0

  skillpacks:                   # Agent Skills 표준 디렉토리 묶음 — 04 Skillpack Modules. 선택.
    - ref: skillpacks/docs@1.0.0  # 선언·배포 검증까지 — agentd 는 아직 쓰지 않고 기동 시 WARN 을 남긴다 (#374)

  memory:                       # 컨텍스트 메모리 space — 09 참조
    spaces:
      - ref: memorysets/agent-longterm@0.1.0
        as: longterm

  decision:                     # 결정 모델 — 01 Decision Models. 선택: 없으면 모든 쓰임이 원래 경로다
    provider: jev               # configs 의 decision.providers 키 — 프록시가 종단한다
    model: jev-1.13             # provider 의 모델 id — 버전 고정
    sets:
      - ref: decisionsets/research-triage@0.1.0
        as: triage              # 질문 참조는 alias.question
    recall_filter:              # 쓰임 1 — 회상 상위 k 를 한 번 더 거른다 (09)
      question: triage.memory_is_relevant
      uncertain: keep           # keep(기본) | drop
    input_screen:               # 쓰임 2 — 신뢰하지 않는 입력의 지시문 표시 (Execution Loop)
      question: triage.contains_instructions
    tool_gates:                 # 쓰임 6 — 부수효과 도구 호출 전 판정 (Execution Loop)
      - tools: ["mcp__mail__*", "post_*"]     # glob — 네임스페이스 이름 기준
        question: triage.tool_call_fits_task
        uncertain: block        # block(기본) | allow
        unavailable: allow      # allow(기본) | block — provider 가 닿지 않을 때

  mcp:                          # 이 에이전트 전용 MCP 서버 — 03 참조
    servers:
      - name: filesystem
        transport: stdio
        command: ["mcp-server-filesystem", "/workspace"]

  a2a:                          # 이 에이전트의 A2A 노출 설정 — 03 참조
    enabled: true
    capabilities:
      streaming: true

  runtime:
    image: aaestro/agent-base:0.1.0   # 빌드 재료가 있으면 aaestro/agent-<name>:<version> 과 같아야 함
    resources:
      cpu: "1.0"
      memory: 1Gi
    env_allowlist:              # 컨테이너에 주입 허용되는 env 키 (secrets 포함)
      - ANTHROPIC_API_KEY
    volumes: []                 # 기본 없음 — 필요 시 명시 선언 (아래 격리 규칙 참조)
```

### Manifest Rules

1. **Validation**: 배포 시 pydantic 스키마로 검증 — 미검증 manifest 로 컨테이너 기동 금지
2. **Versioning**: 다음 변경 시 version bump 필수
   - 입력/출력 계약 변경 (minor/major)
   - promptset/skillset ref 변경 (patch 이상)
   - 모델 변경 (minor)
3. **No Hidden Dependencies**: manifest 에 선언되지 않은 모듈/서버/자원 사용 금지
4. **Reference Format**: 모듈 참조는 항상 `{type}/{name}@{version}` — latest 사용 금지
5. **No Hierarchy Declaration**: manifest 에 에이전트 간 우열 관계 선언 금지 —
   연결 구조는 그래프 배선 소관 ([04-module-system.md](04-module-system.md)).
   Peer 호출을 받으려면 `a2a.enabled: true` 필요
6. **Group Membership**: `metadata.group` 은 최대 하나, 존재하는 그룹만 참조.
   미선언 시 global 에만 소속. `group: global` 직접 선언 금지 (배포 검증 차단).
   그룹은 리소스 스코프일 뿐 — 소속이 달라도 배선/호출 규칙은 동일
   ([01-architecture.md](01-architecture.md) Resource Scoping)
7. **MCP Server Schema**: `spec.mcp.servers` 항목의 정식 필드 집합은
   [03-protocol-integration.md](03-protocol-integration.md) 의 서버 선언 스펙을 따른다 —
   stdio/sidecar/external 3 패턴과 `allowed_tools` / `optional` / `auth` /
   `env_allowlist` 전부 manifest 스키마의 정식 필드다 (03 의 예시가 곧 스키마 계약)
8. **Decision Declaration**: `spec.decision` 은 선택이다. 있으면 `provider` 와 `sets` 가 필수이고,
   `recall_filter` / `input_screen` / `tool_gates` 의 질문은 `sets` 의 alias 로 해석돼야 한다
   (배포 검증 `MOD_001`). 질문 종류도 맞아야 한다 — 세 쓰임 모두 `predicate` (`MOD_003`).
   provider 자격증명은 env 에 없다 — 프록시가 주입한다 (Secrets Injection)
9. **Model Resilience Declaration**: `spec.model` 의 `fallbacks` / `retry` / `circuit` 은 전부
   선택이며 생략하면 기존 동작이다. `fallbacks[].on` 은 `rate_limit` / `server_error` /
   `context_overflow` 중에서 비어 있지 않게 선언하고, fallback 의 (provider, name) 은 primary 및
   다른 fallback 과 달라야 한다. 모르는 키는 거부된다. 해석은 가까운 쪽이 이긴다 —
   manifest → 그래프 노드 `model` → 배포 요청 `model_overrides` 순으로 덮고, override 는 부분
   선언이며 명시한 빈 값 · null 은 아래 계층 값을 지운다 (`provider` 를 적으면 `name` 도 적는다).
   노드 · 배포 override 는 manifest 가 선언한 provider 집합 안에서만 provider 를 바꾼다 — 이그레스
   판정 기본값이 manifest 에서 도출된다. 배포 검증은 모르는 provider 와
   `runtime.egress_proxy.providers` 에 없는 provider 를 `VAL_002` 로 거절한다.
   해석된 선언은 태스크 시작 시 고정된다 (Hot Reload 와 같은 규칙)

## Docker Isolation Rules

### Container Standards

1. **One Agent, One Container**
   - 에이전트 프로세스와 그 MCP stdio 서버들만 같은 컨테이너에 공존 가능
   - 서로 다른 에이전트의 프로세스 동거 금지

2. **Base Image**
   - 모든 에이전트는 `aaestro/agent-base` 에서 시작 (agentd 포함)
   - 커스텀 의존성은 에이전트별 **빌드 재료**의 `Dockerfile` 에서 base 를 확장
     (재료는 스토어에 둔다 — 아래 Registering Agents)
   - 이미지 태그는 semver — `latest` 태그로 배포 금지

3. **Security**
   ```dockerfile
   # 빌드 재료의 Dockerfile — 컨텍스트 루트는 빌더가 조립하는 임시 디렉토리
   # (Dockerfile, manifest.yaml, modules/, src/)
   FROM aaestro/agent-base:0.1.0

   # 에이전트별 추가 의존성만 여기서 설치
   COPY src/requirements.txt /tmp/requirements.txt
   RUN pip install --no-cache-dir -r /tmp/requirements.txt

   COPY --chown=1000:1000 src/ /app/src/
   # base 이미지가 이미 non-root (uid 1000, user: agent) 로 실행
   ```
   - 빌더가 **굽기 전에** 검사한다 (`VAL_002`): `FROM aaestro/agent-base:<tag>` 로 시작,
     root 로 끝나지 않음, `COPY`/`ADD` 소스가 컨텍스트 안의 로컬 경로 (원격 `ADD` 금지)
   - MUST run as non-root user
   - MUST NOT bake secrets into images (빌드 arg 로도 금지)
   - SHOULD use read-only root filesystem + 명시적 writable volume (`/workspace`, `/tmp`)
   - Capability drop: `--cap-drop=ALL` 기본, 필요 capability 만 manifest 로 선언

4. **Resources**
   - CPU/Memory limit 필수 (manifest `runtime.resources`)
   - 미선언 시 프레임워크 기본값 적용 (CPU 1.0 / 1Gi)
   - PID limit 설정 (fork bomb 방지, 기본 256)
   - 소속 그룹의 quota 합계 검증 — 초과 시 기동 거부 (`RT_006`)

5. **Network**
   - 에이전트는 **외부 경로가 없는 내부 네트워크**(Docker `--internal`)에만 연결한다
   - 외부로 나가는 유일한 경로는 **egress proxy** — 내부 네트워크와 외부 네트워크에 함께 붙는
     유일한 컨테이너다 ([01-architecture.md](01-architecture.md) Access Control)
   - 노출 포트는 두 개뿐:
     - **Control port** (agentd, 컨테이너 내부 8080) — runtime layer 만 접근
     - **A2A port** (manifest 로 활성화 시) — 호출마다 레지스트리 판정을 거친 peer 만
   - 호스트 네트워크 모드 금지, 임의 포트 publish 금지
   - Egress 는 문서화가 아니라 **강제**다: 모델 API 는 base URL 로 프록시가 종단하고, 그 밖의
     외부 HTTPS 는 프록시가 CONNECT 목적지 호스트 단위로 판정한다
     ([03-protocol-integration.md](03-protocol-integration.md) Egress)
   - 전환 시에는 먼저 **기록만 하는 모드**로 켜서 선언되지 않은 외부 호출을 드러낸 뒤 강제한다
   - 도구 주입 여부(스킬셋·MCP 선언)는 **강제 수단이 아니다** — 컨테이너 안의 코드는 무엇이든
     실행할 수 있으므로, 외부로 향하는 권한은 프록시의 판정이 쥔다

6. **Volumes**
   - 기본: 볼륨 없음
   - 필요 시 manifest 에 명시 선언 + 에이전트별 격리 경로만 마운트
   - 에이전트 간 볼륨 공유 금지 (사이드채널 차단). 예외는 runtime 이 거는 **읽기 전용 선언
     마운트**(자기 매니페스트 디렉토리, 모듈 루트) 하나다 — 쓸 수 없으므로 사이드채널이 아니다
   - 선언은 **디렉토리 단위**로 마운트한다 — 단일 파일 바인드는 원자적 교체를 반영하지 못해
     떠 있는 컨테이너가 옛 선언을 읽는다
   - 호스트 민감 경로 (`/var/run/docker.sock` 등) 마운트 절대 금지

### Secrets Injection — Scoped

```
Secret stores:   local(agent) ── group ── global   (3계층, 01 Resource Scoping)
Runtime layer →  env_allowlist 각 키를 local > group > global 순으로 해석
              → (기동 시) docker env 주입 → 컨테이너
```

- **프록시가 종단하는 서비스의 자격증명은 env 로 넣지 않는다**: 모델 API 키, 결정 모델 API 키,
  원격 MCP 서버 자격증명은 egress proxy 가 요청에 주입한다. 에이전트 컨테이너는 그 값을 갖지 않으므로
  회수가 재배포 없이 반영된다
- 그 밖의 secrets 는 runtime 이 기동 시점에 env 로 주입 — `env_allowlist` 에 있는 키만.
  env 로 넣은 값은 실시간 회수가 되지 않는다 (재배포 필요)
- 키 해석은 **local > 소속 group > global** — 가까운 스코프 값이 우선 (shadowing 허용)
- Group 스코프 키는 group.yaml 의 `secrets` 목록에 선언된 것만 멤버에게 제공 —
  비멤버 에이전트는 같은 키를 allowlist 에 넣어도 group 값으로 해석되지 않는다
- 배포 검증: `env_allowlist` 의 각 키가 세 스코프 중 하나에서 해석 가능해야 함
  (실패 시 `CFG_002`)
- 로그에 secret 값 출력 금지 (structlog processor 로 마스킹)
- 에이전트 코드는 `os.environ` 직접 접근 대신 `AgentContext.secrets` 를 통해 접근

## Agent Lifecycle

### Lifecycle States

```
        ┌──────────┐   build   ┌─────────┐   start   ┌──────────┐
        │ Declared │──────────▶│  Built  │──────────▶│ Starting │
        └──────────┘           └─────────┘           └────┬─────┘
                                                          │ initialize 성공
                                    health fail ┌─────────▼─────────┐
                              ┌─────────────────│       Ready       │
                              ▼                 └─────────┬─────────┘
                        ┌───────────┐   재시작 정책        │ drain 요청
                        │ Unhealthy │────────────┐  ┌─────▼─────┐
                        └───────────┘            │  │ Draining  │
                              │ 임계 초과         │  └─────┬─────┘
                              ▼                  │        │ 진행 태스크 완료
                        ┌───────────┐            │  ┌─────▼─────┐
                        │  Failed   │            └─▶│  Stopped  │
                        └───────────┘               └───────────┘
```

### Lifecycle Rules

1. **Build**: 이미지 빌드는 **명시적 빌드 단계**에서 — 런타임 중 빌드 금지.
   빌드 재료가 있는 에이전트는 그 버전이 `built` 여야 배포된다 — 굽지 않았거나 실패했거나
   굽는 중이거나 기록의 태그가 띄울 태그와 다르면 배포가 기동 전에 거절한다 (`RT_012`, HTTP 409). 배포가 대신 굽지 않는다.
   재료가 없는 declarative agent 는 빌드 단계가 없다 (base 이미지 + 선언 마운트)
2. **Start**: 기동 → `initialize()` → health OK 가 되어야 그래프에 attach
3. **Ready**: health check 주기 실행 (기본 10s 간격, 3회 연속 실패 시 Unhealthy)
4. **Drain**: 새 태스크 수락 중지 → 진행 중 태스크 완료 대기 (기본 30s) → 종료
5. **Stop**: SIGTERM → 30s grace → SIGKILL. `shutdown()` 에서 MCP/A2A 세션 정리
6. **Restart Policy**: Unhealthy 시 exponential backoff 재시작 (1s → 2s → 4s... max 60s),
   10분 내 5회 초과 시 Failed 로 전환하고 알림

### Hot Reload

- **Promptset / Skillset 교체**: `POST /reload` 로 무중단 리로드 지원 (신규 태스크부터 적용)
  - 다시 읽는 것: promptset 선언(템플릿 목록·변수·출력 키), skillset 선언과 코드, decisionset
    선언(질문·구간), 광고 도구와 AgentCard. 템플릿 **본문**은 렌더할 때마다 읽으므로 리로드와
    무관하다
  - 진행 중 태스크는 시작할 때 잡은 모듈 묶음으로 끝까지 간다 — 한 태스크 안에서 옛·새 모듈이 섞이지 않는다
  - 새 묶음을 끝까지 조립한 뒤 교체한다. 실패하면 typed 에러로 답하고 이전 상태를 유지한다
  - 메모리·peer·MCP 연결과 결정 provider 클라이언트는 모듈이 아니라 배선이라 유지한다
  - 리로드할 모듈이 없는 실행기(커스텀 entrypoint, echo)는 `unsupported` 로 답한다
- **Manifest 변경**: 리로드 불가 — 새 버전으로 재배포 (컨테이너 교체)
- **MCP 서버 목록 변경**: manifest 변경에 해당 — 재배포

## Agent Control API

agentd 가 컨테이너 내부 8080 포트로 서빙하는 표준 API. Runtime layer 외 직접 호출 금지.

```
POST /v1/invoke          # TaskRequest → TaskResult (동기)
POST /v1/stream          # TaskRequest → SSE(TaskEvent 스트림)
GET  /v1/health          # HealthStatus (모델/MCP/모듈 종합)
GET  /v1/card            # A2A AgentCard
POST /v1/cancel/{task_id}# 진행 중 태스크 취소
POST /v1/reload          # promptset/skillset hot reload
POST /v1/drain           # graceful drain 개시
```

### API Rules

1. 모든 응답은 pydantic 모델의 JSON 직렬화 — ad-hoc dict 금지
2. `/invoke` 는 202 + polling 이 아닌 동기 응답 (LangGraph node 실행 모델과 일치).
   장시간 태스크는 `/stream` 사용
3. 인증: runtime 이 발급한 per-agent token 을 `Authorization` 헤더로 요구
4. `/health` 는 무인증 — Docker healthcheck 가 직접 호출

## Execution Loop (agentd internals)

에이전트 내부 실행 루프의 표준 구조:

```python
async def execute(self, task: TaskRequest) -> TaskResult:
    prompt = self.promptset.render(task.node_id, **task.input)
    tools = [*self.skillset.tools(), *self.mcp.tools()]

    async with task_span(task):  # tracing + 로그 컨텍스트
        for turn in range(self.config.max_turns):
            response = await self.model.run(prompt, tools=tools)

            if not response.tool_calls:
                return TaskResult.completed(task, output=response.content)

            results = await self.run_tools(response.tool_calls, task)
            prompt = prompt.extend(response, results)

    raise AaestroError(category=ErrorCategory.MODEL, code="LLM_005",
                       message="max turns exceeded", retryable=False)
```

### Loop Rules

1. **Max Turns**: tool loop 는 상한 필수 (기본 20) — 무한 루프 방지
2. **Tool Timeout**: 개별 tool 호출 timeout (기본 60s)
3. **Parallel Tools**: 독립 tool call 은 `asyncio.gather` 로 병렬 실행
4. **Usage Tracking**: 매 모델 호출의 토큰 사용량 누적 → TaskResult.usage
5. **Event Emission**: 스트리밍 모드에서 turn 별 tool_call/tool_result 이벤트 발행
6. **Model Failover**: 모델 호출의 재시도가 소진되면 `spec.model.fallbacks` 중 실패 부류가
   `on` 에 든 다음 모델로 같은 턴을 보낸다 — 전환 주체는 agentd (05 Retry Layering).
   circuit open 인 모델은 건너뛴다

### Decision Hooks — 루프 안의 결정 모델

`spec.decision` 이 있으면 루프의 세 자리에서 결정 모델을 부른다. 셋 다 **원래 경로를 바꾸지
않는 층**이다 — provider 가 없거나 `uncertain` 이면 결정 모델이 없을 때와 같이 동작한다
([01-architecture.md](01-architecture.md) Decision Models).

```python
# 1. 회상 필터 — 태스크 진입, auto-recall 직후 (09 Context Assembly)
hits = await self.recall(task)                                   # 검색은 인덱스가
if self.decision.recall_filter:
    hits = await self.decision.filter_relevant(task, hits)        # 상위 k 를 한 번에 물어 act/keep 만 남긴다

# 2. 입력 판별 — tool 결과 / A2A 응답이 대화에 들어가기 직전
outcome = tool_outcome(call, result)
if self.decision.input_screen and await self.decision.looks_like_instructions(outcome):
    outcome = outcome.flagged()                                  # 경계 표시를 강화하고 WARN + 메트릭. 버리지 않는다

# 3. 도구 게이트 — 부수효과 도구를 실행하기 직전
gate = self.decision.gate_for(call.name)
if gate is not None:
    verdict = await self.decision.gate(task, call, gate)          # act → 실행 / reject·uncertain(block) → 실행하지 않는다
    if verdict.blocked:
        return ToolOutcome(call.id, content=f"gated: {verdict.reason}", is_error=True)  # 모델이 다시 생각할 수 있게 결과로 돌려준다
```

1. **호출 횟수**: 회상 필터는 태스크당 1회(상위 k 를 한 요청에), 입력 판별은 결과당 1회, 게이트는
   게이트된 호출당 1회. 루프의 turn 마다 반복하지 않는다
2. **게이트는 편의다**: 컨테이너 안의 검사이므로 강제 수단이 아니다 (01 Access Control).
   부수효과의 진짜 통제는 이그레스 판정과 도구 주입 여부다. 그래서 `unavailable` 기본값은
   `allow` + WARN — provider 장애가 에이전트 전체를 멈추게 두지 않는다. `block` 은 그 도구의
   부수효과가 provider 장애보다 비싼 경우에만 선언한다
3. **차단은 태스크 실패가 아니다**: 게이트에 걸린 호출은 `is_error` 결과로 모델에 돌아간다 —
   모델이 인자를 고치거나 다른 길을 택할 수 있다. 같은 도구가 한 태스크에서 두 번 차단되면 그
   태스크의 이후 호출도 차단한다 (게이트를 두드리는 루프 방지)
4. **판별은 신호다**: 입력 판별의 `act` 는 경계 표시(`[untrusted: instructions detected]`)와
   `error_code` 없는 WARN 로그, `aaestro_decision_bands_total{use="input_screen"}` 로 남는다.
   결과를 버리거나 태스크를 멈추지 않는다 — 03/09 의 경계 규칙은 결정 모델 없이도 유지된다
5. **게이트에 넣는 state**: 태스크 입력(렌더된 프롬프트가 아니라 `TaskRequest.input`)과 도구
   호출(이름·인자)만. 대화 전체를 보내지 않는다 — 비용이자 외부 반출이다
6. **결정은 타임아웃이 짧다**: `decision.timeout_s`(기본 5초) — 넘기면 `unavailable` 로 취급한다.
   판정이 생성만큼 느리면 두는 의미가 없다

## Decision Tasks — 그래프 decision 노드

그래프가 노드를 `decision:` 으로 선언하면 ([04-module-system.md](04-module-system.md)) 그 노드의
태스크는 LLM 루프를 타지 않는다. agentd 는 `TaskRequest.decision` 을 보고 결정 한 번으로 답한다.

```python
async def decide(self, task: TaskRequest) -> TaskResult:
    ask = task.decision                                          # alias.question + 어느 입력 키를 state 로 보낼지
    question = self.decisionsets.resolve(ask.question)
    state = render_state(task.input, ask.fields)                 # 선언된 키만 텍스트로 — 프롬프트 템플릿 없음
    decision = await self.decision_model.decide(state, [question])
    return TaskResult.completed(task, output={
        "value": decision.value,             # predicate: bool / rating: 등급 / choice: 보기
        "band": decision.band,               # act | uncertain | reject
        "probability": decision.probability,
        "distribution": decision.distribution,
    })
```

1. **같은 계약**: TaskRequest/TaskResult, 멱등성, timeout, 로깅·메트릭은 그래프 태스크와 같다.
   usage 는 0 토큰이다
2. **에이전트가 provider 를 가진다**: 노드는 `agent:` 로 결정을 내릴 에이전트를 가리키고, 그
   에이전트의 `spec.decision` 이 provider 와 decisionset 을 정한다. `spec.decision` 없는
   에이전트에 decision 노드를 물리면 배포 검증 실패 (`MOD_001`)
3. **답은 state 로**: 결과는 `output_map` 으로 state 에 병합된다. edge 조건은 state 를 읽는다 —
   조건 안에서 결정 모델을 부르지 않는다 (재개 시 재판정 금지, 01 Decision Models 3)
4. **unavailable 은 실패가 아니라 uncertain 이다**: provider 가 닿지 않으면 `band: uncertain`,
   `value: null` 로 답하고 WARN 을 남긴다 — 그래프의 기본 edge 가 원래 경로(LLM 노드)로 보낸다.
   그래서 decision 노드에는 조건 없는 기본 edge 가 하나 있어야 한다 (04 검증)
5. **direct 요청에는 없다**: `decision` 은 그래프 노드가 넣는다. direct 요청은 항상 LLM 경로다

## Direct Requests — 인터랙티브 직접 호출

모든 에이전트는 그래프 run 과 무관하게 **직접 요청**을 받을 수 있다.
클라이언트가 특정 에이전트를 지목해 단독 태스크를 실행하거나 스트리밍 대화를 나누는 경로다.

```
Client → Control Plane → Runtime → 대상 에이전트 Control API (/invoke | /stream)
```

### Direct Request Rules

1. **동일 계약**: direct 태스크도 TaskRequest — `node_id=None`,
   `run_id` 는 runtime 이 발급한 `direct-` prefix 의 ad-hoc id
2. **템플릿 선택**: `node_id` 가 없으므로 promptset 의 `default` 템플릿 사용
   ([04-module-system.md](04-module-system.md) 호환성 규칙)
3. **Graph State 불간섭**: direct 태스크는 어떤 graph run 의 state 도 읽거나 쓰지 않는다
4. **동등한 규칙 적용**: timeout / max_turns / usage 집계 / 로깅 — 그래프 태스크와 동일.
   Direct 요청 중에도 allowlist 내 peer A2A 호출 가능
5. **동시성**: direct 태스크와 그래프 태스크는 같은 큐에서 처리 — 에이전트별 동시 실행
   상한(`max_concurrent_tasks`, 기본 4) 공유. 상주 그래프에 붙은 에이전트도
   직접 요청에 응답 가능해야 한다
6. **인터랙티브 세션**: 멀티턴 대화는 클라이언트가 대화 이력을 재전송하는 stateless 방식
   기본 — 에이전트가 세션 상태를 메모리에 쥐지 않는다

## Registering Agents

### Agent Registry

```
agents/                     # 선언만 — 빌드 입력은 두지 않는다
├── planner/
│   └── manifest.yaml
├── researcher/
│   └── manifest.yaml       # spec.entrypoint: agent:ResearchAgent
└── writer/
    └── manifest.yaml

Material store (agent, version) → files      # 커스텀 에이전트의 빌드 재료
├── Dockerfile              # 선택 — 없으면 스켈레톤
└── src/
    └── agent.py            # BaseAgent 커스텀 구현
```

1. **Declarative Agent** (기본): manifest 만으로 정의 — agentd 의 기본 실행 루프 사용.
   base 이미지에 선언(manifest, modules)을 읽기 전용으로 마운트해 돈다. 빌드 없음.
   대부분의 에이전트는 promptset + skillset 조합으로 충분해야 한다
2. **Custom Agent**: 빌드 재료의 `src/agent.py` 에 `BaseAgent` 서브클래스 제공 — manifest 의
   `spec.entrypoint: agent:ResearchAgent` 로 지정. 커스텀 구현도 모든 계약 규칙 준수
3. **Build Materials**: 커스텀 에이전트의 `Dockerfile` 과 `src/` 는 저장소가 아니라
   **재료 스토어**에 `(에이전트, 버전)` 단위로 둔다 (`PUT /v1/agents/{name}/materials`,
   `aaestro agent-push`). 같은 버전의 재료는 불변이고, 배포 중인 에이전트의 재료는 바꾸지
   못한다. 굽는 것은 명시적 단계다 (`POST /v1/agents/{name}/image`, `aaestro agent-build`):
   임시 디렉토리에 선언과 재료를 조립해 `aaestro/agent-<name>:<version>` 으로 굽고 지운다.
   manifest 가 `runtime.image` 를 선언하면 그 태그와 같아야 한다 (다르면 `VAL_002`)
4. 그래프는 에이전트를 `agents/{name}@{version}` 으로만 참조

## Monitoring per Agent

에이전트 단위로 반드시 수집하는 지표 (구현은 [05-error-handling.md](05-error-handling.md)):

- 태스크 성공/실패율, 태스크 latency (p50/p95)
- 모델 토큰 사용량, tool 호출 횟수/실패율
- 결정 호출 횟수·지연, 쓰임별 구간 분포 (`uncertain` 비율), 게이트 차단 횟수
- 컨테이너 재시작 횟수, health check 실패율
- MCP 세션 상태, A2A 호출 성공률
