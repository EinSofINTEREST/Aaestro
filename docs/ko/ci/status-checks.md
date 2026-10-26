# Required Status Checks — 단일 소스

한국어 | **[English](../../en/ci/status-checks.md)**

이 문서는 PR 머지 게이트에 사용되는 Required status check 이름의 **유일한 단일
소스(Single Source of Truth)** 입니다. GitHub Ruleset 과 PR 템플릿은 모두 이 문서의
이름과 **토씨 단위로 일치**해야 합니다.

## 명명 규칙

- **표의 `이름` 열에는 GitHub Ruleset 에 실제 등록된 체크 이름을 그대로 기재**한다
  (GitHub Actions job `name:` 값이 그대로 context 가 되므로 Title Case 포함 가능.
  `Review Presence` 는 job 이 아니라 commit status context 다 — 아래 참고).
- 신규 추가 시 가독성을 위해 Title Case 허용, 워크플로 간 중복 금지.
- 리네임 시 머지 게이트가 일시 중단되므로, 문서/워크플로/Ruleset 3곳을
  **같은 PR 에서 동시 갱신**한다.

## 현재 등록된 체크

| 이름 | 워크플로 / Job | 설명 | Required |
|------|---------------|------|----------|
| `Lint` | `ci-quality.yml` / `lint` | `make lint` — ruff check + ruff format --check | Yes |
| `Type Check` | `ci-quality.yml` / `typecheck` | `make typecheck` — mypy (`src/aaestro/core` strict) | Yes |
| `Test` | `ci-quality.yml` / `test` | `make test` — pytest unit + 커버리지 70% 강제 | Yes |
| `Integration Test` | `ci-quality.yml` / `integration` | `make test-integration` — Docker 기반 통합 테스트 | Yes |
| `Commit Lint` | `ci-convention.yml` / `commit-lint` | PR 의 모든 커밋 메시지 `[카테고리]:` 포맷 강제 | Yes |
| `PR Title Lint` | `ci-convention.yml` / `pr-title-lint` | PR 타이틀 `[카테고리#이슈번호] 제목` (또는 `[카테고리#이슈번호]: 제목`) 엄격 강제 (PR only) | Yes |
| `Linked Issue Check` | `ci-convention.yml` / `linked-issue` | closing reference 최소 1개 검증 (`closingIssuesReferences.totalCount ≥ 1`, PR only; bot allowlist 적용) | Yes |
| `Docs Sync Check` | `ci-docs.yml` / `docs-sync` | `docs/en` ↔ `docs/ko` 구조 미러 + 언어 선택자 존재 검증 | Yes |
| `Review Presence` | `ci-review.yml` / `judge` (commit status) | 리뷰로 인정할 것이 최소 1개 — [Review Presence](#review-presence) 참고 | Yes |
| `Branch Name Lint` | `ci-convention.yml` / `branch-lint` | `{카테고리}/#{이슈번호}/{요약}` 위반 경고 — 정보성 | No |
| `E2E Test` | `ci-nightly.yml` / `e2e` | Nightly 전체 스택 실행 (fake LLM provider) — 머지 게이트 아님 | No |

## Review Presence

`Review Presence` 는 job 체크가 아니라 **commit status** 다. `ci-review.yml` (job
`Review Presence Judge` — 일부러 다른 이름) 이 PR 을 판정해 PR head 커밋에 status 를
쓴다. schedule 실행은 PR head 에서 돌지 않아 job 결과가 그 PR 의 체크가 될 수 없으므로,
모든 발화 경로가 같은 status context 를 쓴다.

- **발화**: PR opened / synchronize / reopened / ready_for_review (`pull_request_target`),
  리뷰 submitted / edited / dismissed, 리뷰 코멘트 created / edited / deleted, 매시 7 · 22 ·
  37 · 52분의 schedule 로 열린 PR 전부 (리액션은 워크플로를 발화시키지 못해 리뷰 봇의 👍 는
  이 경로로만 반영된다), 수동 `workflow_dispatch`. PR 이벤트는 PR 마다 concurrency 그룹을
  하나씩 쓰고, schedule 과 수동 실행은 따로 한 그룹을 쓴다.
- **신뢰 계정** — 이 계정만 인정될 수 있고, PR 작성자는 제외한다:
  - `author_association` 이 `OWNER` · `MEMBER` · `COLLABORATOR` 인 사람 (공개 저장소라
    외부인 리뷰로는 게이트가 열리지 않는다)
  - 리뷰 봇 allowlist 의 봇 (`ci-review.yml` 의 `REVIEW_BOTS`, 기본값
    `chatgpt-codex-connector[bot]`, `copilot-pull-request-reviewer[bot]`,
    `coderabbitai[bot]`). `github-actions[bot]` 은 일부러 넣지 않는다 — 워크플로가 리뷰를
    지어내지 못하게.
- **리뷰로 인정** — 신뢰 계정의:
  - `APPROVED` 또는 `CHANGES_REQUESTED` 리뷰
  - 라인 코멘트가 1개 이상인 리뷰 (상태 무관)
  - 신뢰하는 사람의 본문 있는 `COMMENTED` 리뷰
  - allowlist 의 리뷰 봇이 PR 본문에 단 👍 (`+1`) 리액션
- **불인정**: 신뢰하지 않는 계정 (외부인, 목록 밖 봇) 의 모든 것, allowlist 안이어도 봇의
  본문만 있는 리뷰 (예: Copilot 할당량 부족 알림), 작성자 본인의 리뷰, 👀,
  `DISMISSED` / `PENDING` 리뷰.

  작성자가 아닌 모든 계정을 인정하던 이슈 #514 의 정의보다 좁다.
- **상태**: 인정할 것이 있으면 `success`, 없으면 `pending` ("waiting for at least one
  review") — 리뷰는 시간이 지나 도착하므로 실패가 아니고, `pending` 도 머지를 막는다.
- **쓰기**: 쓰기 전에 head sha 의 현재 `Review Presence` status 를 읽고, state 나
  description 이 다를 때만 쓴다. GitHub 은 sha · context 당 status 를 1000건으로 제한한다
  ("Attempts to create more than 1000 statuses will result in a validation error") — 실행마다
  같은 값을 다시 쓰면 약 열흘 뒤 게이트가 멈춘다. `--all-open` 은 PR 하나를 판정한 직후 곧바로
  현재 값을 읽고 쓴 뒤에 다음 PR 로 간다. PR 하나의 실패 (API 오류, 모양이 어긋난 응답) 는
  로그를 남기고 건너뛰며, 실행은 종료 코드 1 로 끝난다.
- **게이트 무결성**: 판정 스크립트는 항상 기본 브랜치에서 checkout 하고, PR 이벤트는 기본
  브랜치의 워크플로 파일로 도는 `pull_request_target` 을 쓴다 — `ci-review.yml` 을 고친 PR 이
  이 경로로 자기 `success` 를 쓰지 못한다. 워크플로는 PR 코드를 checkout 하지 않고 API 로
  PR 을 읽기만 한다.
- **알려진 한계**:
  - `pull_request_review` · `pull_request_review_comment` 는 PR 쪽 워크플로 파일로 돌고, 쓰기
    권한이 있는 토큰 (사람, 또는 이 저장소의 어떤 워크플로) 은 commit status 를 직접 쓸 수
    있다. **한 번** 쓴 위조는 판정과 다르므로 다음 판정이 덮어쓰지만, schedule 은
    best-effort 다 — 부하가 높으면 GitHub 이 실행을 늦추거나 빠뜨릴 수 있다.
  - 쓰기 권한 토큰으로 **반복해** 쓰는 위조는 판정이 이길 수 없다. 그 루프가 그 sha 의
    1000건을 다 쓰면 판정 쓰기가 실패하고 위조가 남는다.
  - 쓸지 말지는 (state, description) 로만 판단한다 — 현재 status 를 누가 썼는지 (출처) 는
    보지 않는다.
  - schedule 과 PR 이벤트는 서로 겹칠 수 있다. schedule 의 판정과 쓰기 사이 수 초 동안 철회가
    들어오면, 오래된 판정이 새 값을 덮어 그 PR 의 다음 판정까지 남을 수 있다.
  - 공개 저장소에서는 저장소 활동이 60일 없으면 schedule 워크플로가 자동으로 꺼진다.
  - Ruleset 에 등록할 때 기대 출처를 GitHub Actions 앱으로 고정하면 사람 토큰의 직접 위조는
    막힌다 — GitHub 문서: "If the status is set by any other person or integration, merging
    won't be allowed". 같은 저장소의 다른 워크플로가 하는 위조는 같은 앱이라 남는다.
    확인하지 못함: `GITHUB_TOKEN` 으로 쓴 commit status (check run 이 아닌) 가 GitHub Actions
    앱 출처로 매칭되는지.
  - 그래서 이 체크는 워크플로를 바꾸는 PR 에 대한 **강제가 아니다**. 그런 PR 은 사람이
    리뷰해야 하고, 강제 장치 (`.github/` 의 CODEOWNERS, Ruleset 규칙) 는 저장소 설정이라 이
    체크 밖의 사용자 결정 대상이다.
- **리뷰어가 아무도 응답하지 않을 때**: 지금 신뢰할 수 있는 사람은 PR 작성자 한 명뿐이고
  작성자는 인정되지 않는다. 리뷰 봇이 모두 응답하지 않으면 (할당량, 장애) 게이트가 열리지
  않는다. 쓸 수 있는 경로:
  - 봇 리뷰 재요청 (예: `@codex review` 코멘트)
  - 이 체크에 대한 Ruleset bypass 또는 두 번째 collaborator — 둘 다 저장소 설정이고 사용자
    결정 대상이다

## 진화 내역 (IssueTracker 원형 대비)

본 게이트 구성은 IssueTracker 저장소에서 이식하며 Aaestro 에 맞게 발전시킨 것입니다:

- `Format Check` + `Lint` (gofmt / golangci-lint) → 단일 `Lint` 로 통합
  (ruff 가 lint 와 format 검사를 모두 수행)
- `Build` (go build) → `Type Check` (Python 에서 컴파일 안전성의 대응물은 mypy)
- `Test` 커버리지 게이트 상향: 40% → **70%**
  ([06-testing.md](../../../.claude/rules/06-testing.md) 기준)
- 신규: `Integration Test` (Docker 런타임), `Docs Sync Check` (en/ko 문서 정책),
  `Branch Name Lint` (정보성), nightly `E2E Test`
- **부트스트랩 가드**: `pyproject.toml`/`Makefile` 부재 시 `ci-quality.yml` job 이
  skip 되어 (skipped → 게이트 통과) 문서 단계 PR 이 차단되지 않음.

## 변경 절차

1. 이 문서를 먼저 업데이트한다.
2. 워크플로의 job name 을 문서에 맞춘다.
3. GitHub Ruleset 의 "Require status checks to pass" 목록을 문서에 맞춘다.
4. PR 템플릿 체크리스트를 갱신한다.
5. 위 모두를 **같은 PR** 에서 수행한다.

> 이름 불일치는 "머지 영구 차단"의 가장 흔한 원인입니다. 리네임 시 모든 위치를
> 같은 PR 에서 갱신하세요.
