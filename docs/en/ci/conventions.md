# CI Governance and Conventions

**[한국어](../../ko/ci/conventions.md)** | English

Repository governance and GitHub Actions design rules. Ported from the IssueTracker
repository and evolved for Aaestro's Python/uv toolchain and documentation policy.

Related: [Required Status Checks — Single Source](status-checks.md)

---

## 1. PR Merge Gate Conventions

### 1.1 Requirements
- **Required status checks**: names must match [status-checks.md](status-checks.md)
  exactly.
- **Required approving reviews**: the Ruleset requires **0** approvals
  (`required_approving_review_count: 0`). Enable `Require review from Code Owners` once
  CODEOWNERS is registered.
- **Review presence**: the presence of at least one review — bot reviews included — is
  guaranteed by the `Review Presence` check instead
  ([status-checks.md](status-checks.md#review-presence)), once it is registered as a
  required check in the Ruleset.
- **Conversation resolution**: all review threads resolved before merge
  (`required_review_thread_resolution: true`).
- **Merge commits only** (squash/rebase disabled): every per-TODO commit survives on `main`
  and each PR adds one merge node — inspect PR-level history with `git log --first-parent`.
  The merge commit takes the PR title/body, so `git log --first-parent main` reads as the PR list.

### 1.2 Ruleset-First Principle
Use **Repository Rulesets** rather than legacy Branch Protection:
- Explicitly bounded admin bypass
- Fine-grained targeting (branch patterns, tags, paths)
- Rule-change events are audit-logged

### 1.3 Bot/App Exceptions
- Allowlist-based: only listed automation may bypass specific gates
  (currently the `Linked Issue Check`).
- Human accounts get no bypass. The list lives in
  [Appendix A](#appendix-a-allowed-botsapps) and must stay in sync with the
  `bot allowlist` step in `.github/workflows/ci-convention.yml`.

### 1.4 Label-Gated Automerge

Adding the `automerge` label to a PR is an **explicit human merge approval**. It fires
the `pr-merge-gatekeeper` cloud routine, which merges (merge commit) after verifying
all of:

1. **Review completion** — zero unresolved review threads, no outstanding
   `changes_requested` review per reviewer
2. **Gates** — every required status check SUCCESS, NEUTRAL, or SKIPPED
   (GitHub treats all three as passing; per [status-checks.md](status-checks.md))
3. **Goal attainment** — the linked issue's scope/acceptance criteria are satisfied by
   the PR's actual diff

On any failure the routine comments the gaps and **removes the label** instead of
merging (re-label after fixing). PRs without the label are never merged automatically,
and the Ruleset gates still bind the routine — it cannot bypass required checks.

**Label authority**: anyone with the Triage role or higher can apply labels on GitHub,
so granting Triage effectively grants merge-approval authority. The gatekeeper
therefore verifies the `labeled` timeline-event actor against a maintainer allowlist
(currently `juhy0987`) before merging — extend the allowlist deliberately, in the same
PR that adds a collaborator.

---

## 2. CODEOWNERS Strategy

- No single point of failure: cover critical paths with person + team overlap.
- `.github/`, CI workflows, and deployment paths must be covered.
- No global `*` pattern (merge bottleneck).
- **Status**: not yet registered — add `.github/CODEOWNERS` when maintainer GitHub
  handles are confirmed, and update this section in the same PR.

---

## 3. GitHub Actions Design Rules

### 3.1 Workflow Structure

Workflows are **split by concern** (readability, clear Actions UI grouping):

| File | Concern | Jobs |
|---|---|---|
| `ci-quality.yml` | Code quality (PR gate) | `Lint`, `Type Check`, `Test`, `Integration Test` |
| `ci-convention.yml` | PR/commit metadata format | `Commit Lint`, `PR Title Lint`, `Linked Issue Check`, `Branch Name Lint` |
| `ci-docs.yml` | Documentation policy | `Docs Sync Check` |
| `ci-review.yml` | Review presence (PR gate, commit status) | `Review Presence` |
| `ci-nightly.yml` | Scheduled full-stack verification | `E2E Test` |

- Jobs run independently in parallel (fast feedback). The only dependency is the
  lightweight `Detect Sources` bootstrap guard in `ci-quality.yml`.
- uv cache is enabled (`astral-sh/setup-uv` with `enable-cache: true`).
- New jobs go into the matching concern file; a genuinely new concern gets a new file.

### 3.2 Bootstrap Guard

Aaestro starts as a docs/ruleset-first repository. Quality jobs check for
`pyproject.toml` + `Makefile` and **skip** when absent — skipped required checks pass
the gate, so documentation PRs are never blocked while keeping the Ruleset registered
from day one. The guard removes itself from relevance the moment code lands.

### 3.3 Job Add/Change Procedure

1. Register the name in [status-checks.md](status-checks.md) first.
2. Add the job (name matching the document).
3. Register in the Ruleset `required_status_checks`.
4. Add to the PR template checklist.
5. All in the **same PR**.

### 3.4 Failure Handling

- Default: job failure blocks the merge (Required check).
- `continue-on-error: true` is forbidden on Required jobs (it bypasses the gate).
- Informational jobs (e.g. `Branch Name Lint`) are **not** registered as Required and
  may use `continue-on-error: true`. Use `if: ${{ !cancelled() }}` instead of
  `if: always()` (the latter wastes runners on manual cancellation — an expression
  starting with `!` must be wrapped in `${{ }}` or YAML parsing breaks).

---

## 4. Review Checklist

- [ ] PR template CI section filled in completely
- [ ] Required check names match [status-checks.md](status-checks.md)
- [ ] No `continue-on-error: true` on Required jobs
- [ ] CODEOWNERS changes keep fallback approvers
- [ ] Workflow changes update docs + Ruleset in the same PR

---

## Appendix A. Allowed Bots/Apps

| Account/App | Purpose | Bypass Scope |
|---|---|---|
| `dependabot[bot]` | Dependency update PRs (`pyproject.toml` / `uv.lock`) | `Linked Issue Check` skipped |
| _(update this table via PR)_ | - | - |
