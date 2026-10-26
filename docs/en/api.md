# Control Plane API

**[한국어](../ko/api.md)** | English

The Control Plane is the one process that owns everything outside an agent container: the
catalog of declarations, the authoring surface that writes them, the deployments that turn a
graph into running containers, and the runs that drive those containers. The
[Web UI](ui.md) is a client of this API and nothing more — every screen is one of the calls
below.

```bash
python -m aaestro.orchestrator
```

It reads `configs/{AAESTRO_ENV}.yaml`. Which surfaces open depends on that file:

| Surface | Opens when |
|---|---|
| Runs (read, drain) | always |
| Catalog, authoring | always — `registry.roots` resolves against `AAESTRO_REPO_ROOT` |
| Deployments, run submission, resume, steer | `orchestrator.deployment_store` is set |
| Build materials | `orchestrator.material_store` is set — otherwise the routes answer `400` (`CFG_001`) |
| Image builds, and the deploy gate on custom agents | `orchestrator.material_store` **and** `orchestrator.build_store` are set — otherwise the image routes are absent (`404`) |
| Access registry | `orchestrator.access_store` is set — otherwise the routes are absent (`404`) |
| Web UI at `/ui` | always |

Without `orchestrator.deployment_store` the process refuses to start containers and answers
`POST /v1/runs/{id}/resume` and `POST /v1/runs/{id}/steer` with `501` — it reads the run store
but does not drive runs.

## Authentication

**Authentication is on only when `orchestrator.control_token` is set.** With a token, every
`/v1/*` route requires it as a bearer token:

```bash
curl -H "Authorization: Bearer $AAESTRO_CONTROL_TOKEN" http://127.0.0.1:8700/v1/graphs
```

**Without a token the guard is off and every route is open.** That configuration is allowed
only on a loopback bind: the process refuses to start when it binds any other address without
a token (`CFG_001`), and it logs a warning when it starts without one. Set a token for
anything but a single-operator machine.

Two routes are unauthenticated by design even with a token set: `GET /v1/health` (Docker
healthchecks call it) and the static UI under `/ui` (the page holds no data of its own).

A missing or wrong token is `401` — and that response is **not** the error envelope below.
It is FastAPI's own `{"detail": "invalid control plane token"}` with a `WWW-Authenticate`
header, because the check runs as a dependency before any route.

## Errors

Failures raised by the control plane share one envelope, and the `code` is the stable part —
match on it rather than on the message. Two responses do **not** use it: `401` (above) and the
`501` from the run routes (a control plane that drives no runs, or an agent executor that cannot
be steered), which carries a flatter legacy shape.

```json
{
  "error": {
    "category": "not_found",
    "code": "NF_001",
    "message": "unknown graph: no-such-graph",
    "agent": null,
    "task_id": null,
    "retryable": false,
    "details": {"kind": "graph", "name": "no-such-graph"}
  }
}
```

Status codes follow one rule — **who can fix it**:

| Status | Meaning | Typical codes |
|---|---|---|
| `400` | the request is malformed or the declaration is invalid | `VAL_001`, `VAL_002`, `MOD_002`, `CFG_002` |
| `401` | missing or wrong token | — |
| `404` | no such resource | `NF_001` |
| `409` | the request conflicts with current state, and you can resolve it | `GRAPH_006`, `RT_010`, `RT_011`, `RT_012` |
| `500` | the server has to fix it | `RT_001`, `GRAPH_002`, `STOR_003` |
| `503` | not now, but worth retrying | anything with `retryable: true` |

`details` carries the specifics you need to act: which field failed, which version was
expected, which agent is already deployed.

## Health and UI

### `GET /v1/health`

Unauthenticated liveness. `{"status": "ok"}`.

### `GET /` → `GET /ui/`

`/` redirects to the operator UI. See [the UI guide](ui.md).

## Catalog

Read-only views of the declarations on disk. Every listing returns two arrays: `items` for
what parsed, and `problems` for what did not. **A broken file never hides the rest** — one
unparseable manifest would otherwise turn the whole catalog into a `500` with no clue which
file to fix.

Each problem is `{path, code, message, errors, reason}`: `path` is the file (or module version
directory), `errors` lists the offending keys as `{field, problem}` for a schema violation, and
`reason` says where a file could not be read — a repeated key (`duplicate key 'k' at line N`),
a parse failure (`ScannerError at line N, column M`), or a file that is not text
(`not valid UTF-8`). Neither carries the rejected value or a piece of the input; key names are
reported (cut to 64 characters, non-printable characters replaced). A name mismatch carries the
expected name (`located`), not the declared one.

### `GET /v1/agents`, `GET /v1/graphs`, `GET /v1/groups`

```json
{
  "items": [
    {
      "name": "planner",
      "version": "0.4.0",
      "group": "research",
      "description": "질의를 실행 가능한 리서치 계획으로 나누는 에이전트",
      "model": {"provider": "anthropic", "name": "claude-sonnet-5"}
    }
  ],
  "problems": []
}
```

Graph summaries carry `mode`, `goal`, and `nodes` instead of `model`; group summaries carry
`quotas`. A `problem` names the `path`, a `code`, and the message. Declarations (agents,
graphs, groups) report `VAL_002` for both kinds of failure — a schema violation and a name
that does not match its location. Module listings report what the registry raised instead
(`MOD_001`, `MOD_003`).

### `GET /v1/agents/{name}`, `GET /v1/graphs/{name}`, `GET /v1/groups/{name}`

The whole declaration as parsed — the same document the runtime will act on, not the file
text. `404` (`NF_001`) when the name is unknown.

### `GET /v1/modules/{module_type}`

`module_type` is `skillsets`, `promptsets`, `memorysets`, or `skillpacks`. Lists every
published version:

```json
{"items": [{"name": "planner", "versions": ["0.1.0", "0.2.0", "0.3.0"]}], "problems": [], "warnings": []}
```

`problems` are versions that are not published (each with `path`, `code`, `message`).
`warnings` are advisory strings about versions that **are** published — today only skillpacks
produce them (a `SKILL.md` over 500 lines, a frontmatter key outside the standard, a stray file
under `skills/`). A skillpack is validated in full here, so a pack
whose declaration is fine but whose skill breaks the standard appears under `problems`
([modules.md](modules.md#skillpacks)).

### `GET /v1/modules/{module_type}/{name}/{version}`

One module document. Versions are exact — there is no `latest`.

A skillpack goes through the same validation as the listing, so a pack listed under `problems`
answers here with the same `MOD_003` error (`400`) instead of `200`. Its declaration has no
skill list — the directories are the list — so the response adds one from the validated pack:

```json
{"apiVersion": "aaestro/v1", "kind": "Skillpack",
 "metadata": {"name": "docs", "version": "1.0.0"},
 "skills": [{"name": "changelog-entry", "files": 3}, {"name": "doc-review", "files": 3}]}
```

`skills` carries names and file counts only. Frontmatter values such as `description` are
content that came with the skill, and are not returned here.

## Authoring

Writes declarations to disk after validating them. Location is identity: a graph named
`docs-demo` lives at `graphs/docs-demo.yaml`, so the path segment and the declared name must
agree (`VAL_002` otherwise).

Because a name becomes a file path, every route that takes an agent, graph or group name
checks it first: lowercase letters, digits and single hyphens only. Anything else, such as
`..`, a slash or an uppercase letter, is `400` (`VAL_002`) and nothing on disk is read, written
or deleted. The same holds for reads, material routes and image routes.

### `POST /v1/validate`

Validates drafts **without writing anything**. This is what the editor calls on every change.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"graphs": [ ... ], "agents": [ ... ]}' http://127.0.0.1:8700/v1/validate
```

```json
{"ok": false, "findings": [
  {"check": "mode_rules", "code": "GRAPH_001",
   "message": "mission graph must be able to reach END", "graph": "docs-demo"}
]}
```

A node with two edges and no `condition` on either is a finding too (`mode_rules`,
`GRAPH_001`, with `node_id` and `targets`). With no conditional edge beside them, both targets
would run in parallel, and the run would fail because shared state (including the iteration
counter `_iterations`) has no rule for merging two branches. Beside conditional edges, only
the first would ever be taken as the fallback. Parallel branches are not supported yet.

Findings are `200`, not an error status — an invalid draft is a normal answer to "is this
valid yet?". A body that is not a draft at all is `400` (`VAL_002`).

### `PUT /v1/graphs/{name}`, `PUT /v1/agents/{name}`

Validates and saves one declaration. Returns `{"name": ..., "path": ...}`.

Two rules the editor depends on:

- **Changing content requires a version bump.** Saving different content under the same
  version is `400` (`MOD_002`, with `existing` and `proposed` in `details`). Saving identical
  content is idempotent.
- **A declaration in use cannot be replaced.** If a running run or a live deployment
  references it, the save is refused (`VAL_002`, "agent is currently deployed — tear the
  deployment down first"). Tear the deployment down first.

Writes are atomic: the file is written beside its target and renamed, so a crash mid-write
cannot leave a truncated declaration.

### `PUT /v1/declarations`

Saves several declarations **as one unit** — validated together, and rolled back together if
any of them fails. Use it when a change spans files, such as adding a node and the agent it
points at:

```json
{"graphs": {"docs-bundle": { ... }}, "agents": {"echo": { ... }}}
```

```json
{"written": ["/repo/graphs/docs-bundle.yaml", "/repo/agents/echo/manifest.yaml"]}
```

### `DELETE /v1/graphs/{name}`, `DELETE /v1/agents/{name}`

`204` on success — except when an agent's directory still holds files someone put there, which
answers `200` with their names (below). Refused while something references the declaration
(`400`, `VAL_002`), and the `details` differ by reason: an agent still used by saved graphs lists
them in `referenced_by`, while anything currently deployed reports `kind` and `name` instead.

**An agent's directory goes with its manifest, unless someone else put something there.** The
only file the framework writes under `agents/<name>/` is the manifest, so once that is gone the
directory is removed too and `204` means what it says. If the directory still holds files —
something you put there by hand — they are kept, and the answer is `200` naming them instead:

```json
{"retained": ["Dockerfile", "src/agent.py"]}
```

Nothing under `agents/` is ever deleted except the manifest and now-empty directories. Delete
retained files yourself; leaving them means that declaring the same name again brings them back.

**Build materials are not touched.** They live in the material store, bound to their version: if
you declare the same name and version again, you get those materials back, and different
materials under that version are still refused (`MOD_002`). Bump the version to start over, or
clear them with `DELETE /v1/agents/{name}/materials` before deleting the agent.

## Build materials and images

A custom agent is an agent with **build materials**: a `Dockerfile` (optional) and a `src/`
tree. They live in the control plane's material store, keyed by the agent's name and its
declared version, not in the repository. Declarative agents have none and never build: they
run on the base image with their declarations mounted.

Building is an explicit step. Saving materials does not build, and deploying does not build
either — a deploy **refuses** a custom agent whose image for that version is not `built`
(`409`, `RT_012`; see [`POST /v1/deployments`](#post-v1deployments)) — or whose `built` record
names a different tag than the one a deploy would run.

### `GET /v1/agents/{name}/materials`

The materials for the agent's **current** version. An agent without any is not an error:

```json
{"agent": "docs-custom", "version": "0.1.0", "files": {}, "updated_at": ""}
```

`404` (`NF_001`) if the agent is not declared.

### `PUT /v1/agents/{name}/materials`

Stores the materials for the agent's current version. The body maps context-relative paths
to text content:

```bash
curl -X PUT -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"files": {"src/marker.py": "MARK = 1\n"}}' \
  http://127.0.0.1:8700/v1/agents/docs-custom/materials
```

The response has the same shape as `GET`, with `updated_at` stamped. The rules:

- **Paths** are `Dockerfile` or under `src/`, relative, posix-style, and already normalised
  (`src/./a.py` and `src//a.py` are refused, as is anything with `..`). At most 200 files,
  each at most 256 KiB of text. Violations are `400` (`VAL_002`) with the offending `path`
  in `details`.
- **A version's materials are immutable.** Different content under the same version is `400`
  (`MOD_002`); identical content is idempotent. Bump the agent's version, save the agent,
  then save the new materials.
- **Materials of a deployed agent cannot change** (`400`, `VAL_002`).

The `Dockerfile` is checked when you build, not when you save.

### `DELETE /v1/agents/{name}/materials`

`204`. Clears the materials of the current version, so the agent runs as a declarative agent
again. The version stays taken: saving different materials under it afterwards is still
`MOD_002`. Refused while the agent is deployed.

### `POST /v1/agents/{name}/image`

Submits a build and returns at once with `202`:

```json
{"agent": "docs-custom", "version": "0.1.0", "status": "building",
 "image": "aaestro/agent-docs-custom:0.1.0", "error": null, "log": "",
 "updated_at": "2026-09-14T11:02:03.123456+00:00"}
```

The builder assembles a temporary build context from the catalog and the store, bakes it as
`aaestro/agent-<name>:<version>`, and deletes the directory:

```
Dockerfile        # yours, or the skeleton when you saved none
manifest.yaml     # the agent's declaration
modules/          # the module roots
src/              # your materials
```

The skeleton copies those three into `/app` and puts `/app/src` on `PYTHONPATH`, so a manifest
`spec.entrypoint: agent:MyAgent` resolves to `src/agent.py`. A `Dockerfile` of your own must:

- start `FROM aaestro/agent-base:<tag>` — the base carries `agentd`;
- not end as `root` (going up to install and back down is fine);
- `COPY`/`ADD` only from inside the context — no absolute paths, no `..`, no remote URLs.

What is refused before anything is built:

- `404` (`NF_001`) — the agent is not declared.
- `400` (`VAL_002`) — the agent has no materials, or the `Dockerfile` breaks one of the rules
  above.
- `409` (`RT_011`) — a build of this version is already running. Two builds would write the
  same tag, and the one that finished last would win.

A build that starts and then fails is **not** an HTTP error. It ends as `failed` in the
record below. `error` only says which image failed; the cause is in `log`, the tail of
Docker's output:

```
Step 2/2 : RUN exit 3
 ---> Running in 80c5a8ac55e7
The command '/bin/sh -c exit 3' returned a non-zero code: 3
```

### `GET /v1/agents/{name}/image`

The build record for the agent's current version, plus whether it needs one:

```json
{"agent": "docs-custom", "version": "0.1.2", "status": "failed",
 "image": "aaestro/agent-docs-custom:0.1.2", "expected_image": "aaestro/agent-docs-custom:0.1.2",
 "needs_build": true, "error": "image build failed: aaestro/agent-docs-custom:0.1.2",
 "log": "Step 1/2 : FROM aaestro/agent-base:0.1.0\n ... returned a non-zero code: 3",
 "updated_at": "..."}
```

| `status` | Meaning |
|---|---|
| `null` | never built |
| `building` | a build is running — poll again |
| `built` | the image exists; a deploy may use it |
| `failed` | the last build failed — read `error` and `log` |

`image` is the tag the record was built under; `expected_image` is the tag a deploy would run. A
`built` record whose two differ is refused by a deploy. `needs_build` is `false` for an agent
without materials. `log` keeps the last 8000 characters.
Records survive a control plane restart.

## Deployments

A deployment turns one graph into a set of running containers. The base image carries no
declarations, so the runtime mounts the manifest and the module roots read-only and injects
the A2A wiring the graph declares.

### `POST /v1/deployments`

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"graph": "research-pipeline"}' http://127.0.0.1:8700/v1/deployments
```

Optionally pass `model_overrides` (node id → partial `spec.model`) to swap models without editing
any declaration; see [Agents — model resilience](modules.md#agents--model-resilience). agentd runs
the task with that model:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"graph": "research-pipeline", "model_overrides": {"researcher": {"name": "claude-haiku-4-5", "fallbacks": []}}}' \
  http://127.0.0.1:8700/v1/deployments
```

The response echoes the map you sent, only the keys you set — here `"model_overrides": {"researcher": {"name": "claude-haiku-4-5", "fallbacks": []}}` (`{}` for the first example). Promoting a fallback to primary also needs `fallbacks` adjusted —
a fallback equal to the primary is refused.

`201` once every agent reports healthy:

```json
{
  "deployment_id": "dep-2fc94a0b5f24",
  "graph": "research-pipeline",
  "version": "1.0.0",
  "status": "ready",
  "error": null,
  "updated_at": "2026-09-13T11:29:06.391594+00:00",
  "model_overrides": {},
  "agents": [
    {"name": "planner", "replica": 0, "container_id": "0f2edbe1aa5a",
     "image": "aaestro/agent-base:0.1.0", "control_port": 32852, "a2a_port": 9100}
  ]
}
```

The call blocks until the agents are healthy or the deadline passes — a few seconds for the
reference graph. Per-agent tokens are never in a response.

`status` is one of:

| Status | Meaning |
|---|---|
| `starting` | containers are coming up |
| `ready` | every agent passed a health check and accepts tasks |
| `failed` | a step failed; **everything this deployment started was stopped again** |
| `stopped` | torn down through `DELETE` |
| `lost` | after a restart, at least one container was gone |

Failures leave no ghost containers. What you get back tells you which kind it was:

- `404` (`NF_001`) — no such graph.
- `400` (`VAL_001`) — the graph does not validate; nothing was started. This includes the
  `model_providers` check: an unknown provider, a provider missing from
  `runtime.egress_proxy.providers`, or an override leaving the manifest's provider set — the finding
  is in `details.findings`.
- `400` (`VAL_002`) — an agent with build materials declares a `runtime.image` other than the
  tag its build produces (`aaestro/agent-<name>:<version>`). Two places naming two images
  leave no way to tell what runs.
- `400` (`VAL_002`) — `model_overrides` names a node that is not in the graph, or an override
  body is malformed (provider without name, unknown key). Nothing was started.
- `409` (`RT_012`) — an agent with build materials has no `built` image for its version: never
  built, the last build failed, a build is still running, or the record's tag differs from the
  tag a deploy would run. `details` carries `image` (the expected tag), `build_status` and
  `build_error`, plus `recorded_image` when the tag differs. Build it, then deploy again — a
  deploy never builds for you. After the image tag rule changes, rebuild custom agents built
  earlier. Nothing was started and no deployment was recorded. Agents without materials run on
  the base image and are never gated.
- `409` (`RT_010`) — an agent of this graph belongs to another deployment that is `starting`,
  `ready` or `lost`, or to one being started or torn down right now. For this refusal
  `details.agents` names the agents and `details.deployments` the deployments holding them (the
  launcher's own `RT_010` for a single replica carries `details.replica` instead). Tear that
  deployment down first; a graph and its agents deploy as a unit, so two live deployments cannot
  share an agent. A deployment left `starting` by a crashed control plane keeps holding its
  agents until it is torn down. The check runs before anything is touched: the other deployment
  keeps its MCP sidecars and A2A ports, nothing was started and no deployment was recorded.
- `503` (`RT_002`) — an agent never became healthy before the deadline. Marked retryable:
  the containers were rolled back, so a second attempt is safe.

### `GET /v1/deployments`, `GET /v1/deployments/{id}`

The listing is `{"items": [...]}`; a single fetch is the record itself. Each record carries the
`model_overrides` the deployment was created with (`{}` when none). Both reflect the
containers that are actually there right now — if supervision replaced a container after a
crash, the record shows the new id and port.

### `DELETE /v1/deployments/{id}`

Drains each agent, waits for in-flight tasks, then stops and removes the containers. Returns
the record with `status: "stopped"`; the record stays so the history remains readable. A
deployment already `stopped` or `failed` is returned unchanged.
A second request for a deployment that is still being torn down (a retry, a double click)
joins the teardown in progress and gets the same record — the agents are drained and stopped
once. A caller that gives up (client timeout) does not stop the teardown; it runs to the end.

## Runs

A run drives a deployed graph. Because the deployment knows where its agents are, submitting
a run takes a `deployment_id` — never an address.

### `POST /v1/runs`

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"deployment_id": "dep-2fc94a0b5f24", "input": {"query": "..."}}' \
  http://127.0.0.1:8700/v1/runs
```

`202` immediately — a run can take minutes, and holding an HTTP request open for it helps
nobody:

```json
{"run_id": "run-a2bce8e90f40", "graph": "research-pipeline", "mode": "mission",
 "status": "running", "iteration": 0, "failure_streak": 0,
 "drain_requested": false, "updated_at": "..."}
```

`mode` comes from the graph, not from the caller. Optional fields: `run_id` to choose the id,
`mode` to assert the graph's mode (a mismatch is `400`).

Refusals: `404` (`NF_001`) when the deployment is unknown or not `ready`; `400` (`VAL_002`)
when the graph on disk has been edited to a different version than the one deployed —
redeploy before running, or the run would drive containers built from another declaration.

### `GET /v1/runs`, `GET /v1/runs/{run_id}`

The listing takes `?mode=mission|service`. A single run adds two fields once it has finished
**in this process**: `state` (the final graph state) and `error`. After a control plane
restart the record survives but that final state does not — durable state lives in the
checkpointer.

### `POST /v1/runs/{run_id}/drain`

Asks a run to stop after its current iteration and returns immediately with
`drain_requested: true`. The run reaches `stopped` on its own; poll `GET` to see it. Draining
is how a service run is stopped — there is no kill.

### `POST /v1/runs/{run_id}/resume`

Continues a run from where it stopped. What that means differs by mode:

- **Service runs** resume from their last iteration, and only from `halted` — the state a
  service graph reaches after too many consecutive failures (`GRAPH_005`). Any other state is
  `409` (`GRAPH_006`): a run you drained on purpose is submitted again, not resumed.
- **Mission runs** resume from the last checkpoint and carry **no state guard** — the
  checkpointer decides what continuing means, so resuming one that already finished re-drives
  it from that checkpoint. Without a durable checkpointer there is nothing to continue from
  (`STOR_002`).

`501` means this control plane has no deployment surface: it can read runs but does not drive
them, and answering `200` would leave an operator believing a resume happened.

### `POST /v1/runs/{run_id}/steer`

Redirects the task a run is executing **without discarding the work done so far**. Cancelling
and resubmitting starts over; a steer adds one instruction to the conversation the agent is
already in.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"text": "Use the 2024 figures, not 2023.", "by": "alice"}' \
  http://127.0.0.1:8700/v1/runs/run-a2bce8e90f40/steer
```

| Field | |
|---|---|
| `text` | The instruction. Required, at most 4000 characters. |
| `by` | Who issued it, for the audit trail. Defaults to `operator`; at most 200 printable characters. It is the **caller's claim**, not an authenticated identity — the control plane token does not tell people apart. |
| `node` | The node to steer. Omit it to steer the node running right now. |

`202` — the instruction is **queued, not applied**:

```json
{"run_id": "run-a2bce8e90f40", "node": "researcher", "task_id": "…",
 "agent": "researcher", "status": "queued"}
```

**When it takes effect.** The agent adds the instruction at its next *turn boundary* — right
after the tool results of the current turn, as a user turn
(`assistant(tool calls) → tool results → user(steer)`). It never interrupts a model call in
progress, never rewrites a message already sent, and never touches the system prompt (that
would break the task's prompt-cache prefix). Several instructions waiting at the same boundary
become one user turn. Each one is prefixed with `[operator steer]` so the model can tell it
from the task input. On providers where tool results already travel in the `user` role, the
instruction is appended to that same message rather than sent as a second `user` message.

**It may not take effect at all.** If the task ends before another turn boundary — the model
was already writing its final answer, the tool failed, the turn limit was reached, the task
was cancelled — the instruction is dropped. `queued` is not a promise. The agent logs a
`WARN` (`steer dropped, task ended before the next turn boundary`, with `agent`, `task_id`,
`run_id`, and who issued it) and nothing else happens; check the run's output, and resubmit
if the direction mattered. A stream consumer sees a `steer` event at the moment an
instruction enters the conversation, and none if it was dropped. The event says the
instruction was added — the model reads it on its next call, and that call can still fail.

Refusals:

| Status | Code | When |
|---|---|---|
| `404` | `NF_001` | The run is unknown; or it has no task in flight (finished, or between two nodes); or the named `node` is not running; or the agent has not entered its loop yet (the node was dispatched a moment ago — try again); or the task ended just before the instruction arrived. The `message` says which. |
| `400` | `VAL_002` | The body is malformed or has an unknown key; `text` is empty or longer than 4000 characters; `by` is an empty string, longer than 200 characters, or contains control characters or a line break; 8 instructions are already waiting on that task; several nodes are running and `node` was omitted (`running` in the details lists them); or the node has more than one task in flight, which a node name cannot tell apart (`task_ids` lists them). |
| `501` | legacy shape ([Errors](#errors)) | This control plane does not drive the run (see resume above), or the agent's executor does not support steering — a custom `spec.entrypoint` that does not implement it. Never answered as `200`. |

What cannot be steered: only the LLM tool loop has turn boundaries, and a task an agent
delegated to a peer over A2A is not addressable from here — steer the node that delegated.
Refused instructions are logged by the control plane too (`run steer refused`), with who tried. The instruction text is not written to logs (only its length); the `steer` event
on the task stream carries it.

## Access registry

Permissions that change while agents run are decided **outside the agent's container, per
request** — an agent may hold every permission inside its own container, so a check there is
a convenience, not a boundary. The registry is where those decisions are made; the enforcement
points (Memory Service, egress proxy, a callee's A2A server) ask it and cache the answer.

The surface opens when `orchestrator.access_store` is set:

```yaml
orchestrator:
  access_store: ./var/access.db          # identities, grants, revocations — survives restarts
  access_enforcer_token: ${ENFORCER}     # must differ from control_token
  access_agent_url: http://control-plane:8700   # the control plane as agent containers reach it
  access_stewards: [permission-agent]    # the only agents that may grant
```

Binding a non-loopback address with `access_store` but no `access_enforcer_token` refuses to
start (`CFG_001`), for the same reason the control token is required there. `access_store`
without `access_agent_url` also refuses to start (`CFG_001`): agents need it to prove who they
are on A2A calls, and without it they would fall back to a signing key every agent in the graph
holds.

**Agents present credentials over this URL.** Plain `http` is for the private network that only
agents and framework services share — the same trust boundary as the Agent Control API and the
Memory Service. When agent traffic crosses hosts or any network you do not control, terminate TLS
in front of the control plane and use `https`. The URL must be `http(s)://host[:port]` with nothing
else — credentials, paths and queries in it are refused (`CFG_001`).

**Enforcement is being rolled out point by point.** Memory, A2A calls and egress through the proxy
are enforced today — the Memory Service in registry mode, each callee's A2A server and the egress
proxy ask for every request (see [Memory enforcement](#memory-enforcement),
[A2A enforcement](#a2a-enforcement) and [Egress enforcement](#egress-enforcement)). With the proxy
on, agents sit on a network with no external route, so the proxy is the only way out, and remote MCP
tools are decided one by one (see [Remote MCP tools](#remote-mcp-tools)).

### Three callers, three credentials

One token for everyone would hand the enforcement points operator powers, and the permission
agent the ability to read every decision. So each audience authenticates as itself:

| Caller | Credential | Routes |
|---|---|---|
| Operator | control plane token | read records, revoke, lift |
| Permission agent | **its own agent identity** | grant |
| Any agent (A2A) | **its own agent identity** | call tickets, verify a received ticket, change feed |
| Enforcement point | `access_enforcer_token` | decide, identities, change feed |

### Agent identity

Every deployment issues one identity per agent and injects it as
`AAESTRO_ACCESS_CREDENTIAL`. The registry stores only its SHA-256 hash; the value appears in no
API response. Identities survive control plane restarts, are re-injected when supervision
replaces a container, and stop working the moment the deployment is torn down or rolled back.

### How a request is decided

In order, first match wins:

1. an active operator **revocation** covering the request → `deny`
2. the agent's **declaration** allows it → `allow`, `decided_by: "declaration"`
3. an active **grant** covering it → `allow`, `decided_by: <permission agent>`
4. otherwise → `deny`, `decided_by: "default"`

A revocation therefore beats both the declaration and any grant. For memory, revoking with
`mode: "rw"` removes writing only — reading stays.

Step 2 counts only for a resource kind whose enforcement point is in place — today, all four:
`memory`, `a2a`, `egress` and `mcp_tool`. For `a2a` the declaration is the `connections` of the graph
the caller is **currently deployed in**; a graph on disk that is not deployed grants nothing. For
`egress` it is the manifest's `runtime.egress`, the model provider's host and any external MCP
server's host. For `mcp_tool` (`server/tool`) it is every tool of a remote server the manifest
declares, or only its `allowed_tools` when those are listed.
Each kind's declaration check is wired together with the enforcement point that uses it, so the
declaration and its enforcement cannot drift apart. Until then, a declaration contributes
nothing for that kind, and a request with no grant is `deny` with `decided_by: "default"`.

Declarations are read on every decision, so a change takes effect without a restart. The
registry watches the agent manifests, group files and graph files (a graph's `connections` are
the A2A declarations, so revoking a connection is seen at once) and moves its version when one changes,
which makes enforcement points drop cached decisions. It also moves the version once when it
starts, because it cannot know what changed while it was down.

### Expansion ceilings

A permission agent can widen an agent's permissions only inside the **ceiling** the operator
declared for that agent's group or for `global`. The registry enforces it; the permission
agent's judgement is not the limit, because the request it acts on is untrusted input.

```yaml
# groups/research.yaml
spec:
  access:
    ceiling:
      max_ttl_s: 3600                                   # every grant expires, at most this late
      memory:
        - {space: "group:research:knowledge", mode: ro}
      egress: [api.search.example.com]
      mcp_tool: []
      a2a: []
```

A group without a ceiling gets no expansion. When the group's ceiling and `global` both
cover a request, the smaller `max_ttl_s` applies. Memory spaces must be explicit persistent
space ids (`local|group|global:<owner>:<alias>`), and no target may be blank or contain a
wildcard. Changing a ceiling is a declaration change; a permission agent cannot change its own.

### `POST /v1/access/grants` — permission agent

```bash
curl -X POST -H "Authorization: Bearer $AAESTRO_ACCESS_CREDENTIAL" \
  -H 'content-type: application/json' \
  -d '{"agent": "researcher", "kind": "egress", "target": "api.search.example.com",
       "ttl_s": 600, "reason": "fetch sources for run r-12", "requested_by": "researcher"}' \
  http://127.0.0.1:8700/v1/access/grants
```

`kind` is `memory`, `egress`, `mcp_tool` or `a2a`; `mode` (`ro`/`rw`) is required for memory
and must be absent otherwise. A mode that does not fit the kind is `400` (`VAL_002`) on every
access route, including revocations and decisions. `201` returns the record:

```json
{
  "rule_id": "rule-7c1e0a9b2d3f4e5a", "agent": "researcher", "kind": "egress",
  "target": "api.search.example.com", "mode": null, "effect": "allow",
  "decided_by": "permission-agent", "requested_by": "researcher",
  "reason": "fetch sources for run r-12",
  "created_at": 1789381200.0, "expires_at": 1789381800.0, "lifted_at": null
}
```

A refusal is `403` with `ACC_003`, and it is logged and counted
(`aaestro_access_grants_total{op="refuse"}`). It is refused when the caller is not listed in
`access_stewards`, grants to itself, names a memory permission without a mode, exceeds the
ceiling or its `max_ttl_s`, or asks for something an operator revoked — only an operator
undoes a revocation. An unknown or revoked identity is `403` with `ACC_001`; an unknown agent
is `404`.

### The permission agent

Workers do not call the grant route; they ask the **permission agent** over A2A, and it calls the
route with its own identity. The reference agent is `agents/permission-agent`
(`aaestro.access.steward:PermissionAgent`), deployed by `graphs/permissions.yaml`. It decides by
rules only — it never calls the model its manifest declares — so the text of a request cannot talk
it past anything. The ceiling is enforced by the registry either way.

Wiring:

- List it in `orchestrator.access_stewards`. Every agent may call a listed steward: the steward is
  an allowed `a2a` target in every caller's declaration, and when a registry is configured the
  runtime adds each running steward to every agent's peers.
- **Deploy `permissions` before the graphs whose agents will ask.** Agents learn their peers when
  they start; an agent started before the steward was running has no route to it until it is
  redeployed.

The task input is the request itself, or `{"request": "<the same request as a JSON string>"}` —
what `ask_peer` sends:

```json
{"kind": "memory", "target": "global:global:org", "mode": "rw", "ttl_s": 300,
 "reason": "record the findings of run r-12"}
```

Unknown fields are refused, and there is no `agent` field: the grant always goes to the caller the
callee's A2A server verified from its ticket, so an agent cannot ask on another's behalf. A task
with no verified caller — a direct request, a graph node — is refused without asking the registry.
A granted answer is:

```json
{"granted": true, "rule_id": "rule-7c1e0a9b2d3f4e5a", "kind": "memory",
 "target": "global:global:org", "mode": "rw", "expires_at": 1789381500.0}
```

A failure reaches the caller as `A2A_003` with the permission agent's error in
`details.peer_error`:

| `peer_error.code` | When | Retry |
|---|---|---|
| `ACC_003` | the registry refused — its own code is in `details.registry_code` | no |
| `VAL_002` | the request is not a well-formed expansion request | no |
| `ACC_002` | the registry was unreachable | yes |

Repeating a task id returns the first answer instead of recording a second grant; an `ACC_002`
answer is not remembered, so a retry asks again. Stopping the permission agent stops new grants
only — existing grants run to their expiry and operator revocations keep working.

**Addressing a granted memory space.** A space the agent did not declare has no alias for it. Name
it by space id instead — `{"space": "global:global:org"}` in a Memory Service request — and the
Memory Service decides it like any other space.

### `POST /v1/access/decisions` — enforcement point

```json
{"credential": "<the identity presented to the enforcement point>",
 "kind": "egress", "target": "api.search.example.com"}
```

```json
{"agent": "researcher", "decision": "allow", "decided_by": "permission-agent", "version": 42,
 "valid_until": 1789381800.0}
```

`valid_until` is the earliest expiry among the records behind this decision, or `null`.
Expiry does not move the registry version, so an enforcement point must not use a cached
decision past `valid_until` — its own clock tells it when, even while the registry is
unreachable. Memory decisions require `mode`.

An unknown or revoked identity is not an error: it is answered `200` with
`{"agent": null, "decision": "deny", "decided_by": "unknown-identity"}`, so the enforcement
point can cache the refusal like any other answer.

### `POST /v1/access/identities` — enforcement point

```json
{"credential": "<the identity presented to the enforcement point>"}
```

```json
{"agent": "researcher", "version": 42}
```

An enforcement point that must resolve a name before it can ask for a decision — the Memory
Service turns an alias into a space id through the agent's declarations — first learns who is
asking. An unknown or revoked identity is `200` with `"agent": null`, cacheable like a denial.

### `GET /v1/access/changes?after=<version>&wait_s=<seconds>` — enforcement point or agent

Long-poll. Answers `{"version": N}` as soon as the registry version moves past `after`, or when
`wait_s` (at most 30) passes. Every grant, revocation and lift moves the version, and so does
tearing down a deployment (its identities stop working); an enforcement point drops its cache
when it sees a new one. The bearer is `access_enforcer_token` or a live agent identity — a callee
follows the feed to drop cached A2A decisions, and the feed carries only version numbers.

### `GET /v1/access/agents/{name}` — operator

Everything the operator needs to narrow one agent's permissions, in one answer — this is what the
[permissions tab](ui.md) shows:

```json
{
  "agent": "researcher",
  "version": 42,
  "rules": [
    {"rule_id": "rule-7c1e0a9b2d3f4e5a", "agent": "researcher", "kind": "memory",
     "target": "local:researcher:longterm", "mode": "rw", "effect": "deny",
     "decided_by": "operator", "requested_by": "", "reason": "incident 311",
     "created_at": 1789381200.0, "expires_at": null, "lifted_at": null}
  ],
  "declared": [
    {"kind": "memory", "target": "local:researcher:longterm", "mode": "rw"},
    {"kind": "memory", "target": "global:global:org", "mode": "ro"},
    {"kind": "a2a", "target": "planner", "mode": null},
    {"kind": "egress", "target": "api.anthropic.com", "mode": null},
    {"kind": "mcp_tool", "target": "corp/*", "mode": null}
  ],
  "ceilings": [
    {"group": "research", "max_ttl_s": 3600, "memory": [], "egress": ["api.search.example.com"],
     "mcp_tool": [], "a2a": []}
  ],
  "denials": [
    {"kind": "memory", "target": "local:researcher:longterm", "mode": "rw",
     "decided_by": "operator", "at": 1789381260.0}
  ]
}
```

- `rules` — every record, active, expired and lifted. Records are never deleted, so this is the
  history of who decided what and why. `requested_by` is empty for an operator's revocation; a grant
  carries the agent that asked.
- `declared` — what the declarations give the agent today, for each kind whose enforcement point is
  in place. It is built from the same declarations the decisions read, so what is listed is exactly
  what step 2 allows. `server/*` stands for every tool of a remote MCP server without
  `allowed_tools`; it is not a target you can revoke — revoke `server/tool` or the server's host.
- `ceilings` — the expansion ceilings of the agent's group and `global`, by the group that declares
  them.
- `denials` — the last 50 refusals the registry decided for this agent, newest first. They live in
  the control plane's memory and start empty after a restart. A refusal an enforcement point
  answered from its cache is not here.

### `POST /v1/access/revocations` — operator

```json
{"agent": "researcher", "kind": "memory", "target": "group:research:knowledge",
 "mode": "rw", "reason": "incident 311", "expires_in_s": 3600}
```

`201` with the record, `effect: "deny"`, `decided_by: "operator"`. `expires_in_s` is optional;
without it the revocation lasts until lifted.

### `DELETE /v1/access/rules/{rule_id}` — operator

Lifts a revocation or ends a grant early. The record stays, with `lifted_at` set. `404`
(`NF_001`) for an unknown id.

### Memory enforcement

The Memory Service switches to **registry mode** when both of these are set in its environment
(one without the other refuses to start, `CFG_001`):

| Variable | Value |
|---|---|
| `AAESTRO_ACCESS_URL` | the control plane, reachable from the Memory Service |
| `AAESTRO_ACCESS_ENFORCER_TOKEN` | the control plane's `access_enforcer_token` |

In registry mode:

- **No memory tokens are issued.** An agent presents the identity its deployment injected;
  the control plane passes it as `AAESTRO_MEMORY_TOKEN` when a registry is configured, and never
  falls back to a static token.
- **Every request is decided.** The service resolves the alias through the agent's current
  declarations, then asks for `ro` (read, search, latest) or `rw` (append) on that space id.
  A denial is the usual `401` with `MEM_001`, and the audit log records it.
- **A revocation applies to the next request** of a running agent, and so does a `rw`→`ro`
  demotion — reads keep working, writes stop. Nothing restarts.
- **Searching without naming spaces skips spaces that are not allowed now**, rather than failing
  the whole search. `GET /v1/spaces` lists what is allowed, with the mode actually permitted.
- **The identity outlives a Memory Service restart.** It lives in the control plane.
- **Identity is checked before the request body is read.** Without a recognised identity every
  route answers the same `401` with `MEM_001`, whatever the body contains. The only route open
  without identity is `GET /v1/health` (liveness only); the API documentation paths are not served.
  Unknown routes and methods (`404` / `405`) are answered before identity, in the typed error shape.
- **While the registry is unreachable**, decisions already cached keep working and anything not
  yet decided is denied. A cached decision is still dropped at its `valid_until`.

The service follows the change feed while it runs. When the feed is lost but the registry still
answers, cached decisions older than two seconds are asked again.

**Turn both sides on together.** A control plane with `access_store` injects identities as memory
tokens; a Memory Service still in token mode does not know them and refuses every agent. The
reverse — a registry-mode Memory Service with a control plane that has no registry — refuses
every agent too, because nothing issues identities.

### Egress enforcement

The egress proxy (`python -m aaestro.egress`, image `aaestro/egress-proxy`) is the path agents take
to anything outside. It hosts no tools; it decides and forwards.

| Listener | What agents send | Decided as |
|---|---|---|
| CONNECT (`8080`) | HTTPS through `HTTPS_PROXY`, identity as proxy credentials | `egress` on `host[:port]` — `:443` is left out |
| Provider (`8081`) | model API calls to `ANTHROPIC_BASE_URL` / `OPENAI_COMPAT_BASE_URL`, identity in place of the key | `egress` on the provider's logical host (`api.anthropic.com`, `openai-compat.invalid`) |

The proxy never looks inside TLS. For model calls it removes the agent's identity, adds the key it
holds, and streams the provider's answer back, so **the agent's environment never has the model key**
and revoking model access needs no redeploy.

Each provider opens only the paths a model call needs — `POST v1/messages`,
`POST v1/messages/count_tokens`, `GET v1/models` for `anthropic`; `POST v1/chat/completions` for
`openai-compat`. Anything else is refused with `403 ACC_001` before the key is attached, and the
path that was matched is the path that is forwarded. For `openai-compat` the key goes out as
`Authorization: Bearer <key>`; the `Authorization` the agent sent is its identity and is always
removed first.

Request headers are an allowlist per provider: `content-type`, `accept`, `accept-encoding`,
`user-agent`, plus `anthropic-version` and `anthropic-beta` for `anthropic`. Everything else the
agent sets is dropped — a header chosen inside the container must not ride along with the
operator's key. Cookies cross in neither direction, and the proxy keeps none between calls: one
agent's upstream session is never attached to another agent's request.

`openai-compat` is decided under `openai-compat.invalid`, not under the upstream's address: what an
operator revokes is "the model API", wherever this deployment points it. The name is synthetic —
`.invalid` is reserved, and the CONNECT listener refuses any `.invalid` destination with
`403 ACC_001` before asking the registry, so declaring the provider never opens a tunnel.

Turn it on in the control plane:

```yaml
runtime:
  egress_proxy:
    connect_url: http://aaestro-egress:8080
    providers_url: http://aaestro-egress:8081
    container: aaestro-egress   # needed only for MCP sidecars — see below
    providers: [anthropic]      # providers the proxy terminates — add openai-compat when its upstream is set (default [anthropic])
```

Deployment validation refuses a model provider (primary or fallback) that is not in `providers`
(`400 VAL_001`, finding `model_providers`).

Deployments then give each agent `HTTPS_PROXY` (its own identity as credentials),
`ANTHROPIC_BASE_URL` and `OPENAI_COMPAT_BASE_URL` pointing at the proxy, and its identity where
`ANTHROPIC_API_KEY` / `OPENAI_COMPAT_API_KEY` were — the real keys stay with the proxy even if a
manifest lists them in `env_allowlist`. Plain
`http` is not proxied — framework services on the agent network stay direct. `runtime.egress_proxy`
without `orchestrator.access_store` refuses to start (`CFG_001`).

#### Network isolation

A proxy that agents can walk around decides nothing, so **turning the proxy on isolates the agent
network**. `runtime.network` becomes an internal Docker network (`--internal`) with no route out:
inside an agent container, a name outside that network does not resolve and a direct connection to an
outside address fails. The proxy is the only way out.

- **Only the proxy and the framework services join both networks.** Attach the egress proxy, the
  Memory Service, and whatever answers `orchestrator.access_agent_url` to the agent network and to a
  network with a route out. Agents reach them by name on the agent network.
- **An existing network must already be internal.** The control plane creates the network when it
  is missing. It refuses to start an agent on an existing network whose isolation differs (`RT_001`,
  not retryable) instead of reusing it: a network with a route out would let agents bypass the proxy.
  Remove or recreate that network.
- **Reattaching checks isolation too.** After a restart, a deployment is reattached only when the
  agent network is internal and each of its containers is attached to that network alone, and the
  container still carries the hardening the start spec sets (`CapDrop` has `ALL`, a read-only root
  filesystem, user `1000:1000`, the PID limit (`DEFAULT_PID_LIMIT`, 256), and a `security_opt` whose
  effective value per key equals the spec's — adding an entry or overriding one later is refused)
  and nothing that voids it (privileged, added capabilities, host PID / IPC / user / network
  namespaces, an empty `MaskedPaths` or `ReadonlyPaths`). Its mounts must be exactly the ones the
  start spec sets: the read-only declaration mounts, the declared volumes and the `tmpfs` paths. A
  bind the spec did not declare (a host path, `/var/run/docker.sock`), a declared mount made
  writable or pointed elsewhere, and an extra or missing `tmpfs` are refused (`Mounts` / `Tmpfs`;
  the reason names the field, never the path). Paths are compared normalized (`/cache/` is
  `/cache`). The one exception is the anonymous volume Docker creates for a path the image declares
  with `VOLUME`. Every volume, declared or anonymous, must also be a plain `local` volume: one
  recreated under the same name with a `device` or `bind` option opens a host path and is refused,
  and so is a volume that cannot be inspected. Otherwise
  the deployment becomes `lost` ("network isolation mismatch", or "container hardening mismatch"
  with the field names), nothing is attached, and its agent
  identities are revoked, so containers with a route out lose their memory, peer and model access.
  The containers are left running for you to inspect and tear down. On a host that cannot set a PID
  limit (no pids cgroup), reattaching is refused; a check at start time is a follow-up.
  A deployment that cannot be rebuilt from its declarations (a manifest is gone, or a node left the
  graph) also becomes `lost` ("cannot rebuild from declarations") and its identities are revoked.
- **Agents publish no ports.** Ports cannot be published from an internal network, so the control
  plane reaches each agent's Control API at the container's address on that network, and reattaching
  after a restart looks the address up again. The control plane must be able to reach that
  address: run it on the agent network, or on the Linux host that owns the network's bridge.
- **The host is still reachable from agents** at the agent network's gateway address, like any
  bridge network — a host service listening on all interfaces is a way out the proxy never sees.
  Close it with `deployments/docker/isolate-agent-network.sh apply <network>` (root): it drops
  connections agents open to the host and keeps the replies to connections the host opens, so the
  control plane still reaches each agent. See the
  [runbook](runbooks/access-control.md#closing-the-host-gateway). The control plane logs a warning at
  startup when the proxy is on and it binds anything but loopback, because it is then one of those
  services.

Without `runtime.egress_proxy`, agents stay on a regular network with ports published on loopback, as
before.

Declare destinations in the manifest — host or `host:port`, no wildcards, schemes or paths:

```yaml
spec:
  runtime:
    egress: [api.search.example.com, feeds.example.com:8443]
```

The proxy process takes:

| Variable | Meaning |
|---|---|
| `AAESTRO_ACCESS_URL`, `AAESTRO_ACCESS_ENFORCER_TOKEN` | the registry — both required, or it refuses to start |
| `ANTHROPIC_API_KEY` | the key the proxy adds to model calls |
| `AAESTRO_EGRESS_ANTHROPIC_UPSTREAM` | where model calls go (default `https://api.anthropic.com`) |
| `OPENAI_COMPAT_API_KEY` | the key the proxy adds to `openai-compat` calls as `Authorization: Bearer`. A keyless local server still needs a placeholder. A provider key that is set but blank refuses to start (`CFG_001`) — this applies to `ANTHROPIC_API_KEY` too |
| `AAESTRO_EGRESS_OPENAI_COMPAT_UPSTREAM` | where `openai-compat` calls go — **up to, not including, `/v1`**: `https://api.openai.com`, `http://vllm:8000`, `https://gateway.example.com/llm`. No default. Set together with the key or not at all; one without the other, or a value ending in `/v1`, refuses to start (`CFG_001`). The plain-`http` rule below applies |
| `AAESTRO_EGRESS_ALLOW_PLAINTEXT_UPSTREAM` | `true` to accept plain `http` to a destination **also listed** in `AAESTRO_EGRESS_PRIVATE_DESTINATIONS` — for a test double on a private network. Each request resolves the name and connects only to a private address; a listed name that resolves publicly is refused (`502`). A public address is never reached over plain `http`, whatever the settings |
| `AAESTRO_EGRESS_PORT`, `AAESTRO_EGRESS_PROVIDER_PORT` | the CONNECT and provider listeners (default `8080`, `8081`) |
| `AAESTRO_EGRESS_MODE` | `enforce` (default) or `record` |
| `AAESTRO_EGRESS_PRIVATE_DESTINATIONS` | comma-separated targets allowed to resolve to private addresses |

Responses: a denied destination is `403` (`ACC_001`); no decision while the registry is unreachable
and nothing is cached is `503` (`ACC_002`); a missing or unknown identity is `407` on CONNECT and
`401` on the provider listener. A malformed setting refuses to start (`CFG_001`), and so does the proxy
when its registry feed or either listener ends — run it under a restart policy.

**Private addresses.** The proxy sits on the external network, so it can reach what agents cannot —
cloud metadata addresses, services on the host. A destination that resolves to a private, loopback,
link-local or shared (CGNAT) address is refused unless it is listed in
`AAESTRO_EGRESS_PRIVATE_DESTINATIONS`, and the proxy connects to the address it checked, so a name
cannot be re-pointed between the decision and the connection.

**Record mode.** `record` logs a denial (`egress denied but recorded only`) and lets the call
through. Use it to surface undeclared destinations before enforcing. A missing or unknown identity,
or a registry that cannot decide, is refused in both modes — there is no agent to record.

#### Remote MCP tools

The proxy also terminates remote MCP servers (`streamable-http` with a `url`). With
`runtime.egress_proxy` on, agentd connects to `http://<proxy>:8081/mcp/<server>` instead of the declared
URL and presents its identity where the server credential was. For every request the proxy:

1. learns the agent from the identity and looks up **that agent's declaration** for the server's URL and
   `auth.token_env` — the request names only the server, so an agent cannot point the proxy's
   credential at another address;
2. decides `egress` on the server's host, then `mcp_tool` on `server/tool` for every `tools/call`;
3. drops the identity, adds the credential, and connects to the address it checked.

Revoking one tool refuses the next call to it and leaves the server's other tools working:

```json
{"agent": "researcher", "kind": "mcp_tool", "target": "corp/search", "reason": "incident 311"}
```

A refused tool call is answered as a JSON-RPC error (`ACC_001: mcp_tool denied: corp/search`), which
reaches the agent as `MCP_003` with the reason in `details.detail`. A batch containing a refused call is
refused whole (`403`). A server the agent did not declare is `404`.

The proxy needs two more settings for this:

| Variable | Meaning |
|---|---|
| `AAESTRO_REPO_ROOT` | the declarations, mounted read-only — without it, remote MCP calls are `404` |
| `AAESTRO_EGRESS_MCP_TOKENS` | comma-separated credential names the proxy may send to MCP servers, each set in the proxy's environment |

**Only listed credential names are sent.** A declaration that names another secret as
`auth.token_env` gets `502` (`CFG_002`) instead of that value. The list itself may not name secrets the
proxy holds for other purposes — `ANTHROPIC_API_KEY`, `OPENAI_COMPAT_API_KEY` or any `AAESTRO_*` name refuses to start
(`CFG_001`). A remote MCP server must use `https`: plain `http` is refused (`502`) unless
`AAESTRO_EGRESS_ALLOW_PLAINTEXT_UPSTREAM=true` **and** the server is listed in
`AAESTRO_EGRESS_PRIVATE_DESTINATIONS` — the switch alone opens nothing, so a switch left on in a
production config cannot send a credential in the clear to a public host. A server that resolves to a
private address must be listed there too, like any destination.

**Sessions belong to one agent and one server.** Every agent reaches a server with the same proxy
credential, so the server cannot tell their sessions apart. The proxy signs each `mcp-session-id` it
returns with the agent and server it was issued to, and strips and checks the signature on the way in.
A session id used by another agent, for another server, or altered is `404` — what the MCP
specification prescribes for an unknown session. The signing key is derived from the proxy's registry credential, so sessions
survive a proxy restart.

Deployments leave remote MCP credentials out of the agent's environment. Declare them in a scope as
usual (`env_allowlist` and a group or global `secrets` list) so validation can resolve them. A remote
server's `auth.token_env` may not also appear in a stdio server's `env_allowlist` — the manifest is
rejected, since the proxy-held credential would reach a process inside the container.

Not covered: stdio MCP servers run inside the agent container, so their tools are not decided one by
one — their outside effects go through the proxy's destination decisions, and local effects are not
controlled at run time.

**agentd now starts the declared MCP sessions.** The tools are advertised on the card and run from the
first task. A required server that fails stops startup (`MCP_001`), and reload keeps the live sessions.

#### MCP sidecars

A server declared with `sidecar` runs in a container of its own, owned by one agent:

```yaml
spec:
  mcp:
    servers:
      - name: browser
        transport: streamable-http
        sidecar:
          image: mcp/playwright:1.2.0   # a pinned tag
          resources: {cpu: "0.5", memory: 512Mi}
          port: 8000                    # default
          path: /mcp                    # default
        allowed_tools: [navigate, screenshot]
```

The runtime starts it before the agent as `aaestro-<agent>--mcp-<server>` and removes it when the
deployment is torn down or rolled back. The address comes from that name; a sidecar never declares a
URL.

- **A network of its own.** An agent's sidecars sit on an internal network, `aaestro-<agent>--mcp`,
  never on the agent network. With `runtime.egress_proxy`, the only other container on it is the
  proxy: the agent calls `…/mcp/<server>` as it would a remote server, and the proxy decides
  `mcp_tool` for every `tools/call`. Revoking one tool refuses its next call, the server's other tools
  keep working, and nothing restarts. There is no `egress` decision, because a sidecar is not an
  outside destination. Without the proxy, the owning agent's replicas join that network directly, and
  other agents still cannot reach it.
- **The proxy has to be attached.** Set `runtime.egress_proxy.container` to the proxy's container name;
  the runtime attaches it to each sidecar network under the host name of `connect_url`. A graph with a
  sidecar behind the proxy is refused without this setting (`CFG_001`), because joining the agent to its
  sidecar directly would leave the tool decisions to a check inside the agent's container.
- **Hardened like an agent.** Agents and sidecars take the same hardening set from one place
  (`runtime/spec.py`): uid 1000, a read-only root filesystem with a writable `/tmp` (also
  `HOME`), `cap-drop ALL`, `no-new-privileges`, and a PID limit. Networking differs: the agent's
  own control and optional A2A ports bind to loopback, while a sidecar has no port mapping at all.
  Undeclared
  resources default to the agent default (1 CPU, 1Gi), and each sidecar counts toward the group quota
  once, whatever the replica count.
- **Environment.** Only the server's `env_allowlist`, plus the owning agent's `HTTPS_PROXY` when the
  proxy is on, so the sidecar's outside calls are decided per destination under the agent's identity.
  Without the proxy a sidecar has no way out.
- **Plain http, private addresses only.** A sidecar carries no credential (`auth` is rejected for
  sidecars), so the proxy calls it over `http`, and only at an address that resolves privately.
- **Restarts.** Docker restarts a sidecar that exits with an error, up to five times. After a control
  plane restart, reattach brings back a sidecar that is gone or stopped, or that no longer matches
  its start arguments (hardening, networks, mounts), and marks the deployment
  `lost` if it cannot. agentd waits up to about ten seconds for a sidecar that is still starting; a
  remote server is not waited for.

### A2A enforcement

Without a registry, a callee checks an HMAC token signed with a key the runtime gives **every
agent in the graph** — any of them can mint a token claiming to be any other. With a registry,
the callee decides each call itself:

1. The caller asks the registry for a **ticket** for one callee, authenticating with its own
   identity. The ticket lives five minutes and is reused until thirty seconds before it expires.
2. The caller sends the ticket in `x-aaestro-a2a-ticket`. Its identity never leaves for the callee.
3. The callee's A2A server sends the ticket to the registry, authenticating with **its own**
   identity. The registry refuses a ticket issued for another callee, an expired one, and one
   whose caller identity was revoked; otherwise it decides `a2a` for (caller, callee).
4. A denial — or no decision while the registry is unreachable and nothing is cached — is
   `A2A_004`, before the task reaches the agent. The callee caches decisions, follows the change
   feed with its own identity, and never keeps one past the ticket's expiry.

What this gives you:

- **Revoking a connection applies to the next call** of a running caller; lifting it restores
  calls without a redeploy. Nothing restarts.
- **Skipping the caller's own check does not help.** A caller that calls a port directly with an
  edge token, with another agent's name, or without a ticket is refused by the callee.
- **A ticket is useless elsewhere.** It is not an identity: the Memory Service does not accept it,
  and another agent rejects it because it names a different callee.

The caller still checks the declared connections first — it spares a round trip and gives a clear
`A2A_004` locally — but that check is a convenience, not the boundary.

With or without a registry, an agent's A2A port answers two things only: the agent card
(`GET /.well-known/agent-card.json`, readable without a credential) and JSON-RPC on `POST /`.

- **Only new tasks are taken.** `SendMessage` and `SendStreamingMessage` are the only requests
  served, and the caller is checked before a task is created — a request without a valid token or
  ticket costs the callee no task and never reaches the agent.
- **Tasks cannot be read back or cancelled.** `GetTask`, `ListTasks`, `SubscribeToTask`,
  `CancelTask`, the push-notification config requests and `GetExtendedAgentCard` are refused with
  "This operation is not supported" (`-32004`). The answer is the same for every caller and for
  any task id, so it does not tell whether a task exists.
- **A message cannot be addressed to an existing task.** A send that names a `taskId` is refused
  the same way; every delegation is a new task.
- **The task store keeps no finished task.** It holds a task only while it runs; once the task
  completes, fails or is refused, the store drops it — the result travels in the response and
  nowhere else. Known limit: a task whose agent run ends by cancellation can stay in the SDK's
  internal registry of active tasks (SDK behaviour, tracked separately).

### `POST /v1/access/a2a/tickets` — any agent

```json
{"callee": "planner"}
```

`201` with `{"ticket": "...", "callee": "planner", "expires_at": 1789381500.0}`. A ticket is proof
of identity, not permission: it is issued even for a revoked connection, and the callee refuses
the call. An unknown or revoked identity is `403` (`ACC_001`); an unknown callee is `404`.

### `POST /v1/access/a2a/verify` — the callee

```json
{"ticket": "<the ticket the caller sent>"}
```

```json
{"agent": "researcher", "decision": "allow", "decided_by": "declaration", "version": 42,
 "valid_until": 1789381500.0}
```

A ticket that is not for this callee, expired, forged, or from a revoked caller is `200` with
`"agent": null, "decision": "deny", "decided_by": "invalid-ticket"`. The bearer must be a live
agent identity — `403` (`ACC_001`) otherwise.

## Operational notes

- **One agent, one deployment.** Two graphs that share an agent cannot be deployed at the
  same time (`409`, `RT_010`).
- **Editing is blocked while deployed.** This is deliberate: the containers were built from a
  specific declaration, and letting the file drift would make the deployment unreproducible.
- **Modifying a live system** means: deploy the new version, then tear the old one down.
  In-place patching of a running deployment is not supported.
- **Restarts reattach.** On startup the control plane matches its records against Docker and
  picks the containers back up, health monitoring included. Records whose containers are gone
  become `lost` rather than being deleted quietly.

## CLI equivalents

| API | CLI |
|---|---|
| `POST /v1/runs` | `aaestro run --deployment <id> --input '{...}'` |
| `GET /v1/runs` | `aaestro run-list [--mode service]` |
| `POST /v1/runs/{id}/drain` | `aaestro run-drain <id>` |
| `POST /v1/runs/{id}/resume` | `aaestro run-resume <id>` |
| `POST /v1/runs/{id}/steer` | `aaestro run-steer <id> "<text>" [--node <node>] [--by <who>]` |
| `PUT /v1/agents/{name}/materials` | `aaestro agent-push <name> <directory>` |
| `POST` + `GET /v1/agents/{name}/image` | `aaestro agent-build <name> [--wait]` |

These commands take `--control-url` and `--control-token` (or `AAESTRO_CONTROL_TOKEN`).
`agent-push` reads a directory, skips cache directories such as `__pycache__`, and refuses
symbolic links. `agent-build --wait` exits non-zero when the build fails and prints the log
tail.

`aaestro validate` is deliberately absent from that table: it is a **local** command that
reads the repository directly and takes no control-plane flags. `POST /v1/validate` is the
remote equivalent, and it validates a draft you have not saved. See the
[root README](../../README.md#commands) for the full command reference.
