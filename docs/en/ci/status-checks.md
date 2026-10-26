# Required Status Checks — Single Source

**[한국어](../../ko/ci/status-checks.md)** | English

This document is the **single source of truth** for the required status check names used
as PR merge gates. The GitHub Ruleset and the PR template must match these names
**verbatim**.

## Naming Rules

- The `Name` column records the exact check name registered in the GitHub Ruleset
  (a GitHub Actions job `name:` becomes the check context — Title Case allowed;
  `Review Presence` is a commit status context instead, see below).
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
| `Review Presence` | `ci-review.yml` / `judge` (commit status) | Requires ≥ 1 review that counts — see [Review Presence](#review-presence) | Yes |
| `Branch Name Lint` | `ci-convention.yml` / `branch-lint` | Warns on `{category}/#{issue}/{summary}` violations — informational | No |
| `E2E Test` | `ci-nightly.yml` / `e2e` | Nightly full-stack run with fake LLM provider — not a merge gate | No |

## Review Presence

`Review Presence` is a **commit status**, not a job check. `ci-review.yml` (job
`Review Presence Judge`, deliberately a different name) judges the PR and writes the
status on the PR head commit. Scheduled runs do not execute on the PR head, so a job
result could never become that PR's check — every trigger therefore writes the same
status context.

- **Triggers**: PR opened / synchronize / reopened / ready_for_review
  (`pull_request_target`); review submitted / edited / dismissed; review comment
  created / edited / deleted; a schedule at minutes 7, 22, 37 and 52 of every hour for
  all open PRs (reactions do not fire workflows, so a review bot's 👍 lands only through
  this path); and manual `workflow_dispatch`. PR events use one concurrency group per
  PR; the schedule and manual runs share another.
- **Trusted accounts** — only these can count, and never the PR author:
  - a human whose `author_association` is `OWNER`, `MEMBER` or `COLLABORATOR`
    (the repository is public — an outsider's review does not open the gate)
  - a bot on the review-bot allowlist (`REVIEW_BOTS` in `ci-review.yml`; default
    `chatgpt-codex-connector[bot]`, `copilot-pull-request-reviewer[bot]`,
    `coderabbitai[bot]`). `github-actions[bot]` is deliberately not on it, so a workflow
    cannot fabricate a review.
- **Counts as a review** — by a trusted account:
  - an `APPROVED` or `CHANGES_REQUESTED` review
  - a review with at least one line comment (any state)
  - a `COMMENTED` review with a non-empty body by a trusted human
  - a 👍 (`+1`) reaction on the PR body by an allowlisted review bot
- **Does not count**: anything by an untrusted account (outsiders, unlisted bots), a bot's
  body-only review even if allowlisted (e.g. Copilot's quota notice), the author's own
  review, 👀, and `DISMISSED` / `PENDING` reviews.

  This is narrower than the definition in issue #514, which counted any non-author account.
- **States**: `success` when something counts, otherwise `pending` ("waiting for at
  least one review") — a review arrives over time, so missing one is not a failure,
  and `pending` still blocks the merge.
- **Writes**: before writing, the judge reads the current `Review Presence` status on the
  head sha and writes only when state or description differs. GitHub caps statuses at
  1000 per sha and context ("Attempts to create more than 1000 statuses will result in a
  validation error"), so rewriting the same value every run would stop the gate after
  about ten days. A status belongs to a sha, not to a PR, so the judgment is made per
  sha: every open PR whose head is that sha is judged, and `success` is written only
  when all of them count — otherwise `pending`, naming the PRs still waiting ("waiting for
  at least one review on #12, #34"). One PR's review never opens another PR's gate. An
  event for PR N also judges the other open PRs on N's head (found by `head.sha` in the
  open-PR list; `commits/{sha}/pulls` returns PRs that *contain* the commit, not PRs it
  heads). A closed PR is judged only when no open PR heads its sha — the gate guards open
  PRs, so a closed PR never holds an open one back. `--all-open` groups by sha and writes each sha once, reading and writing right
  after judging that sha before moving to the next. A failure on one PR (API error, malformed response)
  is logged and skipped; the run still exits 1.
- **Gate integrity**: the judging script is always checked out from the default branch,
  and PR events use `pull_request_target`, which runs the default branch's workflow file —
  a PR that edits `ci-review.yml` cannot post `success` for itself through that path. The
  workflow never checks out PR code; it only reads the PR through the API.
- **Known limits**:
  - `pull_request_review` and `pull_request_review_comment` run the workflow file from the
    PR side, and any token with write access (a person, or any workflow in this
    repository) can write a commit status directly. A forgery written **once** differs
    from the judgment, so the next judgment overwrites it — but the schedule is
    best-effort: GitHub may delay runs under load or drop them.
  - A forgery written **repeatedly** by a write-access token cannot be beaten by the
    judgment. If that loop spends the 1000 statuses of the sha, the judge's writes fail
    and the forgery stays.
  - The judge decides whether to write by (state, description) only; it does not look at
    who wrote the current status (its source).
  - The schedule and PR events can overlap. If a dismissal lands in the seconds between a
    scheduled judgment and its write, the older verdict can overwrite the newer one until
    the next judgment of that PR.
  - In a public repository, scheduled workflows are disabled automatically after 60 days
    without repository activity.
  - Registering the check in the Ruleset with the GitHub Actions app as the expected
    source blocks direct forgery with a person's token: GitHub documents that "if the
    status is set by any other person or integration, merging won't be allowed".
    Forgery from another workflow in the same repository remains, since it runs as the
    same app. Not verified: that a commit status written with `GITHUB_TOKEN` (rather than
    a check run) matches the GitHub Actions app as the source.
  - Therefore this check is **not enforcement** for PRs that change workflows. Those PRs
    must be reviewed by a human; an enforcing mechanism (CODEOWNERS on `.github/`, Ruleset
    rules) is a repository setting and a user decision, outside this check.
- **When no reviewer answers**: today the only trusted human is the PR author, who cannot
  count. If every review bot stays silent (quota, outage), the gate does not open. Paths
  out:
  - re-request a bot review (for example comment `@codex review`)
  - a Ruleset bypass for this check, or a second collaborator — both are repository
    settings and a user decision

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
