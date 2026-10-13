# Testing and Quality Assurance Rules

## Testing Philosophy

### Core Principles

1. **Test Coverage**
   - **강제 게이트는 하나**: `src/aaestro` 전체 기준 70% — `make test` 의
     pytest-cov `--cov-fail-under=70` 가 유일한 CI 강제 수단
   - 90%+ coverage for critical paths (`core/`, `orchestrator/`, protocol boundaries)
     — 목표치 (리뷰에서 확인, CI 강제 아님)
   - 100% coverage for error handling paths (boundary 변환 로직) — 목표치, 신규
     boundary 코드는 리뷰에서 에러 경로 테스트 부재 시 반려

2. **Test Pyramid**
   ```
        E2E Tests (5%)
       ┌─────────────┐
       │Integration  │ (25%)
       ├─────────────┤
       │   Unit      │ (70%)
       └─────────────┘
   ```

3. **Determinism Around Non-Determinism**
   - 테스트는 실제 LLM 을 호출하지 않는다 — fake model / 기록된 응답 사용
   - 결정 모델도 같다 — `FakeDecisionModel` (스크립트된 확률) 사용. calibration 만 예외로,
     실 provider 호출은 명시 표시된 별도 작업이다 (Calibration 절)
   - 테스트는 외부 서비스에 의존하지 않는다 — 컨테이너 fixture 또는 mock
   - 테스트는 결정적이고 병렬 실행 가능해야 한다

4. **Test Naming**
   ```python
   # Pattern: test_<function>_<scenario>_<expected>
   def test_invoke_valid_task_returns_completed(): ...
   def test_invoke_timeout_returns_to_001(): ...
   def test_call_tool_transport_lost_raises_retryable_mcp_004(): ...
   ```

## Test Organization

### Directory Structure

모든 테스트는 `tests/` 아래에 위치하며 **소스 구조를 미러링**한다. `src/` 안에 테스트 파일을
두지 않는다.

```
tests/
├── unit/                          # 외부 의존 없음, 빠름
│   ├── core/
│   │   ├── test_manifest.py       # ← src/aaestro/core/manifest.py
│   │   └── test_errors.py
│   ├── orchestrator/
│   │   ├── test_builder.py
│   │   └── test_topology.py
│   ├── protocols/
│   │   ├── a2a/test_client.py
│   │   └── mcp/test_client.py
│   ├── modules/
│   │   ├── test_skillset.py
│   │   └── test_promptset.py
│   ├── agentd/test_executor.py
│   └── decision/
│       ├── test_bands.py          # 확률 → 구간
│       └── test_uses.py           # 회상 필터 / 입력 판별 / 도구 게이트 — FakeDecisionModel
│
├── integration/                   # Docker/실서버 fixture 사용, 느림
│   ├── runtime/test_docker_lifecycle.py
│   ├── protocols/test_mcp_stdio_session.py
│   └── orchestrator/test_graph_run.py
│
├── e2e/                           # 전체 스택 (compose) — CI nightly
│   └── test_research_pipeline.py
│
├── fixtures/                      # 공유 fixture / builder / fake
│   ├── fake_model.py
│   ├── fake_decision.py           # FakeDecisionModel — 질문별 스크립트된 확률
│   ├── fake_mcp_server.py
│   ├── builders.py
│   └── manifests/                 # 테스트용 manifest/graph yaml
└── conftest.py
```

- 마커: `@pytest.mark.integration`, `@pytest.mark.e2e` — 기본 실행(`make test`)은 unit 만,
  `make test-integration` / `make test-e2e` 로 분리 실행

### Frameworks

1. **Runner**: pytest + pytest-asyncio (`asyncio_mode = "auto"`)
2. **Assertions**: 표준 assert (pytest rewriting)
3. **Mocking**: `unittest.mock` / pytest fixture — 남용 금지, 계약 경계에서만
4. **Containers**: testcontainers-python
5. **Coverage**: pytest-cov

## Unit Testing

### What to Test

1. **core/**: manifest/topology/group 스키마 검증 (유효/무효 케이스 전수 —
   `group: global` 직접 선언 거부 포함), 스코프 해석 (secrets local > group > global),
   에러 타입 동작
2. **orchestrator/**: config → StateGraph 빌드 결과, edge 조건 라우팅, state 병합 규칙,
   토폴로지 검증기의 각 실패 케이스
3. **protocols/**: 에러 변환 (원 예외 → AaestroError 카테고리/코드/retryable),
   allowlist 검사, tool 네임스페이싱
4. **modules/**: ref 파싱/해석, skillset schema 자동 생성, promptset 변수 검증·렌더링,
   memoryset 정책 스키마 검증
5. **memory/**: space ACL (비멤버 group 접근 / global write / ro append → `MEM_001`),
   별칭 스코프 해석 (local > group > global), 하이브리드 검색 병합(RRF), recall
   예산/threshold, supersedes 체인 — fake embedder (결정적 해시) 사용,
   실제 embedding API 호출 금지 ([09-memory-context.md](09-memory-context.md))
6. **agentd/**: tool loop (max turns, 병렬 tool, usage 집계), cancellation 처리,
   direct 태스크 처리 (`node_id=None` → `default` 템플릿 선택, graph state 불간섭)
7. **decision/**: 구간 판정 (predicate / rating 누적 확률 / choice 1위), 세 쓰임 각각의
   act·uncertain·reject·unavailable 네 경로, provider 어댑터의 에러 변환 (`DEC_*`), 값 집합 밖
   응답이 `DEC_004` 인지, decision 노드 태스크가 `decision.` 출력만 내는지

### Fake Model — LLM 호출 테스트의 표준

```python
class FakeModel:
    """스크립트된 응답을 순서대로 반환하는 모델 대역."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = iter(responses)
        self.calls: list[ModelCall] = []       # 검증용 호출 기록

    async def run(self, prompt: Prompt, tools: list[Tool]) -> ModelResponse:
        self.calls.append(ModelCall(prompt=prompt, tools=tools))
        return next(self._responses)


async def test_executor_runs_tool_then_completes():
    model = FakeModel([
        ModelResponse.tool_call("mcp__fs__read_file", {"path": "a.txt"}),
        ModelResponse.text("done"),
    ])
    executor = Executor(model=model, tools=[fake_read_file])

    result = await executor.execute(make_task(input={"query": "read a"}))

    assert result.status == TaskStatus.COMPLETED
    assert len(model.calls) == 2
```

- 실제 provider SDK 호출 금지 — CI 에서 API key 부재로 실패해야 정상
- 실 응답 기반 회귀가 필요하면 기록/재생(cassette) fixture 를 `tests/fixtures/` 에 저장

### Fake Decision Model — 결정 모델 테스트의 표준

```python
class FakeDecisionModel:
    """질문별로 스크립트된 확률을 돌려주는 결정 모델 대역 — 구간은 실제 코드가 읽는다."""

    def __init__(self, answers: dict[str, float | Sequence[float]]) -> None:
        self._answers = answers                # question → predicate 확률 / rating·choice 분포
        self.asked: list[tuple[str, str]] = []  # (question, state) 검증용

    async def decide(self, state: str, questions: Sequence[Question]) -> list[Decision]:
        self.asked.extend((q.name, state) for q in questions)
        return [decision_from(q, self._answers[q.name]) for q in questions]


async def test_recall_filter_drops_what_the_model_rejects():
    decision = FakeDecisionModel({"triage.memory_is_relevant": 0.05})
    executor = Executor(model=FakeModel([text("done")]), decision=decision, recall=recall_of(2))

    await executor.execute(make_task())

    assert executor.injected_memories == []
    assert len(decision.asked) == 1        # 상위 k 를 한 요청에 — 항목마다 부르지 않는다
```

- **네 경로 전부**: 각 쓰임은 `act` / `uncertain` / `reject` / `unavailable` 을 다 테스트한다.
  특히 `unavailable` 이 원래 경로와 **같은 결과**를 내는지 — 결정 모델을 빼도 답이 같아야 한다는
  01 의 원칙을 테스트가 증명한다
- **배선을 뮤테이션한다**: 쓰임을 빼도 초록이면 그 테스트는 헬퍼만 본 것이다 — 게이트를
  제거했을 때 부수효과 도구가 실행되는지, 필터를 제거했을 때 주입이 늘어나는지 본다

### Test Data — Builders and Fixtures

```python
# tests/fixtures/builders.py
def make_manifest(**overrides: Any) -> AgentManifest:
    base = AgentManifest(
        metadata=Metadata(name="test-agent", version="0.1.0"),
        spec=Spec(
            model=ModelConfig(provider="anthropic", name="claude-sonnet-5"),
            promptset=Ref("promptsets/test@0.1.0"),
        ),
    )
    return base.model_copy(update=overrides)


def make_task(**overrides: Any) -> TaskRequest: ...
def make_graph(nodes: list[str], edges: list[tuple[str, str]]) -> GraphTopology: ...
```

- 테스트용 yaml manifest/graph 는 `tests/fixtures/manifests/` 에 — 실제 스키마로 로드해
  사용 (스키마와 fixture 의 드리프트 방지)

### Table-Driven Tests

다중 시나리오는 `pytest.mark.parametrize` 사용:

```python
@pytest.mark.parametrize(
    ("ref", "expected_type", "expected_name", "expected_version"),
    [
        ("skillsets/web-search@0.2.0", "skillsets", "web-search", "0.2.0"),
        ("agents/planner@1.0.0", "agents", "planner", "1.0.0"),
    ],
)
def test_parse_ref_valid(ref, expected_type, expected_name, expected_version):
    parsed = ModuleRef.parse(ref)
    assert (parsed.type, parsed.name, parsed.version) == (
        expected_type, expected_name, expected_version)


@pytest.mark.parametrize("ref", ["web-search", "skillsets/x@latest", "skillsets/@1.0.0"])
def test_parse_ref_invalid_raises(ref):
    with pytest.raises(AaestroError) as exc_info:
        ModuleRef.parse(ref)
    assert exc_info.value.code == "MOD_001"
```

## Integration Testing

### Docker Runtime Tests

```python
@pytest.mark.integration
async def test_agent_container_lifecycle(docker_runtime):
    manifest = load_fixture_manifest("echo-agent")

    handle = await docker_runtime.start(manifest)
    try:
        health = await wait_for_ready(docker_runtime, handle, timeout=30)
        assert health.status == "healthy"

        result = await docker_runtime.invoke(handle, make_task(input={"msg": "hi"}))
        assert result.status == TaskStatus.COMPLETED
    finally:
        await docker_runtime.stop(handle)
```

- 테스트 전용 경량 에이전트 이미지 (`aaestro/agent-echo`) 를 fixture 로 유지 —
  모델 호출 없이 입력을 echo (프로토콜/lifecycle 검증용)
- 테스트 후 컨테이너/네트워크 정리 보장 (fixture finalizer)
- 그룹 quota 초과 manifest 기동 시도 → `RT_006` 으로 거부되는지 검증

### MCP Session Tests

```python
@pytest.mark.integration
async def test_mcp_stdio_session_lists_tools(tmp_path):
    config = McpServerConfig(
        name="fs", transport="stdio",
        command=["mcp-server-filesystem", str(tmp_path)],
    )

    async with McpSession.launch(config, agent_name="test") as session:
        tools = await session.list_tools()
        assert any(t.name == "mcp__fs__read_file" for t in tools)
```

- 실제 참조 MCP 서버(filesystem 등)로 세션 수립/단절/재연결 시나리오 검증
- Fake MCP 서버 (`tests/fixtures/fake_mcp_server.py`) 로 오류 응답/지연 시나리오 검증

### Graph-Level Tests

그래프 라우팅은 **컨테이너 없이** 검증한다 — runtime 을 fake 로 치환:

```python
@pytest.mark.integration
async def test_graph_conditional_routing(fake_runtime):
    fake_runtime.script("planner", output={"plan": "...", "needs_research": False})
    graph = build_graph(load_fixture_graph("research-pipeline"), runtime=fake_runtime)

    final_state = await graph.ainvoke({"query": "q"})

    assert fake_runtime.invoked == ["planner"]     # researcher 스킵 확인
    assert "plan" in final_state
```

- Checkpointer 는 in-memory 사용 (`MemorySaver`)
- 재개 시나리오: node 실패 → 동일 checkpoint 에서 resume → 성공 경로 검증 필수
- **Service 모드 필수 시나리오** (fake clock 주입 — 실제 sleep 금지):
  - Iteration N 회 반복 후 state 누적 검증, iteration 마다 checkpoint 기록 확인
  - Idle 시 exponential backoff 진행 (min → max 상한 도달) / 작업 감지 시 backoff reset
  - Drain 요청 시 진행 중 iteration 완료 후 정지, 재시작 시 다음 iteration 부터 재개
  - `max_failure_streak` 도달 시 GRAPH_005 로 정지 + `status="halted"` 기록

## End-to-End Testing

```python
@pytest.mark.e2e
async def test_research_pipeline_full_stack(compose_stack):
    """compose 로 전체 스택 기동 후 실제 그래프 run 검증. 모델은 fake provider 컨테이너."""
    client = AaestroClient(compose_stack.control_plane_url)

    run = await client.submit("research-pipeline", {"query": "test"})
    result = await client.wait(run.run_id, timeout=120)

    assert result.status == "completed"
    assert result.state["report"]
```

- E2E 에서도 실제 LLM 금지 — OpenAI/Anthropic 호환 fake provider 컨테이너 사용
- 결정 모델도 같은 대역이 낸다 — fake provider 가 결정 API 경로를 하나 더 열고, 질문 문구에
  포함된 지시(`answer 0.95` 등)나 해시로 결정적 확률을 돌려준다. E2E 는 세 쓰임이 실제 컨테이너
  경계(프록시 종단 포함)를 지나는지 본다
- CI 에서는 nightly 로만 실행 (PR gate 는 unit + integration)

## Calibration — 구간은 라벨 데이터가 정한다

decisionset 의 `act_at` / `reject_at` 은 추측이 아니라 **라벨 데이터**로 정한다
([04-module-system.md](04-module-system.md) Decisionset Rules 2).

1. **라벨 세트**: 질문마다 `modules/decisionsets/<name>/<ver>/calibration/<question>.jsonl` —
   한 줄이 `{"state": {...}, "label": <gold>}` 이고 `label` 의 형태는 질문 종류가 정한다:
   `predicate` 는 `true` / `false`, `rating` 은 선언된 등급 이름 하나(`"acceptable"`),
   `choice` 는 선언된 보기 하나(`"bug"`). 선언 밖의 값이 있으면 로더가 거부한다 (`MOD_003`).
   최소 50건, `predicate` 는 양쪽 라벨, `rating` / `choice` 는 모든 등급·보기가 나오게. **실제 태스크·기억·
   도구 결과를 그대로 넣지 않는다** — 모듈은 git 에 실리고, 비밀값이 아니라도 개인정보·테넌트
   기밀이 섞인다. 합성 예시이거나 되돌릴 수 없게 비식별화한 것만 두고, 실 데이터에서 파생한
   세트는 커밋 전에 프라이버시 검토를 거친다 (PR 본문에 검토자 명시)
2. **구간 계산**: `aaestro decision calibrate <ref>` 가 실 provider 로 라벨 세트를 돌려 구간별
   정밀도 표를 낸다. 이것이 **유일한 실 provider 호출**이고, 비용이 드는 명시적 작업이다
   (08 규약 5 — 실 API 호출은 사전 확인). CI 에서는 돌지 않는다
3. **locale 검증**: provider 의 `locales` 는 그 언어의 라벨 세트로 calibration 을 통과한
   언어만 적는다. 한국어 질문을 쓰려면 한국어 라벨 세트로 먼저 확인한다 — provider 문서가
   말하지 않는 것을 믿지 않는다
4. **회귀**: calibration 결과(구간별 정밀도)는 `calibration/report.json` 으로 남긴다. 유닛
   테스트는 이 파일을 읽어 선언된 구간이 보고된 정밀도 안에 있는지만 확인한다 — provider 를
   부르지 않는다
5. **재보정 시점**: `ask` 문구를 바꿨을 때, provider·모델 id 를 바꿨을 때,
   `DecisionModelMostlyUncertain` 알림이 울렸을 때

## Testing Access Control

권한 통제 기능([01-architecture.md](01-architecture.md) Access Control)은 "막혔다" 만으로는
증명되지 않는다 — 실시간이라는 것까지 증명해야 한다.

1. **재시작 없이 반영**: 권한 변경 전후로 대상 컨테이너의 시작 시각이 같아야 한다
   (`docker inspect -f '{{.State.StartedAt}}'`). 실 스택 E2E 필수
2. **다음 요청부터**: 회수 직후의 다음 요청이 거부되고, 되돌리면 재배포 없이 성공한다
3. **컨테이너 안 우회**: 호출자 쪽 검사를 우회해도(직접 요청) 강제 지점에서 거부되는지 본다
4. **장애 시 동작**: 레지스트리를 멈춘 상태에서 캐시에 없는 판정은 거부, 이미 허용된 판정은
   유지되는지 둘 다 본다
5. **확장 상한**: 상한 초과 요청, 요청 본문에 상한을 무시하라는 지시를 넣은 요청이 모두 거절되고
   기록되는지 본다
6. 판정 로직 단위 테스트는 fake clock 과 fake 레지스트리로 — 캐시 만료를 실제로 기다리지 않는다

## Testing Async / Concurrent Code

1. `pytest-asyncio` auto mode — `async def test_*` 그대로 작성
2. 시간 의존 로직(백오프, health 주기)은 sleep 하지 않는다 — clock 주입 또는
   `delay_fn` 파라미터화로 즉시 진행
3. Cancellation 테스트 필수 경로: 태스크 취소 시 tool 정리, drain 중 신규 태스크 거부
4. 동시성 검증: `asyncio.gather` 병렬 tool 실행, 세마포어 상한 동작

```python
async def test_cancel_inflight_task_cleans_up_tools():
    started, cleaned = asyncio.Event(), asyncio.Event()
    executor = Executor(model=slow_model(started), on_cleanup=cleaned.set)

    task = asyncio.create_task(executor.execute(make_task()))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
```

## Module Tests

### Skillset

- skill 함수 단위 테스트 (SkillContext 는 fake)
- **Schema snapshot**: 시그니처 → tool schema 자동 생성 결과를 스냅샷으로 고정 —
  의도치 않은 스키마 변경(=모델이 보는 계약 변경) 감지

### Promptset

- 변수 검증: 필수 변수 누락 시 `MOD_004`
- **Golden test**: 대표 입력에 대한 렌더 결과를 golden 파일로 고정 —
  프롬프트 변경이 diff 로 보이게 (버전 bump 강제와 짝을 이룸)

### Graph

- 토폴로지 검증기: dangling edge / 미도달 노드 / cycle without max_iterations 등
  각 실패 규칙별 케이스
- fake runtime 라우팅 시나리오 (위 Graph-Level Tests)

## Benchmarking

- 프레임워크 오버헤드 벤치마크: Control API 왕복, state 병합, 템플릿 렌더
- `pytest-benchmark` 사용, 회귀 임계 초과 시 CI 경고

```python
def test_bench_topology_validation(benchmark):
    topology = load_fixture_graph("large-50-nodes")
    benchmark(validate_topology, topology)
```

## Quality Gates

### Linting & Type Check

```toml
# pyproject.toml (발췌)
[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "W", "I", "N", "UP", "B", "ASYNC", "S", "C4", "RET", "SIM", "PTH", "TID252"]

[tool.mypy]
python_version = "3.12"
strict = true                      # src/aaestro/core 는 strict 필수
```

### Pre-Merge Checklist (CI 강제)

```bash
make lint        # ruff check + ruff format --check
make typecheck   # mypy
make test        # pytest tests/unit (커버리지 포함)
make test-integration   # PR gate (Docker 가용 환경)
```

- Coverage < 70% 시 CI 실패
- 신규 boundary 코드에 에러 경로 테스트 없으면 리뷰에서 반려

### CI Workflow

실제 워크플로는 `.github/workflows/` 에 구성되어 있다 — job 이름과 머지 게이트 연결은
`docs/en/ci/status-checks.md` (단일 소스) 를 따른다:

- `ci-quality.yml` — Lint / Type Check / Test (coverage gate) / Integration Test.
  코드 부재(부트스트랩 단계) 시 가드로 skip 되어 게이트를 통과
- `ci-convention.yml` — Commit Lint / PR Title Lint / Linked Issue Check
  (+ 정보성 Branch Name Lint)
- `ci-docs.yml` — Docs Sync Check (docs/en ↔ docs/ko 구조 미러 + 언어 선택자)
- `ci-nightly.yml` — E2E Test (schedule 02:00 KST, PR gate 아님)

## Smoke Testing

배포 직후 실행하는 최소 검증:

1. `aaestro status` — 모든 에이전트 healthy
2. 각 그래프의 canary run 1회 (fake 입력) — END 도달 확인
3. 메트릭 endpoint 응답 확인

## Test Documentation

- 테스트 전략/시나리오 문서: `docs/en/testing.md` (+ `docs/ko/`)
- Known limitations 추적: 재현 불가 케이스, 실 LLM 에서만 발생하는 이슈 등을
  문서화하고 관련 테스트 이름을 연결
