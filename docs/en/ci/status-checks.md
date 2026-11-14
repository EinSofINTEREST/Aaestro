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
| `Review Presence` | `ci-review.yml` / `judge` (commit status) | Reports whether ≥ 1 review counts — see [Review Presence](#review-presence). Informational, not a merge gate (not in the Ruleset) | No |
| `Review Presence Signal` | `ci-review-signal.yml` / `signal` | Permissionless relay of review events that wakes `ci-review.yml` through `workflow_run` — see [Review Presence](#review-presence). Its own result means nothing; a bot's review leaves it `action_required` | No |
| `Branch Name Lint` | `ci-convention.yml` / `branch-lint` | Warns on `{category}/#{issue}/{summary}` violations — informational | No |
| `E2E Test` | `ci-nightly.yml` / `e2e` | Nightly full-stack run with fake LLM provider — not a merge gate | No |

## Review Presence

`Review Presence` is **informational**: it is not registered in the Ruleset
`main-merge-gate`, so it does not block the merge (a user decision, #537). The sections
below describe what the check reports and what would apply if it were promoted to a
required check later.

It is a **commit status**, not a job check. `ci-review.yml` (job
`Review Presence Judge`, deliberately a different name) judges the PR and writes the
status on the PR head commit. Scheduled runs do not execute on the PR head, so a job
result could never become that PR's check — every trigger therefore writes the same
status context.

- **Triggers**: PR opened / synchronize / reopened / ready_for_review
  (`pull_request_target`, judged directly). Review submitted / edited / dismissed and
  review comment created / edited / deleted reach `ci-review-signal.yml`, a workflow with
  no permissions and one `echo` step; its run fires `workflow_run` (`requested` and
  `completed`), and that runs `ci-review.yml` from the default branch, which judges the
  PRs GitHub lists for the run (all open PRs when the list is empty, as it is for a fork).
  The relay exists because a `pull_request_review` run waits for approval when the account
  behind the event needs it — every Copilot review ended `action_required` with no job
  (#574) — while `workflow_run` still fires for a run that waits for approval and its own
  run needs none. Also: a schedule at minutes 7, 22, 37 and 52 of every hour for all open
  PRs (reactions do not fire workflows, so a review bot's 👍 lands only through this
  path), and manual `workflow_dispatch`. PR and signal events use one concurrency group
  per PR number (a group name is case-insensitive, so branch names are not used); full
  judgments — the schedule, manual runs and a fork's signal that names no PR — share
  another.
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
  least one review") — a review arrives over time, so missing one is not a failure.
  `pending` blocks the merge only if the check is registered as required; today it does
  not.
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
- **Gate integrity**: the automatic paths — `pull_request_target`, `workflow_run` and
  `schedule` — run the default branch's workflow file, which checks the judging script
  out from the default branch, so a PR that edits `ci-review.yml` or
  `ci-review-signal.yml` cannot change how its own head is judged. A manual
  `workflow_dispatch` runs the workflow file of the ref its runner picks; only
  write-access accounts can start it, the same trust level that can already write a
  status directly (Known limits). The signal runs from the PR side, but it holds no token and passes nothing on: the judge
  reads the PR numbers GitHub put in the event payload and re-reads every review through
  the API. The workflow never checks out PR code.
- **Known limits**:
  - Any token with write access (a person, or any workflow in this repository) can write
    a commit status directly. A forgery written **once** differs from the judgment, so the
    next judgment overwrites it — but the schedule is best-effort: GitHub may delay runs
    under load or drop them. Measured on #574, scheduled runs came 3.4 to 8.7 hours apart
    (6.5 on average), so a status fixed only by the schedule can stay wrong for hours.
  - A 👍 from a review bot with no review event lands only with the next judgment of any
    kind — usually the schedule, so hours later.
  - The signal runs from the PR's copy of `ci-review-signal.yml`. A PR that deletes it, or
    a branch made before the file existed, sends no signal for reviews; its reviews are
    judged at the next push (`pull_request_target`) or by the schedule.
  - GitHub blocks `pull_request_target` by default in public repositories from
    2026-12-02 ("Securely using pull_request_target"). Unless the repository allows it,
    PR events stop being judged then; that is tracked separately from this check.
  - That `workflow_run` fires for a signal run waiting for approval is not stated by the
    GitHub docs. They say `completed` "occurs when a workflow run is completed, regardless
    of whether the workflow was successful", and a run waiting for approval is created
    already `completed` with conclusion `action_required` (run 43603239882, attempt 1);
    other public repositories report both `requested` and `completed` firing for such runs.
    Confirmed here only after the change reaches the default branch, because GitHub runs
    `workflow_run` workflows from there. If it does not fire, reviews are judged only at the
    next push or by the schedule; PR events are judged as before.
  - A forgery written **repeatedly** by a write-access token cannot be beaten by the
    judgment. If that loop spends the 1000 statuses of the sha, the judge's writes fail
    and the forgery stays.
  - The judge decides whether to write by (state, description) only; it does not look at
    who wrote the current status (its source).
  - Full judgments (the schedule, manual runs, a fork's review signal) and per-PR judgments
    can overlap: reading and writing are not atomic and the write does not check freshness.
    If a review or dismissal lands in the seconds between a full judgment's read and its
    write, the older verdict can overwrite the newer one; the next judgment of that PR
    corrects it.
  - In a public repository, scheduled workflows are disabled automatically after 60 days
    without repository activity.
  - If the check is promoted to required, registering it in the Ruleset with the GitHub
    Actions app as the expected (pinned `integration_id`) source blocks direct forgery
    with a person's token: GitHub documents that "if the status is set by any other
    person or integration, merging won't be allowed".
    Forgery from another workflow in the same repository remains, since it runs as the
    same app. Not verified: that a commit status written with `GITHUB_TOKEN` (rather than
    a check run) matches the GitHub Actions app as the source.
  - Therefore this check is **not enforcement** for PRs that change workflows, required
    or not. Those PRs must be reviewed by a human; an enforcing mechanism (CODEOWNERS on `.github/`, Ruleset
    rules) is a repository setting and a user decision, outside this check.
- **When no reviewer answers**: today the only trusted human is the PR author, who cannot
  count. If every review bot stays silent (quota, outage), the status stays `pending`
  (and would hold the gate shut if it were required). Paths out:
  - re-request a bot review (for example comment `@codex review`)
  - if the check is required: a Ruleset bypass for it, or a second collaborator — both are
    repository settings and a user decision

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
