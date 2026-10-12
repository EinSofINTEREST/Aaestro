# Code Style and Conventions

## Core Principles

### Readability First
- Code should be self-documenting
- Clear naming over clever code
- Simplicity over complexity
- Remove unnecessary code

### Minimal Comments
- Only comment WHY, not WHAT
- Code should explain itself through naming
- Remove commented-out code
- Avoid redundant comments

### No Over-Engineering
- Solve current problems, not future ones
- Avoid premature abstraction
- Delete unused code completely
- Keep it simple

## Python Style Guide

기준: PEP 8 + ruff. 포맷팅 논쟁은 도구가 끝낸다 — `ruff format` 결과가 정답.

### Formatting

1. **Indentation**: 4 spaces (Python 표준)
2. **Line Length**: Max 100 characters
3. **Formatter**: `ruff format` — 수동 정렬/개행 금지, 커밋 전 실행
4. **Imports**: ruff(isort) 그룹 순서 — 표준 라이브러리 → 서드파티 → 로컬

   ```python
   import asyncio
   from datetime import datetime

   import structlog
   from pydantic import BaseModel

   from aaestro.core.errors import ErrorCategory, AaestroError
   from aaestro.core.manifest import AgentManifest
   ```

   - `from module import *` 금지
   - 상대 import 금지 — 항상 `aaestro.` 절대 경로

### Naming Conventions

1. **Variables / Functions**: snake_case, descriptive
   ```python
   # Good
   task_count = 10
   max_attempts = 3

   async def resolve_module_ref(ref: str) -> ModulePath: ...

   # Bad
   tc = 10
   maxAttempts = 3
   ```

2. **Constants**: UPPER_SNAKE_CASE (모듈 최상위)
   ```python
   DEFAULT_TIMEOUT_S = 30.0
   MAX_TOOL_TURNS = 20
   ```

3. **Classes**: PascalCase, 단수 명사
   ```python
   class AgentManifest(BaseModel): ...
   class McpSession: ...

   # Bad — 접미사 불필요
   class AgentManifestClass: ...
   class RuntimeInterface(Protocol): ...   # "Interface" 접미사 금지 → Runtime
   ```

4. **Protocols / ABCs**: 역할 명사 그대로 (`Runtime`, `Checkpointer`, `ModuleLoader`)
   — `I` 접두사, `Interface`/`Base` 접미사 금지 (단 `BaseAgent` 는 프레임워크 공개 계약명으로 예외)

5. **Private**: 단일 언더스코어 `_internal_helper` — 이중 언더스코어(name mangling) 금지

6. **Async 함수**: 이름에 `async_`/`_async` 붙이지 않는다 — 시그니처가 이미 말해준다

### Type Hints

1. **공개 API 는 type hint 필수** — 함수 시그니처, 클래스 속성 전부
2. Python 3.12 문법 사용: `list[str]`, `dict[str, Any]`, `str | None` (Optional 금지)
3. `Any` 는 경계(직렬화 입출력)에서만 — 내부 로직에 `Any` 전파 금지
4. `src/aaestro/core/` 는 mypy strict 통과 필수
5. 복잡한 dict 대신 pydantic 모델 또는 `@dataclass` — shape 를 타입으로 표현

```python
# Good
async def invoke(self, task: TaskRequest) -> TaskResult: ...

# Bad — dict 로 계약 흐리기
async def invoke(self, task: dict) -> dict: ...
```

### Error Handling

1. **Early Returns**: 가드 절 우선, 깊은 중첩 금지
   ```python
   # Good
   def validate(self, topology: GraphTopology) -> None:
       if not topology.nodes:
           raise AaestroError(..., code="GRAPH_001", message="graph has no nodes")
       if topology.entry not in topology.node_ids:
           raise AaestroError(..., code="GRAPH_001", message="entry node not found")
       ...
   ```

2. **에러 메시지**: 소문자, 마침표 없음, 영어
   ```python
   # Good
   raise AaestroError(..., message="failed to resolve module ref")
   # Bad
   raise AaestroError(..., message="Failed to resolve module ref.")
   ```

3. **금지 패턴**
   ```python
   # Bad — bare except
   try: ...
   except: ...

   # Bad — 삼키기
   except McpError:
       pass

   # Bad — Exception 광역 캐치 (최상위 boundary 핸들러 제외)
   except Exception: ...
   ```

4. **원인 체인 보존**: 변환 시 반드시 `raise ... from err`
5. Boundary 변환 규칙은 [05-error-handling.md](05-error-handling.md) 를 따른다

### Async Conventions

1. **Async-first**: I/O 가 있는 공개 API 는 모두 `async def`
2. Blocking 호출(파일 대량 I/O, CPU 작업, sync SDK)은 `asyncio.to_thread` 로 감싼다 —
   이벤트 루프 blocking 금지
3. **취소 안전성**: cleanup 은 `try/finally` 또는 async context manager 로 보장
4. `asyncio.gather` 사용 시 예외 정책 명시 — 부분 실패 허용이면
   `return_exceptions=True` + 개별 처리
5. Fire-and-forget task 금지 — 모든 task 는 소유자가 lifecycle 관리
   (`TaskGroup` 권장)
6. 세마포어/락은 생성 위치와 보호 대상을 docstring 에 명시

### Function Design

1. **Small Functions**: Max 50 lines — 초과 시 분리
2. **Parameter Count**: Max 5 — 초과 시 pydantic 모델/dataclass 로 묶기
   ```python
   # Good
   async def start(self, manifest: AgentManifest, opts: StartOptions) -> AgentHandle: ...
   ```
3. **Keyword-only 권장**: 불리언/옵션 인자는 `*,` 이후에
   ```python
   async def stop(self, handle: AgentHandle, *, force: bool = False) -> None: ...
   ```
4. **반환 일관성**: 같은 함수에서 `None`/값 혼합 반환으로 의미 표현 금지 —
   부재는 예외 또는 명시적 `| None` 타입으로

### Pydantic Model Design

1. 모든 대외 계약 (manifest, topology, Task*, API 입출력) 은 pydantic v2 모델
2. `model_config = ConfigDict(frozen=True)` 기본 — 불변 계약, 변경은 `model_copy`
3. 필드 검증은 모델 안에 (`field_validator`) — 사용처에 검증 로직 흩뿌리기 금지
4. 직렬화 별칭 최소화 — 필드명 자체를 snake_case 계약으로

```python
class AgentManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    api_version: Literal["aaestro/v1"]
    kind: Literal["Agent"]
    metadata: Metadata
    spec: AgentSpec
```

### Comments

**Language Policy**:
- **Primary**: Korean (한국어)
- **Secondary**: English (영어)
- **Style**: 한국어 설명 + 영어 기술 용어 자연 혼용

**Principles**:
- Keep comments minimal and essential
- Explain WHY, never WHAT

1. **Inline Comments**: 이유를 설명
   ```python
   # Good — 이유를 설명
   # provider SDK 자체 재시도와 중복되면 backoff 가 곱해지므로 비활성화
   client = Anthropic(max_retries=0)

   # Bad — 당연한 내용
   # 클라이언트를 생성한다
   client = Anthropic(max_retries=0)
   ```

2. **TODO Comments**: 맥락 포함
   ```python
   # Good
   # TODO(juhyuni): registry backend 를 Postgres 로 교체 시 여기 캐시 무효화 필요
   # Bad
   # TODO: fix this
   ```

### Docstrings

- **공개 API (프레임워크 계약)**: English first + Korean, Google style
- **내부 함수**: 복잡한 경우에만, 한국어 허용

```python
async def resolve(self, ref: str) -> ModulePath:
    """Resolve a module reference to a filesystem path.

    모듈 참조 문자열을 실제 경로로 해석합니다.
    게시된 버전 디렉토리가 없으면 MOD_001 을 발생시킵니다.

    Args:
        ref: Module reference in ``{type}/{name}@{version}`` format.

    Returns:
        Resolved module path with verified metadata.

    Raises:
        AaestroError: MOD_001 if the reference cannot be resolved.
    """
```

### Module / Package Design

1. **Package Names**: 짧은 소문자 단수 (`runtime`, `orchestrator`, `protocols`)
2. **File Names**: snake_case (`docker_runtime.py`, `mcp_client.py`)
3. **의존 방향**: `core` ← 나머지 전부. 역방향 import 금지
   - `orchestrator` ↛ `runtime.docker` (runtime 추상 계약만 의존)
   - `protocols` ↛ `orchestrator`
4. `__init__.py` 는 공개 심볼 re-export 만 — 로직 금지.
   **하위 모듈과 이름이 겹치는 심볼은 re-export 하지 않는다** — 겹치면 그 모듈이
   가려져 `aaestro.core.skill` 이 모듈이 아니라 함수로 잡히고, monkeypatch·mock
   대상 지정이 직관과 어긋난다. 겹치는 심볼은 모듈에서 직접 import 한다
   (`from aaestro.core.skill import skill`)
5. 모듈 레벨 부수효과 금지 (import 시 연결/기동 금지) — 명시적 팩토리/생성자로

```python
# Bad — import 시 초기화
docker_client = docker.from_env()

# Good — 명시적 생성
class DockerRuntime:
    @classmethod
    def create(cls, config: RuntimeConfig) -> "DockerRuntime": ...
```

### File Structure

```python
# 1. Module docstring (공개 패키지인 경우)
"""Docker-based agent runtime.

Docker 기반 에이전트 런타임 구현.
"""

# 2. Imports (표준 → 서드파티 → 로컬)

# 3. Constants
DEFAULT_DRAIN_TIMEOUT_S = 30.0

# 4. Types / Models

# 5. Public classes / functions

# 6. Private helpers
```

## Configuration Files

### YAML Style

1. **Indentation**: 2 spaces
2. **Keys**: snake_case
3. **Comments**: 비자명한 값에만

```yaml
# Good
runtime:
  backend: docker
  default_resources:
    cpu: "1.0"
    memory: 1Gi
  health_check:
    interval_s: 10          # agentd /health 폴링 주기
    unhealthy_threshold: 3

# Bad
runtime:
    Backend: "docker"
    defaultResources:
        CPU: "1.0"
```

### Manifest / Graph YAML

- 스키마는 [02-agent-implementation.md](02-agent-implementation.md),
  [04-module-system.md](04-module-system.md) 참조
- 키 순서: `apiVersion` → `kind` → `metadata` → `spec` 고정
- 인라인 flow style (`{from: a, to: b}`) 은 짧은 edge 선언에만 허용

## Anti-Patterns to Avoid

### 1. Magic Numbers
```python
# Bad
if attempt > 3: ...

# Good
MAX_ATTEMPTS = 3
if attempt > MAX_ATTEMPTS: ...
```

### 2. Mutable Default Arguments
```python
# Bad
def build(self, tools: list[Tool] = []) -> ...: ...

# Good
def build(self, tools: list[Tool] | None = None) -> ...:
    tools = tools or []
```

### 3. Deep Nesting → Guard Clauses
```python
# Bad
if a:
    if b:
        if c:
            do()

# Good
if not a:
    return
if not b:
    return
if not c:
    return
do()
```

### 4. Global Mutable State
```python
# Bad — 모듈 전역 레지스트리 변형
REGISTRY: dict[str, Agent] = {}

# Good — 소유 객체 주입
class ModuleRegistry: ...
```

### 5. God Objects
- "Manager", "Helper", "Util" 성격의 만능 클래스 금지 — 역할 단위로 분리

### 6. String-Typed Contracts
```python
# Bad
status = "done"

# Good
class TaskStatus(StrEnum):
    COMPLETED = "completed"
```

## Logging Standards

구조화 로깅 상세 규칙([05-error-handling.md](05-error-handling.md))을 따른다. 요지:

```python
# Good — structured fields, 영어 메시지
log.info("agent ready", agent=self.name, agent_version=self.version,
         duration_ms=elapsed_ms)

# Bad — 보간 문자열
log.info(f"agent {self.name} ready in {elapsed_ms}ms")
```

## Git Commit Conventions

### Commit Message Format
```
[{카테고리}]: {변경 내용}
```

### 카테고리 (Categories)

- **FEAT**: feature, 기능 구현 및 추가
- **FIX**: fix, 버그 수정
- **REFAC**: refactor, 구조 변경, 메소드 구조 변경 및 리팩토링
- **DOCS**: documentation, 문서 작업 및 프롬프트 변경, 주석 등 설명 요소 작성
- **CHORE**: chore, 빌드/설정/의존성 등 코드 외적 작업

### 변경 내용 작성 규칙

**⚠️ 중요: 모든 커밋 메시지는 한국어로 작성해야 합니다.**

1. **언어**: 반드시 한국어로 작성 (영어 사용 금지)
2. **형식**: 명사형 종결 (예: "구현", "수정", "추가")
3. **내용**: 변경 내용의 전체적인 요약, 각 모듈 단위의 변경점을 명확히 기술

### Examples

```
[FEAT]: Docker 런타임 컨테이너 lifecycle 제어 구현

- DockerRuntime.start/stop/health 메소드 구현
- exponential backoff 기반 재시작 정책 추가
- 컨테이너 리소스 limit 적용 (manifest 기반)
```

```
[FIX]: MCP transport 단절 시 세션 재연결 처리 개선

- 단절 감지 시 MCP_004 retryable 에러로 변환
- 재연결 backoff 및 실패 누적 시 unhealthy 전환 로직 추가
```

```
[REFAC]: 그래프 토폴로지 검증 로직 분리

- builder 에서 topology 검증을 TopologyValidator 로 추출
- 검증 실패 케이스별 GRAPH_001 세부 메시지 정리
```

### Branch Naming Convention

```
{카테고리}/#{이슈번호}/{핵심-변경-대상-요약}
```

#### 브랜치 대분류

- **feature/**: 새로운 기능 구현 및 추가 (FEAT 커밋 대응)
- **fix/**: 버그 수정 (FIX 커밋 대응)
- **refactor/**: 구조 변경 및 리팩토링 (REFAC 커밋 대응)
- **docs/**: 문서 작업 (DOCS 커밋 대응)
- **chore/**: 설정/빌드 작업 (CHORE 커밋 대응)

#### 브랜치명 작성 규칙

1. **형식**: `{카테고리}/#{이슈번호}/{핵심-변경-대상-요약}`
2. **이슈번호**: GitHub 이슈 번호를 `#` 접두사와 함께 표기 (예: `#15`)
3. **핵심 변경 대상 요약**: 영문 소문자, 단어 구분은 하이픈(-), 30자 이내

#### Branch Name Examples

```bash
feature/#3/docker-runtime-lifecycle
feature/#4/mcp-session-manager
feature/#5/a2a-connection-allowlist
feature/#6/graph-topology-validator

fix/#12/mcp-reconnect-backoff
refactor/#9/extract-topology-validator
docs/#2/ruleset-langgraph
chore/#7/uv-lockfile-ci
```

## Test Conventions

테스트 상세는 [06-testing.md](06-testing.md). 스타일 요지:

```python
# Pattern: test_<function>_<scenario>_<expected>
async def test_start_invalid_manifest_raises_mod_003(): ...

# Arrange / Act / Assert 구조 유지
async def test_invoke_success():
    # Arrange
    runtime = make_fake_runtime()
    # Act
    result = await runtime.invoke(handle, make_task())
    # Assert
    assert result.status == TaskStatus.COMPLETED
```

## Documentation

### Language Requirements

**Code Documentation** (docstrings):
- **Primary**: English (영어)
- **Secondary**: Korean (한국어)
- **Format**: English first, then Korean explanation

**Documentation Files**:
- **MUST** write all documentation in English first
- **MUST** provide Korean translation in separate directory
- **Directory structure**:
  ```
  docs/
  ├── en/           # English documentation (primary)
  │   ├── README.md
  │   ├── architecture.md
  │   └── api.md
  └── ko/           # Korean translation
      ├── README.md
      ├── architecture.md
      └── api.md
  ```
- **Content**: Keep both versions synchronized

**README Files** (Root level):
- `README.md` - **MUST** be in English
- Link to Korean version: `docs/ko/README.md`
- Include language selector at the top:
  ```markdown
  # Aaestro

  **[한국어](docs/ko/README.md)** | English
  ```

### Documentation Best Practices

1. **Write English First** — `docs/en/` 이 source of truth
2. **Translate to Korean** — 구조 동일하게 `docs/ko/` 유지, 영어 갱신 시 동기화
3. **Language Selector** — 모든 문서 상단
   - English: `**[한국어](../ko/same-file.md)** | English`
   - Korean: `한국어 | **[English](../en/same-file.md)**`
4. **Update Policy** — 갱신 시 영어 먼저 → 한국어 동기화 →
   커밋 메시지에 표기: `[DOCS]: ... (en+ko)`
5. **Changelogs** — 영어로 작성 + 한국어 섹션 병기

## Code Review Checklist

Before submitting code for review, ensure:

- [ ] No commented-out code
- [ ] No magic numbers
- [ ] Type hints on all public APIs (mypy 통과)
- [ ] Error handling follows boundary rules (05)
- [ ] `raise ... from err` 로 원인 체인 보존
- [ ] Functions are small (< 50 lines)
- [ ] No deep nesting (< 4 levels)
- [ ] Async cleanup guaranteed (try/finally)
- [ ] Tests are included (에러 경로 포함)
- [ ] Logging includes standard context fields
- [ ] Comments explain WHY, not WHAT
- [ ] ruff format / ruff check 통과
