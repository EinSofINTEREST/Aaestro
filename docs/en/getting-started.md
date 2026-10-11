# Getting Started

**[한국어](../ko/getting-started.md)** | English

> **Status**: The framework layers are implemented and the reference solution validates
> end to end. `malkuth run` needs deployed agent containers; commands marked
> *(not implemented yet)* are on the roadmap.

## Prerequisites

- Python **3.12+**
- [uv](https://docs.astral.sh/uv/) (package manager — lockfile is committed)
- Docker Engine **24+** (agent isolation runtime)
- `make` (task automation)

## Setup

```bash
git clone https://github.com/EinSofINTEREST/Malkuth.git
cd Malkuth
uv sync                 # install pinned dependencies
```

Quality gates used locally and in CI:

```bash
make lint               # ruff check + ruff format --check
make typecheck          # mypy (strict on src/malkuth/core)
make test               # pytest unit tests + coverage gate (≥ 70%)
make test-integration   # Docker-based integration tests
```

## Assembling a First Solution

A solution is **assembled from modules** — you wire existing agents and modules into a
graph instead of writing framework code.

### 1. Declare an agent

```yaml
# agents/researcher/manifest.yaml
apiVersion: malkuth/v1
kind: Agent
metadata:
  name: researcher
  version: 0.1.0
  group: research               # resource scope membership (optional)
spec:
  model: {provider: anthropic, name: claude-sonnet-5}
  promptset: {ref: promptsets/researcher@0.1.0}
  skillsets:
    - ref: skillsets/web-search@0.2.0
  memory:
    spaces:
      - {ref: memorysets/agent-longterm@0.1.0, as: longterm}
  a2a: {enabled: true}
  runtime:
    resources: {cpu: "1.0", memory: 1Gi}
    env_allowlist: [ANTHROPIC_API_KEY]
```

### 2. Wire a graph (the goal)

```yaml
# graphs/research-pipeline.yaml
apiVersion: malkuth/v1
kind: Graph
metadata: {name: research-pipeline, version: 1.0.0}
spec:
  mode: mission                 # mission (terminating) | service (perpetual)
  goal: answer a query with a researched report
  nodes:
    - {id: planner, agent: agents/planner@0.1.0}
    - {id: researcher, agent: agents/researcher@0.1.0}
    - {id: writer, agent: agents/writer@0.1.0}
  edges:
    - {from: START, to: planner}
    - {from: planner, to: researcher}
    - {from: researcher, to: writer}
    - {from: writer, to: END}
  connections:                  # A2A peer-call allowlist (equal peers)
    - {caller: researcher, callee: planner}
```

### 3. Declare group resources (optional)

```yaml
# groups/research.yaml
apiVersion: malkuth/v1
kind: Group
metadata: {name: research}
spec:
  quotas: {cpu: "8.0", memory: 16Gi, max_agents: 10}
  secrets: [SEARCH_API_KEY]
  memory:
    spaces:
      - {ref: memorysets/domain-knowledge@0.1.0, as: knowledge, mode: rw}
```

### 4. Validate before deploying

Contract validation runs before anything starts — **if any deploy-time contract check
fails, no container is started**:

```bash
malkuth validate                                # every graph in the repository
malkuth deploy graphs/research-pipeline.yaml    # one graph
malkuth status                                  # what is declared here
malkuth config dev                              # resolved settings for an environment
```

Both `validate` and `deploy` exit non-zero when a check fails, so they compose with
scripts. Add `--json` for machine-readable output.

Integrity checks compare records against reality — orphaned checkpoints, dangling
module refs, ghost containers:

```bash
malkuth check observed-state.yaml
```

### 5. Run a graph

`run` submits a mission graph and waits for it to reach END. Contracts are validated
before anything is submitted, and agent addresses are given explicitly — the CLI does
not guess container ports:

```bash
malkuth run graphs/research-pipeline.yaml \
  --input '{"query": "..."}' \
  --agent-token "${MALKUTH_AGENT_TOKEN:-e2e-token}" \
  --agent planner=http://127.0.0.1:18082 \
  --agent researcher=http://127.0.0.1:18083 \
  --agent writer=http://127.0.0.1:18084
```

Bring the agents up first with `make e2e-up` (fake model provider, no real LLM calls).

Service graphs have no completion, so they are driven with `--service` instead:

```bash
malkuth run graphs/feed-monitor.yaml --service \
  --agent-token "${MALKUTH_AGENT_TOKEN:-e2e-token}" \
  --agent watcher=http://127.0.0.1:18082
```

One iteration is a single `START → … → END` pass; the loop repeats until you
interrupt it. `Ctrl-C` **drains** rather than kills — the current iteration finishes
first, so no half-done pass is left behind. Use `--iterations N` to stop after a
fixed number of passes.

Direct requests reach any agent's Control API without a graph run:

```bash
curl -X POST http://127.0.0.1:18081/v1/invoke \
  -H "authorization: Bearer ${MALKUTH_AGENT_TOKEN:-e2e-token}" \
  -H 'content-type: application/json' \
  -d '{"task_id":"t1","run_id":"direct-1","node_id":null,
       "input":{"msg":"hello"},"trace":{"trace_id":"tr-1"}}'
```

The Control API requires a per-agent token; `/v1/health` is the only unauthenticated
endpoint (Docker's healthcheck calls it directly). In production the runtime mints a
random token per agent and injects it — the dev stack uses a fixed placeholder so the
same code path stays exercised.

The placeholder differs per stack: `make e2e-up` (`compose.e2e.yaml`) defaults to
`e2e-token`, while the plain dev stack (`compose.yaml`) defaults to `dev-local-token`.
Pass the one matching the stack you started, or export `MALKUTH_AGENT_TOKEN` before
bringing it up so both sides read the same value. A mismatched token surfaces as `401`
on every node call.

Still on the roadmap *(not implemented yet)*: `malkuth run trace`, `agent invoke`,
`agent logs`, `replay`, `memory reindex`.

## Where to Go Next

- [architecture.md](architecture.md) — how the pieces fit together
- [modules.md](modules.md) — building skillsets, promptsets, memorysets
- [testing.md](testing.md) — writing deterministic tests around LLMs
- [.claude/rules/](../../.claude/rules/README.md) — the full development ruleset
