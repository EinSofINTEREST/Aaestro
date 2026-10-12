# Required Status Checks — 단일 소스

한국어 | **[English](../../en/ci/status-checks.md)**

이 문서는 PR 머지 게이트에 사용되는 Required status check 이름의 **유일한 단일
소스(Single Source of Truth)** 입니다. GitHub Ruleset 과 PR 템플릿은 모두 이 문서의
이름과 **토씨 단위로 일치**해야 합니다.

## 명명 규칙

- **표의 `이름` 열에는 GitHub Ruleset 에 실제 등록된 체크 이름을 그대로 기재**한다
  (GitHub Actions job `name:` 값이 그대로 context 가 되므로 Title Case 포함 가능).
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
| `Branch Name Lint` | `ci-convention.yml` / `branch-lint` | `{카테고리}/#{이슈번호}/{요약}` 위반 경고 — 정보성 | No |
| `E2E Test` | `ci-nightly.yml` / `e2e` | Nightly 전체 스택 실행 (fake LLM provider) — 머지 게이트 아님 | No |

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
