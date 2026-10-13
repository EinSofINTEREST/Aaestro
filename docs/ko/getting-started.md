# 시작하기

한국어 | **[English](../en/getting-started.md)**

> **상태**: 프레임워크 레이어가 구현되었고 레퍼런스 솔루션이 전 구간 검증을
> 통과합니다. `aaestro run` 은 에이전트 컨테이너가 떠 있어야 하며,
> *(미구현)* 표시된 명령은 로드맵에 있습니다.

## 사전 요구사항

- Python **3.12+**
- [uv](https://docs.astral.sh/uv/) (패키지 매니저 — lockfile 커밋)
- Docker Engine **24+** (에이전트 격리 런타임)
- `make` (작업 자동화)

## 환경 구성

```bash
git clone https://github.com/EinSofINTEREST/Aaestro.git
cd Aaestro
uv sync                 # 고정된 의존성 설치
```

로컬/CI 공통 품질 게이트:

```bash
make lint               # ruff check + ruff format --check
make typecheck          # mypy (src/aaestro/core 는 strict)
make test               # pytest unit + 커버리지 게이트 (≥ 70%)
make test-integration   # Docker 기반 통합 테스트
```

## 첫 솔루션 조립

솔루션은 **모듈 조립**으로 구성합니다 — 프레임워크 코드를 작성하는 것이 아니라 기존
에이전트와 모듈을 그래프로 배선합니다.

### 1. 에이전트 선언

```yaml
# agents/researcher/manifest.yaml
apiVersion: aaestro/v1
kind: Agent
metadata:
  name: researcher
  version: 0.1.0
  group: research               # 리소스 스코프 소속 (선택)
spec:
  model: {provider: anthropic, name: claude-sonnet-5}
  promptset: {ref: promptsets/researcher@0.1.0}
  skillsets:
    - ref: skillsets/web-search@0.2.0
  memory:
    spaces:
      - {ref: memorysets/agent-longterm@0.1.0, as: longterm}
  a2a: {enabled: true}
  runtime:
    resources: {cpu: "1.0", memory: 1Gi}
    env_allowlist: [ANTHROPIC_API_KEY]
```

### 2. 그래프 배선 (goal)

```yaml
# graphs/research-pipeline.yaml
apiVersion: aaestro/v1
kind: Graph
metadata: {name: research-pipeline, version: 1.0.0}
spec:
  mode: mission                 # mission(달성형) | service(상주형)
  goal: 질의를 받아 리서치 보고서를 완성한다
  nodes:
    - {id: planner, agent: agents/planner@0.1.0}
    - {id: researcher, agent: agents/researcher@0.1.0}
    - {id: writer, agent: agents/writer@0.1.0}
  edges:
    - {from: START, to: planner}
    - {from: planner, to: researcher}
    - {from: researcher, to: writer}
    - {from: writer, to: END}
  connections:                  # A2A peer 호출 allowlist (동등한 peer)
    - {caller: researcher, callee: planner}
```

### 3. 그룹 리소스 선언 (선택)

```yaml
# groups/research.yaml
apiVersion: aaestro/v1
kind: Group
metadata: {name: research}
spec:
  quotas: {cpu: "8.0", memory: 16Gi, max_agents: 10}
  secrets: [SEARCH_API_KEY]
  memory:
    spaces:
      - {ref: memorysets/domain-knowledge@0.1.0, as: knowledge, mode: rw}
```

### 4. 배포 전 검증

계약 검증은 기동 전에 수행됩니다 — **배포 검증 항목 중 하나라도 실패하면
컨테이너를 기동하지 않습니다**:

```bash
aaestro validate                                # 저장소의 모든 그래프
aaestro deploy graphs/research-pipeline.yaml    # 그래프 하나
aaestro status                                  # 여기 선언된 것
aaestro config dev                              # 환경별 해석된 설정
```

`validate` 와 `deploy` 는 검증 실패 시 0이 아닌 코드로 종료하므로 스크립트와
조합할 수 있습니다. `--json` 을 붙이면 기계 판독 가능한 출력이 나옵니다.

무결성 점검은 기록과 실체를 대조합니다 — 고아 checkpoint, 끊어진 모듈 ref,
유령 컨테이너:

```bash
aaestro check observed-state.yaml
```

### 5. 그래프 실행

`run` 은 mission 그래프를 제출하고 END 도달까지 기다립니다. 제출 전에 계약을
검증하며, 에이전트 주소는 명시적으로 넘깁니다 — CLI 가 컨테이너 포트를
추측하지 않습니다:

```bash
aaestro run graphs/research-pipeline.yaml \
  --input '{"query": "..."}' \
  --agent-token "${AAESTRO_AGENT_TOKEN:-e2e-token}" \
  --agent planner=http://127.0.0.1:18082 \
  --agent researcher=http://127.0.0.1:18083 \
  --agent writer=http://127.0.0.1:18084
```

에이전트는 `make e2e-up` 으로 먼저 띄웁니다 (fake 모델 provider — 실 LLM 미호출).

Service 그래프는 완주 개념이 없으므로 `--service` 로 구동합니다:

```bash
aaestro run graphs/feed-monitor.yaml --service \
  --agent-token "${AAESTRO_AGENT_TOKEN:-e2e-token}" \
  --agent watcher=http://127.0.0.1:18082
```

한 iteration 은 `START → … → END` 한 바퀴이고, 중단할 때까지 반복합니다.
`Ctrl-C` 는 즉시 종료가 아니라 **drain** 입니다 — 진행 중 iteration 을 마친 뒤
정지하므로 반쯤 진행된 회차가 남지 않습니다. 정해진 횟수만 돌리려면
`--iterations N` 을 씁니다.

Direct 요청은 그래프 run 없이 어느 에이전트의 Control API 에나 닿습니다:

```bash
curl -X POST http://127.0.0.1:18081/v1/invoke \
  -H "authorization: Bearer ${AAESTRO_AGENT_TOKEN:-e2e-token}" \
  -H 'content-type: application/json' \
  -d '{"task_id":"t1","run_id":"direct-1","node_id":null,
       "input":{"msg":"hello"},"trace":{"trace_id":"tr-1"}}'
```

Control API 는 에이전트별 토큰을 요구합니다. `/v1/health` 만 무인증입니다
(Docker healthcheck 가 직접 호출하므로). 운영에서는 runtime 이 에이전트마다
난수 토큰을 발급해 주입하며, 개발 스택은 같은 코드 경로를 유지하려고 고정
placeholder 를 씁니다.

agentd 는 `AAESTRO_AGENT_TOKEN` 이 없으면 (미설정 · 빈 값 · 공백뿐) 기동하지 않습니다.
변수 이름을 밝힌 `CFG_001` 로 종료하므로, env 를 빠뜨려도 Control API 가 조용히 열리지
않습니다. 토큰 없는 기동은 명시적 opt-in (`AAESTRO_ALLOW_UNAUTHENTICATED=1`) 이고, 바인드
주소가 loopback 일 때만 유효합니다 (`AAESTRO_AGENT_BIND_HOST=127.0.0.1` — 컨테이너 기본값
`0.0.0.0` 은 거절됩니다). 기동 시 WARN 로그를 남기며, 토큰 값은 어떤 메시지·로그에도 나오지
않습니다.

placeholder 는 스택마다 다릅니다: `make e2e-up` (`compose.e2e.yaml`) 은
`e2e-token`, 일반 개발 스택 (`compose.yaml`) 은 `dev-local-token` 이 기본값입니다.
띄운 스택에 맞는 값을 넘기거나, 기동 전에 `AAESTRO_AGENT_TOKEN` 을 export 해서
양쪽이 같은 값을 보게 하십시오. 토큰이 어긋나면 모든 노드 호출이 `401` 이 됩니다.

아직 로드맵에 있는 것 *(미구현)*: `aaestro run trace`, `agent invoke`,
`agent logs`, `replay`, `memory reindex`.

## 다음 단계

- [architecture.md](architecture.md) — 구성 요소의 전체 그림
- [modules.md](modules.md) — 스킬셋/프롬프트셋/메모리셋 제작
- [testing.md](testing.md) — LLM 주변의 결정적 테스트 작성
- [.claude/rules/](../../.claude/rules/README.md) — 전체 개발 룰셋
