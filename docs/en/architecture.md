# Architecture Overview

**[한국어](../ko/architecture.md)** | English

Aaestro orchestrates multiple AI agents through LangGraph state graphs, with each agent
isolated in its own Docker container. This document is a condensed overview; the
normative rules are in [01-architecture.md](../../.claude/rules/01-architecture.md).

## Layers

```
┌──────────────────────────────────────────────┐
│  Control Plane Layer                         │  API / CLI / graph lifecycle
├──────────────────────────────────────────────┤
│  Orchestration Layer (LangGraph)             │  StateGraph build, routing, checkpoints
├──────────────────────────────────────────────┤
│  Agent Runtime Layer (Docker)                │  container lifecycle, Agent Control API
├──────────────────────────────────────────────┤
│  Protocol Layer — per-agent isolated         │  A2A server/client, MCP client + servers
├──────────────────────────────────────────────┤
│  Module Layer                                │  skillsets, promptsets, memorysets, graphs
├──────────────────────────────────────────────┤
│  Storage & Observability Layer               │  checkpoints, memory + index, logs, metrics
└──────────────────────────────────────────────┘
```

## Interaction Model — Equal Agents

Agents are **equal peers**: there is no hierarchy, and every agent is directly
addressable. Three access paths exist:

1. **Orchestrated run** — the graph invokes agents by its edges and conditions.
   Order comes from wiring, not rank.
2. **Direct request** — a client can interactively invoke any agent, independent of any
   graph run (`node_id=None`, `default` prompt template, no graph-state access).
3. **Peer call (A2A)** — an agent delegates to or queries another agent, allowed only for
   pairs declared in the graph's `connections` allowlist. Symmetric: either direction can
   be declared; mutual declaration enables two-way collaboration.

The orchestrator is a router, not a "super agent" — collaboration structure is defined
entirely by wiring.

## Execution Modes

| | **Mission** | **Service** |
|---|---|---|
| Goal | Complete a specific outcome | A perpetually repeating task |
| Termination | Reaches END, returns result | Operator stop / explicit stop condition |
| Topology | END required; cycles need `max_iterations` | Same, plus an idle policy — the loop belongs to the runner, not the graph |
| Checkpoint | Per node | Per node + per iteration (resumes across restarts) |

One service iteration is a single `START → … → END` pass. The repetition belongs to the
runner, not to the graph — a graph that cycles into itself never finishes a single pass,
so there is no iteration boundary to checkpoint.

Service graphs back off exponentially when idle (no busy-looping model calls) and halt
with an alert after a configured failure streak. If iterations don't need shared state,
prefer a scheduled (recurring) mission instead.

## Resource Scoping — Global / Group / Local

Agent resources (secrets, memory spaces, artifacts, quotas) are managed at three scopes:

```
global  ─ every agent              (groups/global.yaml — reserved group)
group   ─ members of the group     (groups/<name>.yaml)
local   ─ a single agent           (agents/<name>/manifest.yaml)
```

- A group is a **resource boundary, not a rank** — it grants no calling privileges and
  creates no hierarchy. Wiring (edges/connections) and scoping are orthogonal axes.
- An agent belongs to at most one group (`metadata.group`); everyone is implicitly a
  member of the reserved `global` group.
- Name collisions resolve nearest-first: **local > group > global**.

## Access Control — Decided Outside the Container

An agent container may hold every permission inside itself, so a check made there is a
convenience, not a boundary. Permissions that change while agents run are decided **outside the
controlled container, on every request**:

```
                 ┌──────────── Control Plane ────────────┐
                 │  access registry — identities,         │
                 │  declarations, grants, ceilings, feed  │
                 └───▲──────────────▲──────────────▲──────┘
          decisions  │              │              │  + change feed
         ┌───────────┴──┐  ┌────────┴──────┐  ┌────┴────────────────┐
         │ Memory       │  │ Egress proxy  │  │ callee's A2A server │   enforcement points
         │ Service      │  │ model API,    │  │                     │
         │              │  │ CONNECT, MCP  │  │                     │
         └───────▲──────┘  └────────▲──────┘  └────▲────────────────┘
                 │                  │              │
         ┌───────┴──────────────────┴──────────────┴───┐
         │ agent containers — internal network only     │
         └──────────────────────────────────────────────┘
```

| Controlled | Enforcement point | Decided as |
|---|---|---|
| memory access | Memory Service | `memory` on a space id, `ro` / `rw` |
| outside calls | egress proxy | `egress` on `host[:port]` |
| remote MCP tools | egress proxy (terminates the server) | `mcp_tool` on `server/tool` |
| a caller's A2A calls | the callee's A2A server | `a2a` on the callee |

- **Declarations are the baseline.** Manifests, groups and deployed graphs give the default
  permissions. Operators narrow them at any time (revoke, `rw`→`ro`); a dedicated permission agent
  widens them, only within the expansion ceiling the registry enforces.
- **Next request, no restart.** Enforcement points cache decisions briefly and drop them when the
  registry's version moves.
- **An agent's A2A server only takes new tasks.** Reading, listing, subscribing to or cancelling a
  task, push-notification settings, the extended card, and messages addressed to an existing task
  are refused with the same answer whoever asks — a peer cannot see or cancel work another caller
  delegated, nor learn whether a task exists.
- **When the registry is unreachable**, anything not already cached is denied, and cached allows
  keep working until they expire — so a revocation made during an outage may not apply until the
  registry is back.
- **The proxy is the only way out.** With it on, agents run on an internal Docker network; the
  proxy holds the model key and remote MCP credentials, so agents never have them.
- **A task id belongs to whoever sent it.** The sender chooses the `task_id`, and the framework
  does not assume it is unguessable. So an agent keys its per-task state (the idempotency cache,
  the in-flight table behind `/v1/cancel`) by the caller together with the id, and tasks from the
  Control API form their own space: a peer never reaches the results or state of a graph or direct
  task. Separation between two peers holds in registry mode only — in token mode the caller is a
  header checked with the deployment's shared signing key, so one peer can name another — and the
  SDK's task lookup and cancel methods are not covered yet. Retrying the same id is idempotent only
  for the same caller. A peer's message body is checked before the agent runs: a missing required
  field is refused with `VAL_001`, and a malformed body or identifier (empty, over 128 characters,
  unprintable characters, surrounding whitespace) with `VAL_002`; the refusal never repeats the
  value. Logs and responses keep the id exactly as the caller sent it.

Not reached at run time: effects that stay inside the container, data an agent already read, and
secrets injected as environment variables (those need a redeploy). See the
[API reference](api.md#access-registry) and the [runbook](runbooks/access-control.md).

## Context Memory

Agents accumulate searchable context in declared memory spaces
(scopes: `run` / `local` / `group` / `global`), served by the framework Memory Service:

- Hybrid index per space: vector (embeddings) + lexical (BM25) + metadata filters,
  merged with reciprocal rank fusion
- Search hits are materialised by id lookup in the store, so a space's size never hides an
  older memory that the index still holds (there is no scan horizon); corrections are resolved
  the same way, so a superseded memory is dropped from every search path
- Token-budgeted auto-recall at task start; further searches via an explicit tool
- Append-only entries with provenance; retention and compaction declared per memoryset
- The agent-side client (`memory/client.py`) never imports server code (store, service, index),
  so the agent daemon stays free of storage dependencies

See [09-memory-context.md](../../.claude/rules/09-memory-context.md).

## Control Flow (Graph Run)

```
Client → Control Plane → Orchestrator (StateGraph)
                             │ node execution
                             ▼
                       Runtime Layer ──HTTP──▶ Agent Container (agentd)
                             │                   ├─ promptset render
                             ▼                   ├─ model call + tool loop
                       Checkpointer              ├─ skillset / MCP tools
                             │                   └─ A2A peer calls (allowlisted)
                             ▼
                       edge evaluation → next node / END / next iteration
```

## Technology Stack

| Concern | Choice |
|---|---|
| Language / packaging | Python 3.12+, uv |
| Orchestration | LangGraph + langchain-core |
| Protocols | `a2a-sdk`, `mcp` (official SDKs, lockfile-pinned) |
| Runtime | Docker Engine 24+, `docker` SDK; FastAPI in-container |
| Checkpoints | in-memory (dev) → Redis / PostgreSQL (prod) |
| Memory & index | SQLite + sqlite-vec/FTS5 (dev) → PostgreSQL + pgvector (prod) |
| Observability | structlog, prometheus-client |
| Quality | ruff, mypy, pytest (+ testcontainers) |
