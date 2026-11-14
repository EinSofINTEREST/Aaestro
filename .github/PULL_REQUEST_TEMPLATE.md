<!-- PR 타이틀: [카테고리#이슈번호] 제목  (예: [FEAT#3] Docker 런타임 구현) -->

## 연관 이슈

<!-- closing reference 필수 — Linked Issue Check 가 검증합니다 -->

Closes #

## 구현 내용

<!-- 필수 네 항목 (하네스 03-conventions.md "PR 본문") — 이 순서로, 맨 위에서 목적이 보이게.
     문제 → 해결을 한두 문장으로 요약하고, 세부는 각 항목의 <details> 로 접는다.
     보이는 부분만 읽어도 완결된 설명이어야 한다. -->

> 요약: (문제 → 해결 한두 문장)

### 발생했던 문제

<!-- 무엇이 잘못됐나 · 무엇이 필요했나, 누가 어떻게 겪나. 핵심 2~4문장, 해결책은 미리 말하지 않는다 -->

<details><summary>자세히</summary>

(재현 절차 · 로그 · 배경)

</details>

### 해결 방법

<!-- 핵심 접근 3~6개 불릿, 동작 기준 -->

-

<details><summary>자세히</summary>

(모듈/레이어별 변경 · 세부 근거)

</details>

### 작업 분배

<!-- 하네스 02-orchestration.md "기록". 위임도 `메인 직접` 행도 없으면 "없음".
     Codex 실행자는 역할 열에 `qa (Codex)` 처럼, 예비로 대체했으면 사유를 검수 결과 열에 -->

| 하위 작업 | 역할 | 티어 | 왕복 | 검수 결과 |
|---|---|---|---|---|
| 없음 | — | — | — | — |

### 핵심 결정

<!-- 두 안 중 하나를 고른 것마다 한 줄 (고른 안 · 버린 안 · 이유). 전체 ADR 은 아래로 접는다. 없으면 "없음".
     본 저장소는 별도 ADR 저장소가 없어 PR 본문이 1차 기록이다 (08-workflow.md "결정 기록 위치") -->

없음

## CI / 머지 게이트 점검

<!-- 체크 이름은 docs/en/ci/status-checks.md (단일 소스) 와 일치 —
     변경 시 문서/워크플로/Ruleset 을 같은 PR 에서 동시 갱신 -->

- [ ] Lint
- [ ] Type Check
- [ ] Test (커버리지 ≥ 70%)
- [ ] Integration Test
- [ ] Commit Lint (`[카테고리]: 한국어 요약`)
- [ ] PR Title Lint (`[카테고리#이슈번호] 제목`)
- [ ] Linked Issue Check (closing reference)
- [ ] Docs Sync Check (docs/en ↔ docs/ko 동기화)
- [ ] Review Presence (정보성 — 머지 필수 아님. 작성자 외 신뢰 계정 — 저장소 멤버 · 리뷰 봇 allowlist — 의 리뷰 최소 1개. 봇 본문만 있는 리뷰 · 👀 는 불인정)

## 변경 영향 범위 + 위험도

<!-- 영향 레이어: core / orchestrator / runtime / protocols / modules / memory / agentd / docs
     위험도: 낮음 / 중간 / 높음 + 근거 -->

- 영향 범위:
- 위험도:

## 롤백 계획

<!-- revert 가능 여부, 모듈 버전 롤백 경로 (semver), 데이터/설정 영향 -->

-
