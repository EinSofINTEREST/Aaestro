# Web UI

**[한국어](../ko/ui.md)** | English

The Control Plane serves an operator UI at `/ui`. It exists so that assembling, deploying,
and running an agent system is a thing you do in a browser rather than a sequence of `curl`
calls. There is no build step and no second process — the page ships with the package and
talks to the [Control Plane API](api.md) over `fetch`, nothing else. It never touches the
filesystem or Docker; anything the UI can do, the API can do.

```bash
python -m aaestro.orchestrator      # serves both the API and the UI
```

Open `http://127.0.0.1:8700/` — `/` redirects to `/ui/`.

## Connecting

The page loads without a token. Whether it can read anything without one depends on the
control plane: when `orchestrator.control_token` is set, paste it into the **토큰** field and
press **연결**; when no token is configured (allowed only on a loopback bind) the API is open
and the page loads its data straight away. The indicator next to the field reports the
result:

| Indicator | Meaning |
|---|---|
| 미연결 | no token has been submitted yet |
| 연결됨 | the token works; catalog, deployments, and runs have loaded |
| 토큰 거부 | the token is missing or wrong (`401`) |
| 연결 실패 | the control plane is unreachable |

The token is kept in `sessionStorage`, so it survives a reload and disappears when the tab
closes. It is never written to disk.

## The six tabs

### 카탈로그 — what you can build with

Four lists: agents, graphs, groups, and modules. Selecting an entry shows the parsed
declaration in full.

Declarations that failed to parse appear in a separate list at the bottom with their file
path and error code, rather than being silently missing. That distinction matters: an empty
agent list means "none declared", while a problem entry means "this file needs fixing".

### 그래프 편집기 — wiring

A form, not a canvas. The metadata block covers name, version, description, mode, goal, and
the state — its fields as JSON (`{"query": {"type": "string", "required": true}}`), or the
deprecated schema reference; three tables below it hold the moving parts:

- **노드** — an id plus the agent it binds to. The agent comes from a dropdown of the
  catalog, so a node cannot point at something that does not exist.
- **엣지** — `from`, `to`, an optional condition (an expression such as
  `state.needs_research`, see [modules.md](modules.md)), and `max_iterations` for cycles.
- **connections** — the A2A allowlist. Declaring `caller`/`callee` is what permits one agent
  to delegate to another at runtime; without it the call is refused.

Choosing `service` mode reveals the idle policy fields — a service graph without backoff
would spin the model in a busy loop, so the fields are required for that mode.

**검증** sends the draft to `POST /v1/validate` and lists every finding with its check name
and code. **저장** validates first and refuses to write if anything fails, so an invalid
graph never reaches disk through the UI.

**카탈로그에서 불러오기** loads an existing graph and bumps its patch version, because saving
changed content under the same version is refused — the version is how a deployment stays
reproducible.

### 에이전트 편집기 — manifests

The same shape for agent manifests: metadata and group, the model, module references, and the
runtime block (image, entrypoint, `env_allowlist`, whether A2A is exposed).

Module references are dropdowns built from the catalog's published versions. There is no free
text and no `latest` — every reference is pinned to a version that exists.

#### 빌드 재료 — custom agents

Below the manifest form is the material editor. It works on the **saved** agent named in the
form, at its current version, so save the agent first.

1. **재료 불러오기** loads what the store holds for that version and shows the build status.
2. **+ 파일** adds a row: a path and its content. Paths must be `Dockerfile` or under `src/`.
   A path that breaks the rule is listed in red **as you type**, and **재료 저장** refuses to
   send anything while one is listed.
3. **재료 저장** stores the rows. Leave the `Dockerfile` out to build on the skeleton, which
   puts `src/` on the import path — set **entrypoint** to `agent:MyAgent` to run a class from
   `src/agent.py`. Leave **image** empty: a custom agent runs as `aaestro/agent-<name>:<version>`,
   and naming a different image makes deploys refuse it.
4. **빌드** submits the build and follows it until it ends:

| Status line | Meaning |
|---|---|
| 재료 없음 | no materials — a declarative agent, nothing to build |
| 아직 굽지 않음 | materials saved, never built |
| building | the build is running |
| built | the image exists and deploys may use it |
| failed | Docker's output tail opens below the line — the cause is at its end |

A version's materials cannot change once saved. To fix a failed build, bump the version, save
the agent, then save and build the new materials. When a graph already references the agent,
change both together through `PUT /v1/declarations`, because saving either one alone breaks
the reference.

### 배포 — containers

Pick a graph, press **배포**, and the row appears with each agent's container id, ports, and
the deployment status (`starting` → `ready`, or `failed` with the reason). **해체** drains
and stops the containers; the record stays visible as `stopped`.

Picking a graph also checks its agents' builds. If one of them has materials but no `built`
image for its version, the tab lists it under the form and disables **배포** until it is
built. The control plane enforces the same rule on its own (`409`, `RT_012`); the tab just
shows it before you press the button.

Two refusals show up here often, and both are the system protecting a live deployment:

- Deploying a graph whose agents already run under another deployment is refused (`409`).
  Tear that deployment down first.
- Saving an agent or graph that a live deployment uses is refused in the editors. To change a
  deployed system: deploy the new version, then tear the old one down.

### Run — driving it

Pick a ready deployment, give the initial state as JSON, and submit. The submission returns
immediately with a `run_id`; the table polls until the run finishes and then shows the final
state.

Rows carry the controls that apply to their state: **보기** for the full record, **drain**
for a running service run (it stops after the current iteration), and **재개** for a run that
is `halted` or `failed`. What resuming means depends on the mode — a service run continues
from its last iteration, a mission run from its last checkpoint. See the
[API reference](api.md#post-v1runsrun_idresume) for the exact contract.

### 권한 — who may do what

Open only when the control plane runs the access registry (`orchestrator.access_store`); otherwise
the tab says the registry is off. Pick an agent to see four things:

| Section | What it shows |
|---|---|
| 선언 권한 | what the manifest, the agent's group, `global`, and the graphs it is deployed in give it — memory spaces with their mode, A2A callees, egress destinations, remote MCP tools |
| 부여·회수 기록 | every revocation and grant, with who decided (`operator` or a permission agent), who asked, why, until when, and whether it is `active`, `expired` or `lifted` |
| 확장 상한 | the most a permission agent may grant this agent, from its group and `global` |
| 최근 거부 | the latest refusals the registry decided for this agent |

**Revoking.** Write a reason (it is recorded) and press the button on a declared permission.
A writable memory space offers two: **쓰기 회수** stops writing and keeps reading, **전체 회수**
stops both. Every other permission has one **회수**. The revocation applies to the agent's **next
request** — the container is not restarted or redeployed.

**Undoing.** An active record has a button: **되돌리기** lifts a revocation, **끝내기** ends a grant
early. The record stays, marked `lifted`.

Widening is not here. Grants come from a permission agent within the ceiling, and the ceiling is
changed in the group declaration. For an emergency, see the
[access control runbook](runbooks/access-control.md).

## The full loop

Everything the main goal asks for is these tabs in order:

1. **카탈로그** — see which agents and modules exist.
2. **에이전트 편집기** — add or adjust an agent; save. For a custom agent, save its build
   materials and press **빌드** until it reads `built`.
3. **그래프 편집기** — place the nodes, wire the edges, declare the A2A connections; validate
   until clean; save.
4. **배포** — deploy the graph and watch the agents turn healthy.
5. **Run** — submit input, watch the result.
6. **권한** — while it runs, narrow what an agent may reach: revoke a permission and the agent's
   next request is refused, with no restart.

Destroying is the same path in reverse: tear the deployment down, then delete the graph or
agent (deletion is refused while anything still references it).

## What the UI deliberately does not do

- **No drag-and-drop canvas.** Wiring is a table. A canvas needs a frontend toolchain, which
  is a dependency decision that has not been made yet (issue #252).
- **No in-place edit of a running system.** Every change goes through save → deploy → tear
  down, so what runs always matches a declaration on disk.
- **No secrets.** The UI never shows agent tokens or secret values. Secrets reach containers
  from the control plane's environment through `env_allowlist`.

## Troubleshooting

| Symptom | Cause |
|---|---|
| 토큰 거부 after entering a token | it does not match `orchestrator.control_token` |
| 배포 tab shows nothing and deploy fails | `orchestrator.deployment_store` is not configured, so the deployment surface is closed |
| Save refused with a version message | the content changed but the version did not — bump it |
| Save refused with "currently deployed" | tear the deployment down first |
| A run ends immediately with `GRAPH_002` | an agent could not run the node; open its container logs |
| 배포 is disabled with a build warning | an agent in the graph has materials but no `built` image — build it in the agent editor |
| **빌드** answers "agent has no build materials" | save materials first; declarative agents do not build |
| Material save refused with a version message | that version's materials are immutable — bump the agent's version |
| No build status and **빌드** fails with `404` | `orchestrator.material_store` or `orchestrator.build_store` is not configured |
| 권한 says the registry is off | `orchestrator.access_store` is not configured |
| A revocation shows `active` but the agent still acts | the enforcement point cannot reach the registry and keeps its cached allow — see the [runbook](runbooks/access-control.md#when-the-registry-is-unreachable) |

For the exact status codes and payloads behind each screen, see the
[Control Plane API](api.md).
