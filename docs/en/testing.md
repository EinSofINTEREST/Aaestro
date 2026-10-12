# Testing Strategy

**[한국어](../ko/testing.md)** | English

How Aaestro stays deterministic around non-deterministic models. Normative rules:
[06-testing.md](../../.claude/rules/06-testing.md).

## Principles

1. **Tests never call a real LLM** — a scripted `FakeModel` (or recorded cassettes)
   stands in. CI has no provider API keys on purpose; a test that needs one is a bug.
2. **Tests never depend on external services** — Docker fixtures (testcontainers) or
   fakes only.
3. **Deterministic and parallelizable** — time-dependent logic (backoff, health
   intervals, service idle) is tested with injected clocks, never `sleep`.
4. Embeddings use a deterministic hash-based fake — no embedding API calls.

## Test Pyramid & Layout

```
tests/
├── unit/           # no external deps — mirrors src/aaestro structure   (~70%)
├── integration/    # Docker / real MCP server fixtures                  (~25%)
├── e2e/            # full compose stack, fake LLM provider container    (~5%)
└── fixtures/       # FakeModel, fake MCP server, builders, test yamls
```

- Markers: `@pytest.mark.integration`, `@pytest.mark.e2e`
- `make test` runs unit only; `make test-integration` and `make test-e2e` are separate
- E2E runs nightly in CI (`ci-nightly.yml`), not as a PR gate
- Shared E2E scaffolding (the compose stack and the control plane process) lives in
  `tests/e2e/conftest.py` — importing a fixture between test modules shadows it silently

### The browser tests

`tests/e2e/test_ui_browser.py` drives the operator UI with a real browser, because a form
can be miswired while the REST flow behind it stays green. It needs a browser and takes the
first one available:

1. the host's own chromium (`uv run playwright install --with-deps chromium`), or
2. the official Playwright image, started as a browser server on the Docker host network.

The second path needs no root and no host packages, so a development machine runs these
tests without installing anything beyond the dev dependencies. The test prints which path it
took. CI installs the browser natively and falls back to the image if that fails.

## What Must Be Covered

| Area | Focus |
|---|---|
| core | manifest / topology / group schema validation, scope resolution |
| orchestrator | config → StateGraph build, routing, state merge, mission & service mode rules |
| protocols | error mapping to `AaestroError`, A2A allowlist, tool namespacing |
| modules | ref parsing, schema snapshots (skillsets), render goldens (promptsets) |
| memory | space ACL, alias resolution (local > group > global), hybrid search merge, recall budget |
| agentd | tool loop bounds, cancellation cleanup, direct-task handling |
| runtime | container lifecycle, quota rejection (`RT_006`), drain semantics |

Service graphs additionally require: iteration accumulation, idle backoff progression,
drain-mid-iteration resume, and failure-streak halt (`GRAPH_005`) scenarios.

## Quality Gates (CI-enforced)

```bash
make lint          # ruff check + ruff format --check
make typecheck     # mypy — strict on src/aaestro/core
make test          # pytest unit + coverage ≥ 70% (fails below)
make test-integration
```

Coverage: a **single enforced gate** — ≥ 70% across `src/aaestro`, enforced by
`make test` via pytest-cov `--cov-fail-under=70`. The 90%+ (critical paths) and 100%
(error-conversion paths) figures are review targets, not separate CI gates.
Check names and merge-gate wiring: [ci/status-checks.md](ci/status-checks.md).

## Known Limitations

What the suite does **not** prove, so nobody mistakes green for complete:

- **No real model is ever called.** Unit tests use `FakeModel`; E2E runs the
  **standard executor** against a deterministic fake provider that speaks the
  Messages API — so prompt rendering, the tool loop, output shaping, and the
  provider binding all execute. What is still absent is a real provider's own
  behaviour: token limits, streaming quirks, provider-side rate limiting.
- **No real embedding API.** Memory tests use `HashEmbedder`, which is deterministic
  but does not model semantic similarity. Recall *ranking quality* is therefore not
  measured; only the merge, threshold, and budget mechanics are.
- **MCP is covered only in-process.** The `mcp` binding is exercised against a
  reference server process, crossing the real SDK but not a container boundary
  — see the Docker note below. (A2A now crosses one; see below.)
- **Docker-dependent tests skip without a daemon.** Integration and E2E tests report
  as skipped on machines without Docker. A green local run is not evidence that the
  container paths work — check the CI job.
- **A2A now crosses containers; transitive delegation does not.** Declared
  calls, undeclared directions, forged tokens, and the depth limit (`A2A_005`)
  are all checked across live containers in `tests/e2e/test_a2a.py`. The depth
  limit is verified by *injecting* a depth, not by an agent actually delegating
  onward — an agent calling a peer mid-task is still unproven end to end.
- **Auto-recall now runs end to end; its ranking quality does not.** A stored
  fact reaches a later task's prompt across the live stack — the agent talks to
  a containerised Memory Service over HTTP, the fake provider supplies the
  embeddings, and `tests/e2e/test_auto_recall.py` asserts on the prompt the
  model actually received. What that does *not* measure is **which** memories
  win: `HashEmbedder` and the fake endpoint are deterministic but do not model
  semantic similarity, so relevance ordering stays unproven (see the embedding
  note above).
- **Service runs survive both restart boundaries; automatic agent recovery is
  not covered.** An orchestrator restart carries checkpoints, the stored
  iteration count, and an externally-left drain request
  (`tests/e2e/test_service_restart.py`); killing an agent container halts the
  run with `GRAPH_005`, records that halt where another process can read it,
  and lets `resume_service` continue once the agent is back
  (`tests/e2e/test_agent_kill.py`). What is *not* covered is the runtime
  **restarting a dead agent on its own** (02 Lifecycle's backoff policy) —
  here the agent is revived by the test, standing in for an operator.
