# Required Status Checks — Single Source

**[한국어](../../ko/ci/status-checks.md)** | English

This document is the **single source of truth** for the required status check names used
as PR merge gates. The GitHub Ruleset and the PR template must match these names
**verbatim**.

## Naming Rules

- The `Name` column records the exact check name registered in the GitHub Ruleset
  (a GitHub Actions job `name:` becomes the check context — Title Case allowed).
- New checks may use Title Case for readability; duplicate names across workflows are
  forbidden.
- Renaming pauses the merge gate: update this document, the workflow, and the Ruleset
  **in the same PR**.

## Registered Checks

| Name | Workflow / Job | Description | Required |
|------|---------------|-------------|----------|
| `Lint` | `ci-quality.yml` / `lint` | `make lint` — ruff check + ruff format --check | Yes |
| `Type Check` | `ci-quality.yml` / `typecheck` | `make typecheck` — mypy (strict on `src/aaestro/core`) | Yes |
| `Test` | `ci-quality.yml` / `test` | `make test` — pytest unit + coverage ≥ 70% enforced | Yes |
| `Integration Test` | `ci-quality.yml` / `integration` | `make test-integration` — Docker-based integration tests | Yes |
| `Commit Lint` | `ci-convention.yml` / `commit-lint` | Enforces `[카테고리]:` commit message format on all PR commits | Yes |
| `PR Title Lint` | `ci-convention.yml` / `pr-title-lint` | Enforces `[카테고리#이슈번호] 제목` (or `[카테고리#이슈번호]: 제목`) strictly (PR only) | Yes |
| `Linked Issue Check` | `ci-convention.yml` / `linked-issue` | Requires ≥ 1 closing reference (`closingIssuesReferences.totalCount ≥ 1`, PR only; bot allowlist applies) | Yes |
| `Docs Sync Check` | `ci-docs.yml` / `docs-sync` | `docs/en` ↔ `docs/ko` structure mirror + language selector presence | Yes |
| `Branch Name Lint` | `ci-convention.yml` / `branch-lint` | Warns on `{category}/#{issue}/{summary}` violations — informational | No |
| `E2E Test` | `ci-nightly.yml` / `e2e` | Nightly full-stack run with fake LLM provider — not a merge gate | No |

## Evolution Notes (from the IssueTracker origin)

This gate set is ported from the IssueTracker repository and evolved for Aaestro:

- `Format Check` + `Lint` (gofmt / golangci-lint) → merged into a single `Lint`
  (ruff handles both linting and format checking)
- `Build` (go build) → `Type Check` (mypy is the Python analogue of compile-time safety)
- `Test` coverage gate raised: 40% → **70%** (per [06-testing.md](../../../.claude/rules/06-testing.md))
- Added: `Integration Test` (Docker runtime), `Docs Sync Check` (en/ko documentation
  policy), `Branch Name Lint` (informational), nightly `E2E Test`
- **Bootstrap guard**: `ci-quality.yml` jobs skip (reported as skipped → gate passes)
  while `pyproject.toml`/`Makefile` are absent, so docs-phase PRs are not blocked.

## Change Procedure

1. Update this document first.
2. Match the workflow job `name:` to the document.
3. Update the GitHub Ruleset "Require status checks to pass" list.
4. Update the PR template checklist.
5. All of the above **in the same PR**.

> Name mismatches are the most common cause of a permanently blocked merge. When
> renaming, update all locations in one PR.
