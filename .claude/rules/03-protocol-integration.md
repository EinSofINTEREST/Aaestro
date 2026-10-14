# Protocol Integration Rules — A2A and MCP

## Core Principle: Per-Agent Isolation

프로토콜 자원은 **에이전트 단위로 격리**된다. 이것이 Aaestro 프로토콜 계층의 제1원칙이다.

1. **소유권**: 모든 A2A 엔드포인트와 MCP 서버/세션은 정확히 하나의 에이전트에 속한다
2. **공유 금지**:
   - 여러 에이전트가 하나의 MCP 서버 프로세스를 공유하는 것 금지
   - 에이전트 컨테이너 밖의 "글로벌 MCP 허브" / "공용 tool 게이트웨이" 금지
   - A2A 엔드포인트를 우회하는 에이전트 간 직통 채널 금지
   - **예외 — egress proxy**: 공유 프록시는 도구를 **호스팅하지 않고** MCP 세션도 갖지 않는다.
     에이전트가 연 호출을 요청 단위로 판정하고 전달만 한다. MCP 서버는 여전히 에이전트별
     (stdio / 전용 사이드카 / external) 이고, 프록시는 그 사이의 강제 지점일 뿐이다
     (아래 Egress 절)
3. **선언 필수**: 에이전트가 사용하는 모든 프로토콜 자원은 manifest 에 선언되어야 한다
4. **격리 경계 = 컨테이너 경계**: MCP 서버는 소유 에이전트의 컨테이너 내부(stdio) 또는
   해당 에이전트 전용 사이드카로만 실행된다

### Why

- 에이전트별 권한 경계가 명확해진다 — 에이전트 A 의 filesystem MCP 가 B 의 데이터를 볼 수 없음
- 에이전트를 그래프에서 분리하면 그 프로토콜 자원도 함께 정리된다 (누수 없음)
- 장애 격리 — 한 에이전트의 MCP 서버 crash 가 다른 에이전트에 전파되지 않음
- 모듈형 연결/분리 목표와 일치 — 에이전트는 자신의 자원을 전부 들고 이동하는 단위

## A2A (Agent2Agent) Integration

### A2A Server Rules

각 에이전트는 자신의 A2A 서버를 컨테이너 내부에서 직접 노출한다.

1. **AgentCard**
   - manifest 로부터 자동 생성이 기본 (`name`, `description`, `capabilities`, skills 요약)
   - Card 의 skill 목록은 실제 로드된 skillset 과 일치해야 함 — 수동 작성 금지
   - `GET /v1/card` (Control API) 와 A2A well-known 경로 양쪽에서 동일 내용 제공

2. **Port Assignment**
   - Runtime 이 `protocols.a2a.port_range` 에서 에이전트별 포트 할당
   - 에이전트 코드는 포트를 하드코딩하지 않고 `AgentContext.a2a_port` 사용

3. **Task Lifecycle Mapping**
   - A2A task 상태를 내부 TaskStatus 로 매핑:
     `submitted → working → completed | failed | canceled`
   - 스트리밍 지원 시 A2A 이벤트 ↔ 내부 TaskEvent 1:1 매핑 유지

### A2A Client Rules — Connection Allowlist

에이전트 간 A2A 호출은 **그래프 config 의 `connections` 에 선언된 쌍만** 허용된다.

```yaml
# graphs/research-pipeline.yaml (발췌)
spec:
  connections:
    - caller: researcher
      callee: planner
      # researcher → planner 방향 호출만 허용. 역방향은 별도 선언 필요.
```

1. **Enforcement**
   - **Callee 측이 강제 지점이다**: 피호출자 A2A 서버는 호출마다 호출자의 에이전트 신원으로
     레지스트리에 `a2a` 판정을 묻고, 거부면 `A2A_004`. 연결을 회수하면 재배포 없이 다음
     호출부터 거부된다. 통제받는 주체는 **호출자**이고 피호출자의 서버는 호출자 컨테이너 밖에
     있으므로 01 의 원칙을 만족한다 ([01-architecture.md](01-architecture.md) Access Control)
   - 호출자 신원은 호출자 컨테이너가 위조할 수 없어야 한다 — 피호출자는 호출자가 내민 자격을
     레지스트리로 확인하고, 그 자격은 다른 강제 지점에서 재사용할 수 없게 호출 대상에 묶는다
   - Caller 측: agentd 가 allowlist 에 없는 callee 로의 호출을 `A2A_004` 로 먼저 거부한다 —
     헛호출을 줄이는 **편의**이지 강제 수단이 아니다 (컨테이너 안의 검사)
   - 그래프 `connections` 선언이 기본 권한이고, 운영자는 그것을 실시간으로 회수할 수 있다
   - **A2A 포트가 여는 것은 둘뿐이다**: 자격 없이 읽는 AgentCard (well-known 경로) 와 JSON-RPC
     엔드포인트. JSON-RPC 에서는 새 태스크를 보내는 요청 (`SendMessage` / `SendStreamingMessage`)
     만 검사를 거쳐 연다 — 두 모드 모두 SDK 가 태스크를 만들기 **전에** 호출자를 확인한다.
     SDK 가 여는 나머지 — 태스크 조회·목록·구독·취소, push 설정, 확장 카드, 기존 태스크에 잇는
     메시지 — 는 누가 묻든 같은 응답으로 거절한다 (존재 여부도 알려 주지 않는다). 문서 라우트 ·
     REST 바인딩 · 구버전 호환은 켜지 않는다. SDK 가 요청 메서드를 추가하면 열지 거절할지
     분류하기 전에는 구조 테스트가 통과하지 않는다
   - **태스크 store 는 끝난 태스크를 쥐지 않는다**: 읽는 창구가 없으므로 store 는 태스크를 실행
     중에만 들고, 끝나면 (성공·실패·거부) 버린다 — 요청 수만큼 쌓이는 상태는 02 Statelessness
     위반이다. 알려진 한계: 에이전트 실행이 취소로 끝난 태스크는 SDK 내부의 활성 태스크
     레지스트리에 남을 수 있다 (SDK 동작, 별도 이슈)
2. **Discovery**: 에이전트는 peer 의 주소를 직접 알지 못한다 —
   `AgentContext.peers` 로 주입된 (allowlist 기반) 목록만 조회 가능
3. **No Transitive Calls**: A 가 B 를 호출하고 B 가 C 를 호출하는 것은 각각의 선언이 있을 때만
   가능 — caller 의 권한이 전파되지 않는다
4. **Timeout & Retry**: A2A 호출은 timeout 필수 (기본 120s), 재시도는 caller 의
   RetryPolicy 를 따름 — [05-error-handling.md](05-error-handling.md)
5. **Depth Limit**: A2A 호출 체인 깊이 상한 (기본 3) — `TraceContext.depth` 로 전파/검증,
   초과 시 `A2A_005` (순환 위임 방지)
6. **Peer Symmetry (우열 없음)**: A2A 연결에 상위/하위 개념이 없다 — 호출 방향은
   순전히 선언의 문제이며, 역방향도 선언하면 허용된다. 상호 선언 시 양방향 협업
   (교차 질의/위임) 가능 — 폭주는 depth limit 과 timeout 으로만 방지
7. **Group Neutrality**: 그룹 소속 ([01-architecture.md](01-architecture.md)) 은 연결에
   어떤 자동 권한도 부여하지 않는다 — 같은 그룹이라도 allowlist 선언 없는 호출은
   `A2A_004`. 그룹은 리소스 스코프, 연결은 그래프 배선 — 두 축은 직교한다
8. **Task Id Is Caller-Chosen**: A2A 태스크의 `task_id` 는 보낸 쪽이 고른다. 프레임워크는 그 값이
   다른 호출자에게 추측 불가능하다고 가정하지 않는다. 그래서 피호출자는
   - **본문을 형식으로 거른다**: peer 본문의 식별자 (`task_id`, `run_id`, `node_id`, `trace` 의
     `trace_id` · `span_id` · `graph`) 는 문자열 · 비어 있지 않음 · 128자 이하 · 출력 가능한 문자만 ·
     앞뒤 공백 없음. 본문이 JSON 객체가 아니거나 필드 형태가 틀리면 `VAL_002`, 필수 필드가 없으면
     `VAL_001`. 거절 사유와 로그에는 필드 이름과 사유 종류만 싣고 **받은 값은 싣지 않는다**
   - **태스크별 상태의 키를 (호출자, `task_id`) 로 잡는다** (`TaskRequest.key`): 멱등 캐시와 진행 중
     태스크 표. Control API 로 온 그래프 · direct 태스크는 호출자가 없는 별도 공간이다. 같은 호출자의
     같은 id 재호출만 멱등이다. `task_id` 로 상태를 쥐는 새 자리는 반드시 이 키를 쓴다
   - **보장은 호출자 확인만큼이다**: 두 모드 공통으로 peer 는 Control 공간(`caller=None`)의 결과 ·
     상태에 닿지 못한다. peer 와 peer 사이의 분리는 레지스트리 모드에서만 성립한다 — 토큰 모드의
     호출자는 배포가 공유하는 서명 키로 확인한 헤더 주장이라 다른 peer 가 그 이름을 댈 수 있다
     (#415). SDK 의 task 조회 · 취소 메서드(`GetTask` / `ListTasks` / `CancelTask`)는 이 분리의
     밖이다 (#414)
   - 로그 · 메트릭 · 응답의 `task_id` / `a2a_task_id` 는 보낸 쪽이 준 그대로다 — 갈리는 것은 내부
     키뿐이다

### When to Use A2A vs Graph Edge

| 상황 | 사용 |
|---|---|
| 워크플로의 정해진 다음 단계로 데이터 전달 | **Graph edge** (state 경유) |
| 실행 도중 peer 에이전트에게 부분 작업 위임 후 결과를 이어서 사용 | **A2A call** |
| 다른 에이전트에게 질의만 하고 즉시 응답 필요 | **A2A call** |
| 산출물을 여러 후속 노드가 소비 | **Graph edge** (state 경유) |
| LLM 판단이 필요 없는 하위 능력 | **Skillset tool** (에이전트 남발 금지) |

A2A 는 위임/질의용이다. 파이프라인 데이터 흐름을 A2A 로 구현하는 것은 안티패턴 —
checkpoint/재개가 불가능해진다.

## MCP (Model Context Protocol) Integration

### MCP Server Declaration

에이전트가 사용할 MCP 서버는 manifest 의 `spec.mcp.servers` 에 선언한다.

```yaml
spec:
  mcp:
    servers:
      # 패턴 1 — stdio: 에이전트 컨테이너 내부에서 자식 프로세스로 실행 (기본)
      - name: filesystem
        transport: stdio
        command: ["mcp-server-filesystem", "/workspace"]
        env_allowlist: []                # 서버 프로세스에 전달할 env 키

      # 패턴 2 — sidecar: 에이전트 전용 사이드카 컨테이너 (HTTP 계열 transport)
      - name: browser
        transport: streamable-http
        sidecar:
          image: mcp/playwright:1.2.0
          resources: {cpu: "0.5", memory: 512Mi}
        # URL 은 runtime 이 사이드카 기동 후 주입 — 수동 URL 기입 금지

      # 패턴 3 — external: 외부 원격 MCP 서버 (명시적 URL)
      - name: corp-search
        transport: streamable-http
        url: https://mcp.internal.example.com/search
        auth:
          type: bearer
          token_env: CORP_SEARCH_TOKEN   # env_allowlist 에도 등록 필요
```

### MCP Rules

1. **Placement**
   - stdio 서버: 소유 에이전트 컨테이너 내부에서만 실행
   - sidecar 서버: 소유 에이전트와 1:1 — 다른 에이전트가 같은 사이드카에 접속 금지.
     사이드카 lifecycle 은 소유 에이전트를 따름 (에이전트 stop 시 함께 정리)
   - external 서버: 접속은 허용하되, 자격증명은 에이전트별로 분리 발급 권장

2. **Session Management**
   - `initialize()` 에서 세션 수립, 실패 시 에이전트 unhealthy (`MCP_001`)
   - 세션 단절 감지 시 자동 재연결 (backoff), 재연결 실패 누적 시 unhealthy
   - `shutdown()` 에서 세션/자식 프로세스 정리 — 좀비 프로세스 금지

3. **Tool Namespacing**
   - MCP tool 은 `mcp__{server}__{tool}` 로 네임스페이스 — skillset tool 과 충돌 방지
   - 동일 서버 이름 중복 선언 금지 (manifest 검증에서 차단)

4. **Tool Filtering**
   - 서버가 노출하는 tool 전체를 무조건 바인딩하지 않는다 —
     `allowed_tools` 로 필요한 tool 만 선별 가능 (기본: 전체 허용, 명시 시 allowlist)
   ```yaml
   - name: filesystem
     transport: stdio
     command: ["mcp-server-filesystem", "/workspace"]
     allowed_tools: [read_file, list_directory]   # write 계열 차단
   ```

5. **Security**
   - stdio `command` 는 이미지에 설치된 실행 파일만 — 셸 문자열 (`sh -c`) 금지
   - 서버 프로세스에 전달되는 env 는 `env_allowlist` 로 제한
   - MCP 서버가 반환한 컨텐츠는 **untrusted input** — 프롬프트에 주입 시 경계 표시,
     서버 응답 내 지시문을 시스템 지시로 승격 금지 (이 규칙은 기본 루프의 시스템 자리에 프레임워크
     지시로도 실린다 — [04](04-module-system.md) Promptset Rules 6). 결정 모델의 입력 판별
     ([02](02-agent-implementation.md) Decision Hooks) 은 이 경계 위의 **신호**다 —
     표시를 강화할 뿐, 경계 자체는 결정 모델 없이도 유지된다

6. **Resources & Prompts**
   - MCP resources/prompts 기능 사용 시에도 동일한 격리·선언 규칙 적용
   - Resource 구독(subscription)은 태스크 lifecycle 내에서만 — 태스크 종료 시 해제

### MCP Startup Sequence

```
agentd bootstrap
  ├─ 1. manifest 로드 + 검증
  ├─ 2. promptset / skillset 로드
  ├─ 3. MCP 서버 기동 (stdio spawn / sidecar 대기 / external 접속)
  │     └─ 각 서버 initialize + tool 목록 수집 (timeout: 15s/서버)
  ├─ 4. tool registry 구성 (skillset + mcp, 네임스페이스 충돌 검사)
  ├─ 5. A2A 서버 기동 (enabled 시)
  └─ 6. health Ready 보고
```

- 3단계에서 하나라도 실패하면 Ready 로 전환하지 않는다 (부분 기동 금지)
- 단, manifest 에 `optional: true` 로 표시된 서버는 실패해도 기동 지속 + WARN 로그

## Egress — 외부로 나가는 모든 호출

에이전트는 외부 경로가 없는 내부 네트워크에만 있다 ([02-agent-implementation.md](02-agent-implementation.md)
Network). 외부로 나가는 호출은 전부 **egress proxy** 를 거치고, 프록시가 요청마다
레지스트리 판정을 적용한다.

### 무엇을 어디까지 보는가

| 대상 | 프록시 동작 | 판정 단위 |
|---|---|---|
| 모델 API | base URL 로 **종단** — 에이전트는 프록시를 provider 로 부른다 | 요청, 자격증명 주입 |
| 결정 모델 API | **HTTPS** base URL 로 종단 — 모델 API 와 같은 규칙 ([01](01-architecture.md) Decision Models). HTTP 는 명시된 private 목적지와 명시적 plaintext opt-in(테스트 대역·내부망)에서만 | 요청, 자격증명 주입 |
| 원격 MCP (sidecar / external) | base URL 로 **종단** | **도구 이름** (`mcp_tool`), 자격증명 주입 |
| 그 밖의 외부 HTTPS | CONNECT 터널 | 목적지 호스트 (`egress`) |

1. **TLS 가로채기는 하지 않는다**: 종단하지 않는 HTTPS 는 CONNECT 목적지까지만 본다.
   에이전트에 프록시 CA 를 심지 않는다
2. **자격증명은 프록시가 주입한다**: 종단하는 서비스의 키는 에이전트 env 에 없다
   ([02-agent-implementation.md](02-agent-implementation.md) Secrets Injection)
3. **stdio MCP 와 스킬 코드**: 그 외부 효과는 목적지 판정으로 통제된다. 로컬 효과만 있는
   동작은 실시간 통제 대상이 아니다
4. **프록시 자체의 격리**: 프록시는 비밀값을 모두 쥐므로 최소 권한으로 따로 격리한다.
   도구 코드를 실행하지 않고, 에이전트 네트워크 쪽에는 프록시 포트만 연다

### 공유 프록시에서 사이드카로 옮기는 계기

v0.1 은 **전달 전용 공유 프록시** 하나로 시작한다. 격리 요구가 커지면 에이전트별 사이드카
프록시로 옮긴다 — 강제 지점의 계약(판정 조회·캐시·장애 시 동작)은 같다. 계기 예:

- 서로 신뢰하지 않는 테넌트의 에이전트가 한 호스트에 올라올 때
- 프록시 침해의 영향 범위(모든 에이전트의 자격증명)를 받아들일 수 없을 때
- 에이전트마다 이그레스 규칙이 크게 갈라져 공유 프록시 설정이 복잡해질 때

## Protocol Error Mapping

프로토콜 계층에서 발생하는 에러는 boundary 에서 `AaestroError` 로 변환한다.
상세 규칙은 [05-error-handling.md](05-error-handling.md).

| 상황 | Category | Code | Retryable |
|---|---|---|---|
| MCP 서버 기동 실패 | `mcp` | `MCP_001` | 아니오 (설정 문제) |
| MCP tool 미존재 | `mcp` | `MCP_002` | 아니오 |
| MCP tool 실행 실패 | `mcp` | `MCP_003` | tool 에 따라 |
| MCP transport 단절 | `mcp` | `MCP_004` | 예 (재연결 후) |
| A2A task 제출 실패 | `a2a` | `A2A_001` | 예 |
| A2A peer 도달 불가 | `a2a` | `A2A_002` | 예 |
| A2A task 거부/실패 | `a2a` | `A2A_003` | 아니오 |
| A2A allowlist 위반 | `a2a` | `A2A_004` | 아니오 |
| A2A 호출 깊이 초과 | `a2a` | `A2A_005` | 아니오 |
| A2A 본문 필수 필드 누락 (callee 측 거절) | `validation` | `VAL_001` | 아니오 |
| A2A 본문 · 식별자 형식 오류 (callee 측 거절) | `validation` | `VAL_002` | 아니오 |
| 이그레스 목적지·원격 MCP 도구 판정 거부 | `forbidden` | `ACC_001` | 아니오 |
| 판정 불가 (레지스트리 도달 불가, 캐시 없음) | `network` | `ACC_002` | 예 |

## Version Pinning

1. **SDK Versions**: `a2a-sdk`, `mcp` 패키지는 lockfile 로 고정 — 프로토콜 SDK 의
   자동 업그레이드 금지 (breaking change 빈도 높음)
2. **Protocol Version Negotiation**
   - MCP: initialize 시 서버가 제시한 protocol version 기록 (로그 + 메트릭)
   - 지원 범위 밖 버전이면 `MCP_001` 로 실패 — silent degradation 금지
3. **Sidecar Images**: MCP 사이드카 이미지는 semver 태그 고정 — `latest` 금지

## Observability Requirements

프로토콜 계층의 모든 원격 호출은 다음을 남긴다:

```python
log.info(
    "mcp tool call completed",
    agent=self.name,
    mcp_server="filesystem",
    tool="read_file",
    duration_ms=elapsed_ms,
    task_id=task.task_id,
)
```

- **A2A**: caller, callee, a2a_task_id, duration_ms, status
- **MCP**: mcp_server, tool, duration_ms, status
- 메트릭: `aaestro_a2a_calls_total{caller,callee,status}`,
  `aaestro_mcp_tool_calls_total{agent,server,tool,status}`
- Trace: TraceContext 를 A2A 호출에 전파 — run 전체를 단일 trace 로 연결
