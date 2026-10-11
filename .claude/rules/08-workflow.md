# AI 작업 진행 규약 — 본 프로젝트 전용 사항

공통 규약은 사용자 레벨 하네스 **`~/.claude/rules/`** 에 있다 (2026-10-11 이관, 이슈 #375).
본 문서의 구 규약 1~6 은 IssueTracker 규약 (이슈 #152 · #199 · #210 · #212) 의 이식본이었고, 그 원형이 하네스로 옮겨졌다.
이 문서는 **공통 규약이 프로젝트에 위임한 값** 과 **본 저장소만의 명시적 예외** 만 적는다. 공통 규약과 충돌하면 이 문서가 우선한다 —
단, 그 우선권은 여기 적힌 전용 값과 예외 (예: `automerge`) 에만 미친다. 공통 규약 자체를 바꾸고 싶으면 이 문서에 덮어쓰지 말고 하네스 쪽을 고치고 결정으로 기록한다.
코드 자체의 규약은 [01-architecture.md](01-architecture.md) ~ [07-code-style.md](07-code-style.md) 및 [09-memory-context.md](09-memory-context.md).

> `~/.claude/rules/` 가 없는 환경 (CI 등) 에서는 이 문서와 [README.md](README.md) 의 요약만 적용된다.

<br>

## 구 규약 번호 → 공통 하네스 대응표

기존 이슈 · PR · [`ci-convention.yml`](../../.github/workflows/ci-convention.yml) 의 오류 메시지가 "08-workflow.md 규약 N" 으로 참조한다. 번호는 그대로 쓴다.

| 구 규약 | 내용 | 공통 하네스 (`~/.claude/rules/`) | 본 프로젝트 전용 (아래 절) |
|---|---|---|---|
| 1 | 이슈 먼저 · sub-issue 분할 | `01-workflow.md` 1절, `03-conventions.md` Sub-issue 연결 | `gh` 부재 fallback |
| 2 | 자율 진행 · 예외 4영역 | `01-workflow.md` 2절 | `automerge` 예외 · Docker · 실 LLM 비용 |
| 3 | Commit-per-TODO | `01-workflow.md` 3절 | — (각 커밋은 lint + typecheck + unit test 통과) |
| 4 | PR 자동 생성 · 생성 후 피드백 | `01-workflow.md` 4절, `06-pr-feedback.md` | 클라우드 루틴 · `loop.md` |
| 5 | 권한 최소화 | `01-workflow.md` 5절 | base image · 실 LLM 호출 |
| 6 | Label · Issue Type · 접두사 3분리 | `03-conventions.md` | Issue Type ID · 라벨 · 부여 명령 |
| — | 결정 기록 (ADR) | `07-decisions.md` | 저장 위치 (아래) |
| — | 오케스트레이션 · 역할 군 · 티어 | `02-orchestration.md`, 역할 정의 `~/.claude/agents/` | 브리프 포인터 · 작업 분배 표 |

<br>

## 이식 상태 — 보조 도구

IssueTracker 의 보조 도구 중 일부는 아직 이식되지 않았다. 이식 전까지는 명시된 fallback 을 사용한다.

| 도구 | 상태 | 비고 / 이식 전 fallback |
|---|---|---|
| `.github/PULL_REQUEST_TEMPLATE.md` | ✅ 이식 완료 | 규약 4 섹션 구성 반영 |
| PR 타이틀 lint / Commit lint CI | ✅ 구성 완료 | `.github/workflows/ci-convention.yml` (+ Linked Issue Check, Branch Name Lint) |
| 품질 CI (lint/typecheck/test) | ✅ 구성 완료 | `.github/workflows/ci-quality.yml` — 코드 부재 시 부트스트랩 가드로 skip |
| `refactor`/`hotfix`/`chore` 라벨 | ✅ 생성 완료 | `refactor` #D4C5F9, `hotfix` #B60205 |
| Issue Type ID (본 repo) | ✅ 조회 완료 | 아래 규약 6 표 |
| `scripts/gh-meta.sh` | 미이식 | `gh issue edit --add-label` / `gh api graphql` 직접 호출 (아래 명령) |
| `scripts/pr-resolve-comments.sh` | 미이식 | 개별 `gh api` 호출로 👀 reaction + thread resolve |

<br>

## 규약 1 · 4 — `gh` CLI 를 사용할 수 없는 환경

로컬 branch + commit 으로 진행하고, 이슈 · PR 생성이 불가했음을 작업 보고에 명시한다 (사용자가 후속 처리).

<br>

## 규약 2 — 자율 진행 · 예외 영역의 본 저장소 보충

예외 영역 (반드시 확인) 의 본 저장소 예:

| 영역 | 예 |
|---|---|
| 시스템 자체 변경 | Docker daemon 설정 변경 |
| 언급 없는 destructive 권한 | `docker system prune -a`, 운영 볼륨 · 이미지 삭제 |
| 외부 시스템 영향 | **실 LLM API 대량 호출** (수동 E2E · 실 모델 검증 — 비용 발생) |

> **Automerge 예외**: `automerge` 라벨이 부여된 PR 에 한해, `pr-merge-gatekeeper` 루틴 (`trig_013f65aaFS8kiSMUdG2FQk8b`) 이
> 3단계 검증 (리뷰 완결 / required check / 연결 이슈 목표 달성) 후 merge commit 으로 머지하는 것은 허용된다 —
> **라벨 부착이 곧 사람의 명시적 merge 승인** 이기 때문이다. 라벨 없는 PR 의 무인 merge 금지는 그대로 유지된다
> (상세: [`docs/en/ci/conventions.md`](../../docs/en/ci/conventions.md) 1.4).

자율 진행 영역 (확인 불필요) 의 본 저장소 추가 항목:

- 새 모듈 (스킬셋 · 프롬프트셋 · 그래프 · 메모리셋) 생성
- 의존성 추가 `uv add` — 단 신규 외부 모듈은 규약 5 적용
- lint / fmt / typecheck / test / build — `make` 또는 `uv run`
- **로컬 개발용 Docker 조작** — 테스트/개발 컨테이너의 build / run / stop / rm. 운영 리소스 및 프레임워크 외부의 컨테이너는 예외 영역.
  셸에 docker 그룹이 없으면 `sg docker -c "..."` 로 실행한다

<br>

## 규약 4 — PR 생성 후 피드백 (이벤트 드리븐 우선)

- 리뷰 피드백 반영의 기본 경로는 클라우드 루틴 **`pr-review-responder`** (`trig_01BC2kmcyESPDeihNd1QebHo`) — GitHub 리뷰 이벤트로 발화되며
  주간 안전망 스케줄 (일 21:00 UTC) 을 겸한다.
- 작업 세션이 살아 있으면 공통 `06-pr-feedback.md` 대로 `Monitor` 로 세션 안에서 처리한다. 둘이 겹치면 세션이 처리한 것에 👀 를 남겨 루틴이 건너뛰게 한다.
- [`.claude/loop.md`](../loop.md) 는 **수동 보조 수단** — 로컬 세션에서 즉각적인 반복 처리가 필요할 때만 `/loop` 로 사용한다. 자동 등록하지 않는다.
- 머지는 `automerge` 라벨 + `pr-merge-gatekeeper` 루틴이 담당한다. 세션 · 루프는 merge 를 수행하지 않는다.
- PR 은 항상 `main` 을 base 로 연다. `Linked Issue Check` 자체는 base 를 보지 않고 `closingIssuesReferences` 개수만 보지만, GitHub 은 base 가
  기본 브랜치인 PR 에만 그 참조를 만들므로 스택 PR 은 본문에 `Closes #N` 을 써도 0 이 되어 실패한다 (2026-08-25 PR #18 · #22 에서 확인).
  선행 작업은 `main` 에 머지된 뒤 진행한다.
- 완료 조건을 다 못 채운 PR 은 `Refs` 로 회피하지 않고 이슈를 PR 범위에 맞춘다 (Linked Issue Check 는 필수 게이트).

<br>

## 규약 5 — 권한 최소화의 본 저장소 보충

- 신규 외부 의존성 (Python package / system package / **Docker base image 변경**) 은 모호 영역 → 사용자 사전 확인.
- **실 LLM API 호출이 발생하는 실행** 은 비용 발생 — 외부 시스템 영향으로 간주하여 사전 확인.
- 기존 도구 우선: `gh` / `uv` / `git` / `make` / `docker`.

<br>

## 규약 6 — Label · Issue Type

매핑과 접두사 3분리는 공통 `03-conventions.md`. 본 repo (EinSofINTEREST/Malkuth) 의 Issue Type ID (2026-08-24 조회):

| Issue Type | ID |
|---|---|
| `Task` | `IT_kwDODsDQh84By0jZ` |
| `Bug` | `IT_kwDODsDQh84By0ja` |
| `Feature` | `IT_kwDODsDQh84By0jb` |

`scripts/gh-meta.sh` 이식 전 부여 명령:

```bash
# 이슈 생성 + Label
gh issue create --repo EinSofINTEREST/Malkuth --title "[FEATURE] Docker 런타임 구현" --body "..." --label enhancement

# Issue Type 부여 (updateIssueIssueType mutation)
ISSUE_ID=$(gh issue view <NUMBER> --json id --jq .id)
gh api graphql -f query='
mutation($issueId: ID!, $issueTypeId: ID!) {
  updateIssueIssueType(input: {issueId: $issueId, issueTypeId: $issueTypeId}) {
    issue { number issueType { name } }
  }
}' -f issueId="$ISSUE_ID" -f issueTypeId="<위 표의 ID>"

# PR Label (닫는 이슈와 동일)
gh pr edit <PR_NUMBER> --add-label enhancement
```

<br>

## 결정 기록 위치

본 저장소에는 Notion ADR 데이터베이스가 없다. 공통 `07-decisions.md` 의 우선순위에 따라 **PR 본문 "결정" 섹션** 에 ADR 형식으로 적는다.
아키텍처 수준의 결정은 [01-architecture.md](01-architecture.md) 등 규약 문서에 반영한다 (규약 문서가 에러 코드 · 로그 필드 · 메트릭을 선언하면
코드 · 운영 자산에도 같은 PR 로 선언한다 — 룰셋만 고치면 동기화 테스트가 깨진다). 전용 저장소 도입 여부는 별도 결정.

<br>

## 오케스트레이션 보충

- 역할 군 · 티어 · 모델 매핑 · 브리프 · 검수는 공통 `02-orchestration.md` 가 단일 출처. 역할 정의는 `~/.claude/agents/`.
- 서브 에이전트 브리프의 "제약" 에 넣는 포인터: 에이전트 격리 · 스코프 [01-architecture.md](01-architecture.md) · [02-agent-implementation.md](02-agent-implementation.md),
  에러 코드 · 로그 필드 [05-error-handling.md](05-error-handling.md), 테스트 위치 · fake 규칙 [06-testing.md](06-testing.md), 스타일 · 문서 en/ko 미러 [07-code-style.md](07-code-style.md).
- 검수 재실행 명령: `make lint` · `make typecheck` · `make test` (`make check` 로 일괄). 통합은 `make test-integration` (Docker).
- 실제 스택 검증은 `make build` → `make e2e-up`. 서브 에이전트에 실 LLM 호출이 필요한 검증을 맡기지 않는다 (규약 5).
- PR 템플릿에 "작업 분배" 섹션이 없으므로 "구현 내용" 아래에 표를 추가한다. 서브 에이전트를 쓰지 않았으면 "없음".

<br>

## 참고 자료

- 규약 도입 배경 (원 프로젝트): IssueTracker 이슈 #152 (자율 진행), #199 (issue-first), #210 (Label·Type·Sub-issue), #212 (prefix 3분리)
- 이슈 #375 — 공통 규약을 `~/.claude/rules/` 로 이관, 본 문서를 전용 사항으로 재구성
- 관련 규약: [07-code-style.md](07-code-style.md) — commit/PR 메시지 컨벤션 / [06-testing.md](06-testing.md) — 작업 단위 테스트 기준
- 관련 문서: `.github/PULL_REQUEST_TEMPLATE.md`, `docs/en/ci/` (CI 운영 규약 + Required Status Checks 단일 소스)
