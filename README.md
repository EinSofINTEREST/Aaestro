# Aaestro

**[한국어](docs/ko/README.md)** | English

A modular multi-agent orchestration framework built on LangGraph.

Aaestro composes each goal as a graph of **equal, Docker-isolated agents**. Agents carry
their own A2A endpoints and MCP servers, connect and disconnect through config-driven
wiring, and share nothing except the graph state and declared, scoped memory.

## Key Features

- **LangGraph orchestration** — graph topology declared in YAML, built into a
  `StateGraph` with checkpointing, resume, and conditional routing
- **One agent = one Docker container** — controlled through a standard Agent Control API,
  with resource limits, health checks, and graceful drain
- **Per-agent protocol isolation** — every A2A endpoint and MCP server belongs to exactly
  one agent; no shared tool gateways
- **Equal peers, three access paths** — orchestrated runs, interactive direct requests to
  any agent, and declared A2A peer calls; no hierarchy between agents
- **Two execution modes** — goal-oriented **mission** graphs that run to completion, and
  perpetual **service** graphs that iterate indefinitely with idle backoff
- **Everything is a module** — skillsets, promptsets, memorysets, and graphs are
  versioned, swappable deliverables; a solution is assembled, not written from scratch
- **Scoped resources** — secrets, memory, artifacts, and quotas managed at three scopes:
  **global / group / local**, resolved nearest-first
- **Context memory** — per-scope memory spaces with hybrid (vector + lexical) search
  indexes, token-budgeted recall, and declared retention
- **Assembled from a browser** — one control plane serves a REST API and a Web UI for
  composing graphs, deploying them into containers, and running them
  ([API](docs/en/api.md), [UI](docs/en/ui.md))

## Status

**Pre-v0.1.0.** The framework layers described above are implemented and covered by the
test suite, including the control plane REST API and the Web UI it serves. The remote
module registry and the Kubernetes runtime backend remain future work.

## Requirements

Python 3.12+ · [uv](https://docs.astral.sh/uv/) · Docker Engine 24+ (for the runtime,
integration, and E2E paths)

```bash
uv sync --frozen        # or: make install
```

## Commands

### Quality gates

```bash
make lint               # ruff check + ruff format --check
make typecheck          # mypy (strict on aaestro.core)
make test               # unit tests + coverage gate (>= 70%)
make check              # lint + typecheck + test — run this before pushing
```

Integration and E2E suites are opt-in because they need Docker:

```bash
make test-integration   # marked `integration` — Docker containers, real MCP sessions
make test-e2e           # marked `e2e` — full compose stack (nightly in CI)
```

Checkpointer integration tests skip unless their backends are addressable:

```bash
export AAESTRO_TEST_POSTGRES_URL=postgresql://aaestro:aaestro@127.0.0.1:15432/aaestro
export AAESTRO_TEST_REDIS_URL=redis://127.0.0.1:16379    # needs RediSearch (redis-stack)
```

### Images and stacks

```bash
make build              # aaestro/agent-base + agent-echo images
make up                 # dev stack — one echo agent, control port on 18080
make down
make e2e-up             # E2E stack — fake provider, memory service, 4 reference agents
make e2e-down
```

Both `up` targets rebuild `aaestro/agent-base` and the services on top of it first, so a
change under `src/` reaches the containers. The framework code lives only in the base
image — bringing a stack up without rebuilding it would validate stale code.

The E2E stack publishes agent control ports on **18081-18084**, the Memory Service on
**18090**, the checkpoint Postgres on **15433**, agent metrics on **19082-19084**, and A2A
ports on **19102-19104**. Its agents talk to a fake model provider, so nothing reaches a
real LLM.

Custom agents are not built by `make`. Their `Dockerfile` and `src/` live in the control plane's
material store, and a deploy refuses an agent whose image for that version is not built. The
repository ships the `claude-code` agent's materials as a seed:

```bash
uv run aaestro agent-push claude-code examples/materials/claude-code
uv run aaestro agent-build claude-code --wait   # exits non-zero with the log tail if it fails
```

### CLI

```bash
uv run aaestro <command>          # or `aaestro` inside an activated venv
```

| Command | What it does |
|---|---|
| `aaestro validate` | validate every graph, agent manifest, and module ref in the repo |
| `aaestro deploy <graph.yaml>` | validate one graph as a deploy gate (`--a2a-port-range`) |
| `aaestro status` | summarize declared agents, graphs, groups, and modules |
| `aaestro config [env]` | print the resolved configuration (`dev` / `staging` / `prod`) |
| `aaestro check <state.yaml>` | report integrity discrepancies against observed state |
| `aaestro run <graph.yaml>` | submit a mission or service run against agents you address yourself |
| `aaestro run --deployment <id>` | submit a run to a deployment — the control plane resolves the addresses |
| `aaestro run-list` / `run-drain <id>` / `run-resume <id>` | operate runs through a control plane |
| `aaestro run-steer <id> "<text>"` | add an instruction to the task a run is executing — applied at its next turn boundary, or not at all if the task ends first |

`--json` (before the subcommand) switches to machine-readable output; `--root` points at a
repository other than the working directory.

Running a graph needs the address of each agent's Control API — the orchestrator never
guesses where an agent lives. Against the E2E stack:

```bash
export AAESTRO_AGENT_TOKEN=e2e-token

# mission run — terminates at END and prints the final state
uv run aaestro run graphs/research-pipeline.yaml \
  --input '{"query": "aaestro architecture"}' \
  --agent planner=http://127.0.0.1:18082 \
  --agent researcher=http://127.0.0.1:18083 \
  --agent writer=http://127.0.0.1:18084

# service run — perpetual loop, bounded here so it terminates
uv run aaestro run graphs/feed-monitor.yaml --service --iterations 2 \
  --agent researcher=http://127.0.0.1:18083 \
  --agent planner=http://127.0.0.1:18082 \
  --agent writer=http://127.0.0.1:18084
```

A service run without `--iterations` runs until interrupted; `Ctrl-C` requests a drain, so
it stops after finishing the current iteration rather than mid-flight.

The checkpoint backend comes from configuration — `--checkpointer` only overrides it for
one run. `postgres` and `redis` additionally need a connection URL, which is best supplied
out of band so credentials never land in a file:

```bash
AAESTRO_ENV=prod \
AAESTRO_ORCHESTRATOR__CHECKPOINTER_URL=postgresql://user:pass@host:5432/aaestro \
  uv run aaestro run graphs/research-pipeline.yaml --agent ...
```

With a durable backend, re-running the same `--run-id` resumes that run from its last
checkpoint — including from a different process. The default `memory` backend disappears
with the process, so runs on it cannot be resumed.

`run-list` / `run-drain` / `run-resume` / `run-steer` talk to a control plane over `--control-url`, and
they only see runs that were recorded — set `orchestrator.run_store` so the run and the
control plane share one store. `run-drain` leaves a request and returns immediately; the
process driving the run stops at its next iteration boundary.

`--deployment` is the other way to run a graph, and it needs no `--agent` flags at all: the
control plane already knows where the deployed agents are.

```bash
uv run aaestro run --deployment dep-2fc94a0b5f24 \
  --input '{"query": "aaestro architecture"}' \
  --control-url http://127.0.0.1:8700
```

It returns once the run finishes (`--no-wait` to return at submission). `run-resume`
continues a run that a service graph *halted* after repeated failures; a run you drained on
purpose is submitted again rather than resumed. A control plane without a deployment surface
drives no runs at all and answers `run-resume` with `501` instead of reporting a resume that
never happened. See the [Control Plane API](docs/en/api.md) for the full surface.

### Long-running processes

```bash
python -m aaestro.agentd        # in-container agent daemon — Control API on 8080
python -m aaestro.memory        # Memory Service — HTTP surface plus the async indexing loop
python -m aaestro.orchestrator  # Control Plane — REST API + Web UI on /ui
```

`agentd` is what the runtime layer starts inside every agent container; it reads
`AAESTRO_MANIFEST`, `AAESTRO_AGENT_TOKEN`, `AAESTRO_ROOT`, and — when memory is wired —
`AAESTRO_MEMORY_URL` with `AAESTRO_MEMORY_TOKEN` or `AAESTRO_MEMORY_TOKEN_FILE`.

The Memory Service reads `AAESTRO_REPO_ROOT`, `AAESTRO_MEMORY_PORT`, and
`AAESTRO_MEMORY_TOKENS_PATH`. It must run as its own process: appends commit immediately
but indexing is asynchronous, so without the loop nothing becomes searchable.

The Control Plane reads `orchestrator.run_store`, `control_host`, and `control_port` from
configuration and refuses to start without a store — serving an empty list would read as
"there are no runs". Set `orchestrator.control_token` and send it as a bearer token; every
`/v1/*` route requires it, and binding a non-loopback address without one is refused
(`CFG_001`). `GET /v1/health` and the UI's static files stay unauthenticated.

Setting `orchestrator.deployment_store` additionally opens the deployment surface: the
process then starts agent containers itself, drives runs submitted against them, and can
resume a halted one. Without it those routes stay closed and the process only reports runs
that other processes drive.

Setting `orchestrator.material_store` lets the process store custom agents' build materials,
and setting `orchestrator.build_store` as well opens image builds. With both set, the process
bakes images on request, and a deploy refuses any custom agent whose image for its version is
not built. Without a material store the material routes answer `CFG_001`; without both stores
the image routes do not exist, and deploys use the image each manifest names.

Open `http://127.0.0.1:8700/` for the Web UI — catalog, graph and agent editors, deployment,
and runs. It is served from the same process and calls only the documented REST API
([API reference](docs/en/api.md), [UI guide](docs/en/ui.md)).

All three honour `AAESTRO_ENV`, `AAESTRO_CONFIG_DIR`, `AAESTRO_LOG_LEVEL`,
`AAESTRO_LOG_FORMAT`, and `AAESTRO_METRICS_PORT`.

### Configuration

Configuration lives in `configs/{env}.yaml` (`dev`, `staging`, `prod`). The CLI selects it
positionally; the long-running processes read `AAESTRO_ENV`:

```bash
uv run aaestro config prod            # print the resolved prod configuration
```

Overrides use a **double underscore** to separate section from key:

```bash
AAESTRO_ORCHESTRATOR__NODE_TIMEOUT_S=600 uv run aaestro config
```

Single-underscore `AAESTRO_*` variables are process settings (`AAESTRO_AGENT_TOKEN` and
the like), never configuration — the loader ignores them on purpose, so that injecting
agent env into a container cannot corrupt that container's configuration.

For a walkthrough that assembles a solution from scratch, see
[Getting Started](docs/en/getting-started.md).

## Documentation

| Document | Description |
|---|---|
| [Architecture](docs/en/architecture.md) | Layers, interaction model, execution modes, resource scoping |
| [Getting Started](docs/en/getting-started.md) | Environment setup and first solution |
| [Module System](docs/en/modules.md) | Skillsets, promptsets, memorysets, graphs, groups |
| [Control Plane API](docs/en/api.md) | REST reference — catalog, authoring, deployments, runs |
| [Web UI](docs/en/ui.md) | Assembling, deploying, and running a system in a browser |
| [Testing](docs/en/testing.md) | Test strategy and quality gates |
| [CI Conventions](docs/en/ci/conventions.md) | Merge gates, workflow design rules |
| [Required Status Checks](docs/en/ci/status-checks.md) | Single source of truth for check names |

Development rules (the authoritative ruleset) live in [.claude/rules/](.claude/rules/README.md).

## Technology Stack

Python 3.12+ · uv · LangGraph · pydantic v2 · Docker · `a2a-sdk` · `mcp` ·
FastAPI · structlog · Prometheus · pytest

## Conventions (Summary)

- Commit messages: `[FEAT]: ...` / `[FIX]: ...` / `[REFAC]: ...` / `[DOCS]: ...` /
  `[CHORE]: ...` — written in Korean
- PR titles: `[FEAT#N] title` (category + issue number)
- Branches: `{category}/#{issue}/{summary}`
- Documentation: English first (`docs/en/`), Korean mirror (`docs/ko/`)

See [.claude/rules/07-code-style.md](.claude/rules/07-code-style.md) and
[.claude/rules/08-workflow.md](.claude/rules/08-workflow.md) for the full conventions.
