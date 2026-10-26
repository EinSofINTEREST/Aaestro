# CI 운영 규약

한국어 | **[English](../../en/ci/conventions.md)**

저장소 거버넌스와 GitHub Actions CI 설계 규칙입니다. IssueTracker 저장소에서 이식하여
Aaestro 의 Python/uv 툴체인과 문서 정책에 맞게 발전시켰습니다.

관련 문서: [Required Status Checks 단일 소스](status-checks.md)

---

## 1. PR 머지 게이트 컨벤션

### 1.1 필수 요구사항
- **Required status checks**: 이름은 [status-checks.md](status-checks.md) 의 테이블과
  완전 일치.
- **Required approving reviews**: Ruleset 은 승인 **0** 건을 요구한다
  (`required_approving_review_count: 0`). CODEOWNERS 등록 후 `Require review from Code Owners`
  활성화.
- **리뷰 존재**: 리뷰 (봇 리뷰 포함) 가 최소 1개임은 대신 `Review Presence` 체크가 보장한다
  ([status-checks.md](status-checks.md#review-presence)) — Ruleset 의 필수 체크로 등록된 뒤 기준.
- **Conversation resolution**: 모든 리뷰 코멘트 해결 후에만 머지 가능
  (`required_review_thread_resolution: true`).
- **Merge commit 만 허용** (squash/rebase 비활성): per-TODO 커밋이 `main` 에 그대로 보존되고
  PR 마다 merge 노드 1개가 추가됨 — PR 단위 이력은 `git log --first-parent` 로 조회.
  merge 커밋은 PR 제목/본문을 사용하므로 `git log --first-parent main` 이 곧 PR 목록이 된다.

### 1.2 Ruleset 우선 원칙
Branch Protection 대신 **Repository Ruleset** 을 기본 수단으로 운영합니다:
- 우회 방지: admin bypass 범위를 명시적으로 제한
- 세분화 타겟팅: 브랜치 패턴, 태그, 파일 경로별 규칙 분리
- 감사 추적: 규칙 변경 이력이 별도 이벤트로 기록

### 1.3 Bot/App 예외 처리
- **화이트리스트 방식**: 허용된 자동화만 특정 게이트 우회 가능
  (현재는 `Linked Issue Check` 만).
- 인적 계정의 우회 금지 원칙 유지. 목록은 [부록 A](#부록-a-허용된-botapp) 에서 관리하며
  `.github/workflows/ci-convention.yml` 의 `bot allowlist` step 과 동기화합니다.

### 1.4 라벨 게이트 Automerge

PR 에 `automerge` 라벨을 붙이는 것은 **사람의 명시적 머지 승인**입니다. 라벨 부착 시
`pr-merge-gatekeeper` 클라우드 루틴이 발화되어, 다음 세 가지를 모두 검증한 뒤에만
merge commit 방식으로 머지합니다:

1. **리뷰 완결** — 미해결 리뷰 thread 0건, 리뷰어별 미해소 `changes_requested` 없음
2. **게이트** — required status check 전부 SUCCESS / NEUTRAL / SKIPPED
   (GitHub 은 셋 모두 통과로 취급, [status-checks.md](status-checks.md) 기준)
3. **목표 달성** — 연결 이슈의 작업 범위/완료 조건이 PR 의 실제 diff 로 충족됨

하나라도 실패하면 merge 하지 않고 부족한 항목을 코멘트로 남긴 뒤 **라벨을 제거**합니다
(보완 후 다시 라벨 부착). 라벨 없는 PR 은 절대 자동 머지되지 않으며, 루틴도 Ruleset
게이트를 우회할 수 없습니다.

**라벨 권한**: GitHub 에서 Triage 이상 role 은 라벨을 붙일 수 있으므로, Triage 부여가
곧 머지 승인 권한 부여가 됩니다. 이를 보완하기 위해 게이트키퍼는 merge 전에 `labeled`
timeline 이벤트의 actor 를 메인테이너 allowlist (현재 `juhy0987`) 와 대조합니다 —
협업자 추가 시 allowlist 확장은 같은 PR 에서 의도적으로 수행합니다.

---

## 2. CODEOWNERS 전략

- **SPOF 금지**: 핵심 경로는 개인 + 팀 중복 지정.
- `.github/`, CI 워크플로, 배포 관련 경로는 반드시 CODEOWNERS 커버.
- 전역 `*` 패턴 사용 금지 (머지 병목 방지).
- **상태**: 미등록 — 메인테이너 GitHub 핸들 확정 후 `.github/CODEOWNERS` 를 추가하고
  같은 PR 에서 이 절을 갱신합니다.

---

## 3. GitHub Actions CI 설계 규칙

### 3.1 워크플로 구조

워크플로는 **기능별로 분리된 파일**로 운영합니다 (가독성, Actions UI 그룹핑 명확화):

| 파일 | 담당 | Jobs |
|---|---|---|
| `ci-quality.yml` | 코드 품질 (PR gate) | `Lint`, `Type Check`, `Test`, `Integration Test` |
| `ci-convention.yml` | PR/커밋 메타데이터 형식 | `Commit Lint`, `PR Title Lint`, `Linked Issue Check`, `Branch Name Lint` |
| `ci-docs.yml` | 문서 정책 | `Docs Sync Check` |
| `ci-review.yml` | 리뷰 존재 (PR 게이트, commit status) | `Review Presence` |
| `ci-nightly.yml` | 스케줄 전체 스택 검증 | `E2E Test` |

- 각 job 은 독립 병렬 실행 (빠른 피드백). 유일한 의존은 `ci-quality.yml` 의 경량
  `Detect Sources` 부트스트랩 가드뿐.
- uv 캐시 활성화 (`astral-sh/setup-uv` 의 `enable-cache: true`).
- 신규 job 은 기능 그룹에 맞는 파일에 배치, 새 성격이면 새 파일 생성.

### 3.2 부트스트랩 가드

Aaestro 는 문서/룰셋 우선으로 시작하는 저장소입니다. 품질 job 은
`pyproject.toml` + `Makefile` 존재를 확인하고 부재 시 **skip** 합니다 — skipped 된
required check 는 게이트를 통과하므로, Ruleset 을 첫날부터 등록해 두어도 문서 단계 PR 이
차단되지 않습니다. 코드가 들어오는 순간 가드는 자연히 무의미해집니다.

### 3.3 Job 추가/변경 시 규칙

1. [status-checks.md](status-checks.md) 에 이름 먼저 등록.
2. 워크플로에 job 추가 (이름은 문서와 일치).
3. Ruleset `required_status_checks` 에 등록.
4. PR 템플릿 체크리스트에 추가.
5. **같은 PR** 에서 동시 갱신.

### 3.4 Failure 처리

- 기본: job 실패 시 PR 머지 차단 (Required check).
- Required job 에 `continue-on-error: true` 사용 금지 (게이트 우회가 됨).
- 정보성 job (예: `Branch Name Lint`) 은 Required 에 등록하지 않으며
  `continue-on-error: true` 허용. `if: always()` 대신 `if: ${{ !cancelled() }}` 사용
  (수동 취소 시 러너 낭비 방지 — `!` 로 시작하는 표현식은 `${{ }}` 로 감싸지 않으면
  YAML 파싱이 깨진다).

---

## 4. 체크리스트 (PR 리뷰 시 확인)

- [ ] PR 템플릿의 CI 점검 섹션이 누락 없이 작성됨
- [ ] Required status check 이름이 [status-checks.md](status-checks.md) 와 일치
- [ ] `continue-on-error: true` 가 Required job 에 사용되지 않음
- [ ] CODEOWNERS 변경 시 대체 승인자 포함 여부 확인
- [ ] 워크플로 변경 시 문서/Ruleset 동시 갱신 여부 확인

---

## 부록 A. 허용된 Bot/App

| 계정/App | 용도 | 허용 범위 |
|---------|------|-----------|
| `dependabot[bot]` | 의존성 업데이트 PR (`pyproject.toml` / `uv.lock`) | `Linked Issue Check` skip |
| _(추가 시 PR 로 이 표 갱신)_ | - | - |
