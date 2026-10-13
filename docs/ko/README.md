# Aaestro 문서

한국어 | **[English](../en/README.md)**

LangGraph 기반 모듈형 멀티 에이전트 오케스트레이션 프레임워크 **Aaestro** 의 사용자/운영자
문서입니다.

> 개발 룰셋의 원본은 [`.claude/rules/`](../../.claude/rules/README.md) 에 있습니다.
> 본 문서는 요약과 안내를 담당하며, 룰셋과 불일치할 경우 룰셋이 우선하고 문서를 수정해야
> 합니다.

## 목차

| 문서 | 내용 |
|---|---|
| [architecture.md](architecture.md) | 시스템 계층, 상호작용 모델, 실행 모드, 리소스 스코프 |
| [getting-started.md](getting-started.md) | 사전 요구사항, 환경 구성, 첫 솔루션 조립 |
| [modules.md](modules.md) | 모듈 시스템 — 스킬셋/프롬프트셋/메모리셋/그래프/그룹 |
| [api.md](api.md) | Control Plane REST API — 카탈로그·저작·배포·run |
| [ui.md](ui.md) | Web UI — 브라우저에서 조립·배포·구동 |
| [testing.md](testing.md) | 테스트 전략, 결정성 규칙, 품질 게이트 |
| [ci/conventions.md](ci/conventions.md) | 저장소 거버넌스 및 CI 설계 규칙 |
| [ci/status-checks.md](ci/status-checks.md) | Required status check 이름의 단일 소스 |
| [runbooks/incident-response.md](runbooks/incident-response.md) | 알림, 1차 대응, 에스컬레이션 |
| [runbooks/recovery.md](runbooks/recovery.md) | run 복구, 메모리 재인덱싱, 백업 |
| [runbooks/access-control.md](runbooks/access-control.md) | 긴급 회수와 레지스트리 장애 |

## 요구사항

Python 3.12+ · [uv](https://docs.astral.sh/uv/) · Docker Engine 24+ (런타임/통합/E2E 경로에 필요)

```bash
uv sync --frozen        # 또는: make install
```

## 명령 레퍼런스

루트 [README](../../README.md#commands) 의 한국어 대응본입니다.

### 품질 게이트

```bash
make lint               # ruff check + ruff format --check
make typecheck          # mypy (aaestro.core 는 strict)
make test               # unit 테스트 + 커버리지 게이트 (>= 70%)
make check              # lint + typecheck + test — push 전에 이것을 돌린다
```

통합/E2E 는 Docker 가 필요하므로 기본 실행에서 빠져 있습니다:

```bash
make test-integration   # integration 마커 — Docker 컨테이너, 실제 MCP 세션
make test-e2e           # e2e 마커 — 전체 compose 스택 (CI 는 nightly)
```

Checkpointer 통합 테스트는 백엔드 주소가 없으면 skip 됩니다:

```bash
export AAESTRO_TEST_POSTGRES_URL=postgresql://aaestro:aaestro@127.0.0.1:15432/aaestro
export AAESTRO_TEST_REDIS_URL=redis://127.0.0.1:16379    # RediSearch 필요 (redis-stack)
```

### 이미지와 스택

```bash
make build              # aaestro/agent-base + agent-echo 이미지
make up                 # 개발 스택 — echo 에이전트 1대, control port 18080
make down
make e2e-up             # E2E 스택 — fake provider, memory service, 참조 에이전트 4대
make e2e-down
```

두 `up` 타깃은 `aaestro/agent-base` 와 그 위 서비스를 먼저 다시 굽습니다 — `src/` 를
고친 변경이 컨테이너까지 도달합니다. 프레임워크 코드는 base 이미지에만 들어 있어서,
그것을 다시 굽지 않고 스택을 올리면 **옛 코드를 검증**하게 됩니다.

E2E 스택이 노출하는 포트: 에이전트 control **18081-18084**, Memory Service **18090**,
checkpoint Postgres **15433**, 에이전트 metrics **19082-19084**, A2A **19102-19104**.
에이전트는 fake 모델 provider 에 붙으므로 실제 LLM 으로 나가지 않습니다.

커스텀 에이전트는 `make` 로 굽지 않습니다. `Dockerfile` 과 `src/` 는 control plane 의 재료
스토어에 있고, 그 버전의 이미지가 구워지지 않은 에이전트는 배포가 거절합니다. 저장소는
`claude-code` 에이전트의 재료를 시드로 싣고 있습니다:

```bash
uv run aaestro agent-push claude-code examples/materials/claude-code
uv run aaestro agent-build claude-code --wait   # 실패하면 로그 꼬리와 함께 0 이 아닌 코드로 끝난다
```

### CLI

```bash
uv run aaestro <command>          # venv 를 활성화했다면 그냥 `aaestro`
```

| 명령 | 하는 일 |
|---|---|
| `aaestro validate` | 저장소의 모든 그래프·매니페스트·모듈 ref 를 검증 |
| `aaestro deploy <graph.yaml>` | 그래프 하나를 배포 게이트로 검증 (`--a2a-port-range`) |
| `aaestro status` | 선언된 에이전트/그래프/그룹/모듈 요약 |
| `aaestro config [env]` | 해석된 설정 출력 (`dev` / `staging` / `prod`) |
| `aaestro check <state.yaml>` | 관측 상태와 대조해 정합성 불일치 보고 |
| `aaestro run <graph.yaml>` | 주소를 직접 적어 mission/service run 제출 |
| `aaestro run --deployment <id>` | 배포에 run 제출 — 주소는 control plane 이 해석 |
| `aaestro run-list` / `run-drain <id>` / `run-resume <id>` | control plane 을 통한 run 조작 |

`--json` (서브커맨드 앞에) 은 기계 판독 출력으로, `--root` 는 작업 디렉토리가 아닌 다른
저장소를 가리킬 때 씁니다.

그래프 실행에는 각 에이전트 Control API 의 주소가 필요합니다 — 오케스트레이터는 에이전트가
어디 있는지 추측하지 않습니다. E2E 스택 기준:

```bash
export AAESTRO_AGENT_TOKEN=e2e-token

# mission run — END 에 도달하면 종료하고 최종 state 를 출력
uv run aaestro run graphs/research-pipeline.yaml \
  --input '{"query": "aaestro architecture"}' \
  --agent planner=http://127.0.0.1:18082 \
  --agent researcher=http://127.0.0.1:18083 \
  --agent writer=http://127.0.0.1:18084

# service run — 상주 루프. 여기서는 종료되도록 횟수를 제한
uv run aaestro run graphs/feed-monitor.yaml --service --iterations 2 \
  --agent researcher=http://127.0.0.1:18083 \
  --agent planner=http://127.0.0.1:18082 \
  --agent writer=http://127.0.0.1:18084
```

`--iterations` 없는 service run 은 중단할 때까지 돕니다. `Ctrl-C` 는 drain 요청이므로
진행 중인 iteration 을 끝낸 뒤 정지합니다 — 중간에 끊기지 않습니다.

checkpoint 백엔드는 설정이 정하고, `--checkpointer` 는 한 번의 실행에 대한 override 입니다.
`postgres`/`redis` 는 접속 URL 이 추가로 필요한데, 자격증명이 파일에 남지 않도록 밖에서
주는 것이 기본 경로입니다:

```bash
AAESTRO_ENV=prod \
AAESTRO_ORCHESTRATOR__CHECKPOINTER_URL=postgresql://user:pass@host:5432/aaestro \
  uv run aaestro run graphs/research-pipeline.yaml --agent ...
```

영속 백엔드에서는 같은 `--run-id` 로 다시 실행하면 마지막 checkpoint 에서 이어집니다 —
다른 프로세스에서도 됩니다. 기본값 `memory` 는 프로세스와 함께 사라지므로 재개할 수 없습니다.

`run-list` / `run-drain` / `run-resume` 는 `--control-url` 로 control plane 에 붙고,
**기록된 run 만** 봅니다 — `orchestrator.run_store` 를 설정해 run 과 control plane 이 같은
저장소를 보게 하세요. `run-drain` 은 요청만 남기고 즉시 반환하며, 실제 정지는 run 을 구동하는
프로세스가 다음 iteration 경계에서 수행합니다.

그래프를 돌리는 다른 방법이 `--deployment` 이며, 여기에는 `--agent` 가 전혀 필요 없습니다 —
배포된 에이전트의 위치를 control plane 이 이미 알고 있기 때문입니다.

```bash
uv run aaestro run --deployment dep-2fc94a0b5f24 \
  --input '{"query": "aaestro architecture"}' \
  --control-url http://127.0.0.1:8700
```

완주하면 반환합니다 (`--no-wait` 를 주면 제출 시점에 반환). `run-resume` 은 service 그래프가
연속 실패로 *halted* 시킨 run 을 이어가는 명령입니다 — 의도적으로 drain 한 run 은 재개가
아니라 새로 제출합니다. 배포 표면이 없는 control plane 은 run 을 구동하지 않으므로,
일어나지 않은 재개를 보고하는 대신 `501` 로 답합니다. 전체 표면은
[Control Plane API](api.md) 를 보세요.

### 상주 프로세스

```bash
python -m aaestro.agentd        # 컨테이너 내부 에이전트 데몬 — Control API 8080
python -m aaestro.memory        # Memory Service — HTTP 표면 + 비동기 인덱싱 루프
python -m aaestro.orchestrator  # Control Plane — REST API + /ui 의 Web UI
```

`agentd` 는 runtime layer 가 모든 에이전트 컨테이너 안에서 띄우는 프로세스입니다.
`AAESTRO_MANIFEST`, `AAESTRO_AGENT_TOKEN`, `AAESTRO_ROOT` 를 읽고, 메모리가 배선된
경우 `AAESTRO_MEMORY_URL` 과 `AAESTRO_MEMORY_TOKEN` 또는 `AAESTRO_MEMORY_TOKEN_FILE` 을
읽습니다. `AAESTRO_AGENT_TOKEN` 은 필수입니다 — 없으면 agentd 가 `CFG_001` 로 종료합니다.
무인증 기동은 `AAESTRO_ALLOW_UNAUTHENTICATED=1` 과 loopback 인 `AAESTRO_AGENT_BIND_HOST`
(기본 `0.0.0.0`) 가 함께 있어야 합니다.

Memory Service 는 `AAESTRO_REPO_ROOT`, `AAESTRO_MEMORY_PORT`,
`AAESTRO_MEMORY_TOKENS_PATH` 를 읽습니다. 별도 프로세스로 떠 있어야 합니다 — append 는
즉시 커밋되지만 인덱싱은 비동기라, 이 루프가 없으면 저장된 것이 검색되지 않습니다.

Control Plane 은 설정에서 `orchestrator.run_store`, `control_host`, `control_port` 를 읽고,
저장소가 없으면 **기동을 거부**합니다 — 빈 목록을 돌려주면 "run 이 없다" 로 읽히기 때문입니다.
`orchestrator.control_token` 을 설정하고 bearer 토큰으로 보내세요 — 모든 `/v1/*` 라우트가
요구하며, 토큰 없이 loopback 아닌 주소에 바인드하면 거부됩니다 (`CFG_001`).
`GET /v1/health` 와 UI 정적 파일은 무인증으로 남습니다.

`orchestrator.deployment_store` 를 함께 설정하면 배포 표면이 열립니다 — 이 프로세스가 직접
에이전트 컨테이너를 띄우고, 거기에 제출된 run 을 구동하며, halted run 을 재개할 수 있습니다.
설정하지 않으면 그 라우트들은 닫힌 채로, 다른 프로세스가 구동하는 run 을 보고만 합니다.

`orchestrator.material_store` 를 설정하면 커스텀 에이전트의 빌드 재료를 저장할 수 있고,
`orchestrator.build_store` 까지 설정하면 이미지 빌드가 열립니다. 둘 다 있으면 요청에 따라
이미지를 굽고, 그 버전의 이미지가 구워지지 않은 커스텀 에이전트는 배포가 거절합니다. 재료
스토어가 없으면 재료 라우트가 `CFG_001` 로 답하고, 둘 중 하나라도 없으면 이미지 라우트가
없으며 배포는 각 매니페스트가 적은 이미지를 씁니다.

Web UI 는 `http://127.0.0.1:8700/` 입니다 — 카탈로그, 그래프/에이전트 편집기, 배포, run.
같은 프로세스가 서빙하며 문서화된 REST API 만 호출합니다
([API 레퍼런스](api.md), [UI 가이드](ui.md)).

셋 다 `AAESTRO_ENV`, `AAESTRO_CONFIG_DIR`, `AAESTRO_LOG_LEVEL`, `AAESTRO_LOG_FORMAT`,
`AAESTRO_METRICS_PORT` 를 따릅니다.

### 설정

설정은 `configs/{env}.yaml` (`dev`, `staging`, `prod`) 에 있습니다. CLI 는 위치 인자로
고르고, 상주 프로세스는 `AAESTRO_ENV` 를 읽습니다:

```bash
uv run aaestro config prod            # 해석된 prod 설정 출력
```

Override 는 **이중 언더스코어**로 섹션과 키를 구분합니다:

```bash
AAESTRO_ORCHESTRATOR__NODE_TIMEOUT_S=600 uv run aaestro config
```

언더스코어가 하나인 `AAESTRO_*` 는 설정이 아니라 프로세스 설정값입니다
(`AAESTRO_AGENT_TOKEN` 같은 것들). 로더는 이들을 **의도적으로 무시**합니다 — 컨테이너에
주입한 에이전트 env 가 그 컨테이너의 설정을 오염시키지 못하게 하기 위함입니다.

솔루션을 처음부터 조립하는 안내는 [getting-started.md](getting-started.md) 를 보세요.

## 언어 정책

- `docs/en/` 이 source of truth — 영어 먼저 작성
- `docs/ko/` 는 동일 구조의 한국어 번역 미러
- 두 버전은 항상 동기화 (`Docs Sync Check` 가 구조 미러를 강제)

## 추가 예정

- Agent Control API 레퍼런스 — `agentd` 가 컨테이너 안에서 서빙하는 표면. Control Plane
  쪽은 [api.md](api.md) 에 있고, 이것은 그 아래 계층입니다
