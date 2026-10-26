# 인시던트 대응

한국어 | **[English](../../en/runbooks/incident-response.md)**

Aaestro 알림이 울렸을 때의 대응 절차. 규범 규칙:
[05-error-handling.md](../../../.claude/rules/05-error-handling.md).

## 심각도

| 등급 | 의미 | 예시 |
|---|---|---|
| **P0** | 전체 시스템 장애 | 모든 run 실패, checkpoint 유실 |
| **P1** | 핵심 기능 정지 | 핵심 에이전트 다운, 실패율 >50% |
| **P2** | 성능 저하 | 단일 에이전트 지연, 특정 tool 실패 |
| **P3** | 경미 | 소폭 품질 저하 |

## 처음 5분

1. **Overview** 대시보드를 연다 — 특정 에이전트인가, 특정 그래프인가, 호스트인가?
2. `run_id` 로 로그를 필터링한다 — id 하나가 orchestrator → runtime → agentd →
   protocol 을 관통한다.
3. **에러 코드 분포**를 본다. 접두사가 대응을 결정한다:
   `LLM_*` (provider), `RT_*` (컨테이너), `MCP_*` / `A2A_*` (프로토콜),
   `GRAPH_*` (토폴로지/state), `STOR_*` (checkpoint).

## 알림별 대응

### AgentHighFailureRate

한 에이전트의 태스크 실패율이 10% 를 넘었다.

1. `aaestro agent logs <agent>` — 지배적인 `error_code` 를 읽는다.
2. `LLM_001` (rate limit) → [ModelRateLimited](#modelratelimited) 참조.
   `LLM_005` (max turns) → 프롬프트가 루프에 빠졌을 가능성이 높다, promptset 버전 확인.
   `MCP_003` → tool 이 실패 중이다, Protocol 대시보드 확인.
3. 최근 배포와 상관관계가 있으면 모듈 버전을 롤백한다 — 모듈 버저닝 덕에 즉시 가능하다.

### AgentDown

`aaestro_agent_health == 0` 이 3분간 지속.

1. `aaestro agent inspect <agent>` — manifest 와 실제 로드된 상태를 대조한다.
2. `initialize()` 실패는 컨테이너를 Ready 로 보내지 않는다. 흔한 원인은
   `MCP_001` (필수 MCP 서버 기동 실패) 과 `CFG_002` (secret 키가 어느 스코프에서도
   해석되지 않음) 이다.
3. 해당 에이전트만 재시작한다 — 진행 중 그래프는 checkpoint 에서 재개된다.

### ContainerRestartLoop

10분 내 재시작 5회 초과.

1. `aaestro_container_restarts_total` 의 `reason` 을 확인한다: `unhealthy` (health 실패로
   교체됨), `failed` (교체 컨테이너가 기동하지 못함 — 다음 backoff 뒤 다시 시도), `gave_up`
   (상한 도달). OOM 은 아직 따로 분류하지 않는다 — runtime 은 재시작할 때 `RT_003` 을 남기지
   않고 교체된 컨테이너는 지워진다 — 그래서 메모리 부족도 `unhealthy` 로 보인다.
   `docker events --filter event=oom` 에 `oom` 이 있으면 `runtime.resources.memory` 를 올리거나
   동시성을 낮춘다.
2. `RT_001` 과 함께 `failed` 가 반복되면 대개 이미지나 entrypoint 문제다 — 컨테이너가
   health 를 보고할 지점까지 도달하지 못한다. 재시도 동안 그 레플리카는 **Unhealthy** 로
   남고 태스크를 받지 않는다.
3. 기동에 실패한 재시작도 센다. 10분 내 5회를 넘기면 runtime 이 에이전트를 **Failed** 로
   전환하고 재시도를 멈춘다. 원인을 고친 뒤 재배포한다.
4. **기동 중에** 재시작되는 에이전트(한 번도 healthy 인 적 없이 `interval_s × unhealthy_threshold`
   에서 재시작)는 에이전트 문제가 아니라 기동 유예가 짧은 것이다.
   `runtime.health_check.startup_grace_s` 와 `orchestrator.deployment_ready_timeout_s` 를 함께
   올린다 — 유예만 올리면 배포가 먼저 `RT_002` 로 끝난다.

### ModelRateLimited

Provider 가 요청을 거절하고 있다.

1. 에이전트별 세마포어를 줄여 동시 호출 수를 낮춘다.
2. 그래프가 허용한다면 fallback 모델로 전환한다.
3. `RATE_LIMIT_RETRY` 가 이미 최대 300s 까지 백오프한다 — 알림이 지속된다면
   재시도가 없어서가 아니라 quota 자체가 부족한 것이다.

### DecisionModelMostlyUncertain

decisionset 질문이 절반 넘게 `uncertain` 으로 답해, 결정 모델이 대부분의 일을 LLM 경로로
되돌리고 있다.

1. `aaestro_decision_bands_total` 을 질문별로 본다 — 한 질문인가, 전부인가? 한 질문이면 문구나
   구간의 문제이고, 전부면 provider 나 locale 의 문제다.
2. 그 decisionset 버전의 calibration 을 다시 돌려(06 Calibration — 모듈의 `calibration/` 라벨
   세트) 보고서를 선언된 `act_at` / `reject_at` 과 비교한다.
3. 구간을 고친 새 버전(patch) 또는 문구를 고친 새 버전(minor)을 게시한다. 그동안 시스템은
   그대로 돈다 — `uncertain` 은 언제나 원래 경로로 간다.

### DecisionModelUnavailable

provider 의 timeout·오류·circuit open 으로 결정이 원래 경로로 되돌아가고 있다.

1. 사용자에게 깨진 것은 없다: 모든 쓰임이 `unavailable` 을 `uncertain` 으로 취급한다. 비용은
   LLM 작업이 늘고 필터가 줄어드는 것이지, 틀린 답이 아니다.
2. 이그레스 프록시 로그에서 provider base URL 의 결과(`DEC_001` / `DEC_002` / `TO_004`)와
   provider 상태 페이지를 본다.
3. provider 장애가 길면 `decision.timeout_s` 를 낮추거나 가장 바쁜 에이전트의
   `spec.decision` 을 끈다 — 판정마다 timeout 을 기다리며 태스크가 느려지지 않게.

### ServiceRunStalled

Service run 이 활성인데 30분간 진행이 없다.

1. `aaestro_service_idle_delay_seconds` 를 확인한다 — idle 상한에 머물러 있다면
   입력이 실제로 없는 경우이며 **설계대로 동작 중**이다.
2. 입력이 있는데도 멈춰 있다면 watcher 노드가 조용히 실패하고 있을 가능성이 높다.
   iteration 로그(`iteration` 필드)를 읽고 `is_idle` 판정이 항상 참이 아닌지 확인한다.

### AccessRegistryUnreachable

강제 지점(Memory Service, 이그레스 프록시, A2A 서버)이 control plane 의 권한 레지스트리에 닿지
못한다.

1. 이 알림이 켜져 있는 동안 캐시에 없는 판정은 **거부**되고, 이미 허용된 권한은 계속 동작한다 —
   판정에 `valid_until` 이 있으면(근거가 부여) 그 시각까지, 없으면(근거가 선언) 기한 없이.
   지금 한 회수는 레지스트리에 다시 닿기 전까지 **반영되지 않는다**. 레지스트리가 답하면서 강제 지점을
   **거절**하는 경우(강제 지점 토큰이 틀림)는 장애가 아니다: 강제 지점은 캐시를 버리고 모두 거부한다 —
   토큰을 고친다.
2. control plane 프로세스와, 알림의 `component` 와 그 사이의 네트워크를 확인한다.
3. 회수가 급하면 해당 에이전트의 컨테이너를 멈춘다 — 레지스트리와 무관하게 동작한다.
   [access-control.md](access-control.md#레지스트리에-닿지-않을-때) 참조.

### AccessGrantRefusalsSpike

10분 동안 레지스트리가 부여를 많이 거절했다: 확장 상한을 넘는 요청, 권한 에이전트의 자기 부여,
작업 에이전트의 부여 API 직접 호출.

1. 로그에서 거절을 요청자별로 묶는다 (`decided_by`, `resource`, `target`).
2. 한 작업 에이전트가 대부분이라면 신뢰할 수 없는 입력에 조종당하고 있을 가능성이 높다.
   그 에이전트의 남은 부여를 회수하고 요청을 일으킨 run 을 살핀다.
3. 거절은 상한이 제 역할을 한 것이다. 상한은 그룹 선언 변경으로만 올리고, 이 알림을 끄려고
   올리지 않는다.

## 에스컬레이션

P0/P1 은 호출한다. P2/P3 는 `run_id` 와 에러 코드 분포를 첨부해 이슈로 남긴다 —
이 둘이 있어야 재현이 가능하다.

## 함께 보기

- [recovery.md](recovery.md) — run 복구와 메모리 재인덱싱
- [access-control.md](access-control.md) — 긴급 회수와 레지스트리 장애
