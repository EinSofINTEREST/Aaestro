# 테스트 전략

한국어 | **[English](../en/testing.md)**

비결정적인 모델 주변에서 Aaestro 가 결정성을 유지하는 방법. 규범 규칙:
[06-testing.md](../../.claude/rules/06-testing.md).

## 원칙

1. **테스트는 실제 LLM 을 호출하지 않는다** — 스크립트된 `FakeModel` (또는 기록된
   cassette) 로 대체. CI 에는 의도적으로 provider API key 가 없으며, key 가 필요한
   테스트는 버그다.
2. **외부 서비스 의존 금지** — Docker fixture (testcontainers) 또는 fake 만 사용.
3. **결정적 + 병렬 실행 가능** — 시간 의존 로직(backoff, health 주기, service idle)은
   clock 주입으로 테스트, `sleep` 금지.
4. 임베딩은 결정적 해시 기반 fake 사용 — 임베딩 API 호출 금지.

## 테스트 피라미드와 배치

```
tests/
├── unit/           # 외부 의존 없음 — src/aaestro 구조 미러링          (~70%)
├── integration/    # Docker / 실제 MCP 서버 fixture                    (~25%)
├── e2e/            # 전체 compose 스택, fake LLM provider 컨테이너      (~5%)
└── fixtures/       # FakeModel, fake MCP 서버, builder, 테스트 yaml
```

- 마커: `@pytest.mark.integration`, `@pytest.mark.e2e`
- `make test` 는 unit 만 실행; `make test-integration` / `make test-e2e` 는 분리 실행
- E2E 는 CI 에서 nightly (`ci-nightly.yml`) — PR gate 아님
- 공용 E2E 스캐폴딩(compose 스택과 control plane 프로세스)은 `tests/e2e/conftest.py` 에 둔다 —
  테스트 모듈 사이에서 픽스처를 import 하면 이름이 조용히 가려진다

### 브라우저 테스트

`tests/e2e/test_ui_browser.py` 는 실제 브라우저로 운영자 화면을 조작한다. 폼 하나가 잘못
배선돼 있어도 그 뒤의 REST 흐름은 초록일 수 있기 때문이다. 브라우저가 필요하며, 먼저
가능한 쪽을 쓴다:

1. 호스트의 chromium (`uv run playwright install --with-deps chromium`), 또는
2. 공식 Playwright 이미지를 Docker host 네트워크에서 브라우저 서버로 기동

두 번째 경로는 root 도 호스트 패키지도 필요 없다 — 개발 머신에서는 dev 의존성 외에
아무것도 설치하지 않고 돌릴 수 있다. 어느 경로를 썼는지는 테스트가 출력한다. CI 는
브라우저를 네이티브로 깔고, 실패하면 이미지로 넘어간다.

## 필수 커버 영역

| 영역 | 초점 |
|---|---|
| core | manifest / topology / group 스키마 검증, 스코프 해석 |
| orchestrator | config → StateGraph 빌드, 라우팅, state 병합, mission/service 모드 규칙 |
| protocols | `AaestroError` 변환, A2A allowlist, tool 네임스페이싱 |
| modules | ref 파싱, 스키마 스냅샷(스킬셋), 렌더 골든(프롬프트셋) |
| memory | space ACL, 별칭 해석 (local > group > global), 하이브리드 검색 병합, recall 예산 |
| agentd | tool loop 상한, cancellation 정리, direct 태스크 처리 |
| runtime | 컨테이너 lifecycle, quota 거부 (`RT_006`), drain 시맨틱 |

Service 그래프는 추가로: iteration 누적, idle backoff 진행, drain 중 iteration 재개,
연속 실패 정지(`GRAPH_005`) 시나리오가 필수입니다.

## 품질 게이트 (CI 강제)

```bash
make lint          # ruff check + ruff format --check
make typecheck     # mypy — src/aaestro/core 는 strict
make test          # pytest unit + 커버리지 ≥ 70% (미달 시 실패)
make test-integration
```

커버리지: **강제 게이트는 하나** — `src/aaestro` 전체 기준 70% 이상, `make test` 의
pytest-cov `--cov-fail-under=70` 로 강제. 핵심 경로 90%+ 와 에러 변환 경로 100% 는
리뷰 목표치이며 별도 CI 게이트가 아닙니다.
체크 이름과 머지 게이트 연결: [ci/status-checks.md](ci/status-checks.md).

## 알려진 한계

이 스위트가 **증명하지 않는 것** — 초록불을 완전함으로 오해하지 않도록:

- **실제 모델은 한 번도 호출하지 않습니다.** 유닛은 `FakeModel` 을, E2E 는
  Messages API 를 흉내내는 결정적 fake provider 를 상대로 **표준 실행기**를
  돌립니다 — 프롬프트 렌더, tool loop, 출력 정형, provider 바인딩이 전부
  실행됩니다. 여전히 없는 것은 실 provider 자신의 동작입니다: 토큰 한도,
  스트리밍 특이사항, provider 측 rate limit.
- **실제 embedding API 도 쓰지 않습니다.** 메모리 테스트는 `HashEmbedder` 를
  쓰는데, 결정적이지만 의미 유사도를 모델링하지는 않습니다. 따라서 recall 의
  **순위 품질**은 측정되지 않고, 병합·문턱·예산의 동작만 검증됩니다.
- **MCP 는 프로세스 안에서만 검증됩니다.** `mcp` 바인딩을 참조 서버
  프로세스로 태웁니다 — 실제 SDK 를 지나지만 **컨테이너 경계는 넘지
  않습니다** (아래 Docker 항목 참조). A2A 는 이제 넘습니다 (아래 참조).
- **Docker 의존 테스트는 daemon 이 없으면 skip 됩니다.** 로컬 초록불이 컨테이너
  경로의 동작을 보장하지 않습니다 — CI job 을 확인해야 합니다.
- **A2A 는 이제 컨테이너를 가로지릅니다 — 전이 위임은 아직입니다.** 선언된
  호출, 미선언 방향, 위조 토큰, 깊이 상한(`A2A_005`) 이 모두
  `tests/e2e/test_a2a.py` 에서 살아있는 컨테이너를 가로질러 검증됩니다. 다만
  깊이 상한은 depth 를 **주입해** 확인한 것이지 에이전트가 실제로 다시
  위임한 것은 아닙니다 — 태스크 도중 peer 를 호출하는 경로는 여전히 전
  구간으로 증명되지 않았습니다.
- **auto-recall 은 이제 전 구간에서 실행됩니다 — 순위 품질은 아닙니다.** 저장한
  사실이 후속 태스크 프롬프트에 도달하는 것을 살아있는 스택에서 확인합니다:
  에이전트가 컨테이너로 뜬 Memory Service 에 HTTP 로 붙고, 임베딩은 대역이
  내주며, `tests/e2e/test_auto_recall.py` 가 **모델이 실제로 받은 프롬프트**를
  단언합니다. 측정되지 **않는** 것은 어떤 기억이 뽑히는가입니다 —
  `HashEmbedder` 와 대역은 결정적이지만 의미 유사도를 모델링하지 않으므로
  관련성 순서는 여전히 증명되지 않습니다 (위 항목).
- **service run 은 두 재시작 경계를 모두 넘습니다 — 자동 복구는 아직입니다.**
  오케스트레이터 재시작은 checkpoint·저장된 iteration 회차·외부 drain 요청을
  이어받고 (`tests/e2e/test_service_restart.py`), 에이전트 컨테이너를 죽이면
  run 이 `GRAPH_005` 로 정지해 그 사실이 다른 프로세스에서도 보이며,
  에이전트가 돌아오면 `resume_service` 로 이어집니다
  (`tests/e2e/test_agent_kill.py`). 덮이지 **않은** 것은 runtime 이 죽은
  에이전트를 **스스로 재시작**하는 경로입니다 (02 Lifecycle 의 backoff 정책)
  — 여기서는 테스트가 운영자를 대신해 되살립니다.
