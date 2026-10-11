# Module System

**[한국어](../ko/modules.md)** | English

Everything deployable in Malkuth is a module: a solution is assembled by wiring versioned
modules, never by editing framework code. Normative rules:
[04-module-system.md](../../.claude/rules/04-module-system.md) and
[09-memory-context.md](../../.claude/rules/09-memory-context.md).

## Module Types

| Type | Declares | Location |
|---|---|---|
| **Skillset** | Python tools (capabilities) | `modules/skillsets/{name}/{version}/` |
| **Promptset** | Jinja2 templates + variable schemas | `modules/promptsets/{name}/{version}/` |
| **Memoryset** | Memory policy — scope, index, retention, recall | `modules/memorysets/{name}/{version}/` |
| **Agent** | Contract — model, modules, protocols, resources | `agents/{name}/manifest.yaml` |
| **Graph** | Goal — nodes, edges, connections, mode | `graphs/{name}.yaml` |
| **Group** | Resource scope boundary — quotas, secrets, memory | `groups/{name}.yaml` |

## Reference Format

```
{type}/{name}@{version}     e.g. skillsets/web-search@0.2.0
```

- Always an exact semver — `latest`, branches, and commit hashes are forbidden
- The registry resolves references; paths are never hardcoded
- Published version directories are immutable — changes ship as new versions

## Skillsets

A skill is an async Python function; its tool schema is generated from the signature and
docstring — no hand-written JSON schema.

```python
@skill
async def search(
    ctx: SkillContext, query: str, depth: Literal["quick", "deep"] = "quick"
) -> list[dict]:
    """Run a web search and return the top results.

    Args:
        query: What to search for
        depth: How far to follow result links
    """
    ...
```

- The docstring's first paragraph becomes the tool description; each `Args:` entry becomes
  that parameter's `description`. Undocumented parameters reach the model with a type only.
- Types go through pydantic, the same rules as every other contract: `Literal` and `StrEnum`
  become `enum`, pydantic models and `list[Model]` become object schemas. Nested models are
  collected under a top-level `$defs`, so `#/$defs/...` references resolve.

Key rules: async-first, access secrets/logging via `SkillContext` only, timeouts declared
in `skillset.yaml`, failures raised as exceptions (converted at the agentd boundary).

**Import `SkillContext` at runtime, not under `TYPE_CHECKING`.** The tool schema is
derived from your signature via `get_type_hints()`. If a name in the annotations cannot
be resolved at runtime, that call raises and the framework falls back to *no* schema —
the model then sees untyped parameters and cannot tell what to pass:

```python
# Wrong — get_type_hints() raises NameError, every parameter loses its type
if TYPE_CHECKING:
    from malkuth.core.skill import SkillContext

# Right
from malkuth.core.skill import SkillContext, skill
```

The framework warns (`skill type hints could not be resolved` /
`skill parameters have no type`) rather than failing, so dynamically defined skills stay
possible. `LoadedSkillset.untyped_parameters()` reports the same thing per tool.

## Promptsets

Templates are selected by graph `node_id` (or `default` for direct requests). Variables
are declared with schemas — rendering with undeclared variables fails (`MOD_004`) rather
than silently producing empty text. Locale overrides live in `locales/{lang}/`.
Any prompt wording change requires a version bump.

## Memorysets

A memoryset pins the policy of a memory space: scope (`run | local | group | global`),
embedding model (fixed per version), chunking, hybrid search weights, retention/compaction,
and recall defaults (k, min score, token budget). Attachment location must match the
scope: manifest (local), graph (run), `groups/<name>.yaml` (group),
`groups/global.yaml` (global).

## Graphs

The graph is the wiring module: attaching or detaching an agent is a YAML change only.

- `mode: mission` — terminates at END; cycles require `max_iterations`
- `mode: service` — perpetual; requires an idle backoff policy, checkpoints per iteration
- `connections` — the A2A peer-call allowlist (direction matters, peers stay equal)
- Deploy-time validation rejects dangling refs, unreachable nodes, and mode violations
- A node leaves by **one path at a time**: it may have at most one edge without a `condition`.
  Two unconditional edges would run both targets in parallel, and shared state (including the
  iteration counter `_iterations`) has no rule for merging two branches, so the run would fail
  midway. Give the edges conditions instead; one unconditional edge beside conditional ones is
  the fallback route

### State and conditions in the YAML

A new graph needs no code. Declare the state fields and write conditions as expressions:

```yaml
spec:
  state:
    fields:
      query: {type: string, required: true}
      plan: {type: string}
      needs_research: {type: boolean, default: true}
      findings: {type: array}
  edges:
    - {from: planner, to: researcher, condition: state.needs_research}
    - {from: planner, to: END, condition: not state.needs_research}
```

- **Field types**: `string`, `integer`, `number`, `boolean`, `array`, `object`. A field is
  optional unless `required: true`; an optional field without a `default` starts empty
  (`array`, `object`) or null. Names starting with `_` are reserved for the framework.
- **Conditions** read the state and nothing else: `state.<field>` (with `.key` for nested
  values), literals (`true`, `false`, `null`, numbers, strings, `[...]`), comparisons
  (`==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not in`), and `and` / `or` / `not`. A bare value
  is read as true or false — an empty list, `0` and `null` are false. Function calls and
  anything outside `state` are refused when the graph is read (`GRAPH_001`); comparing values
  that cannot be compared, such as `null < 3`, fails the run (`GRAPH_003`) rather than
  silently taking the other branch.
- **Deprecated**: `state.schema` and conditions written as import references
  (`malkuth.graphs.conditions:needs_research`) still work for one version. They may only
  point into `malkuth.graphs`, and validation logs a warning for each import-reference
  condition.

## Agents — model resilience

An agent's `spec.model` may declare what to do when the primary model fails. Every field below is
optional; leave them out and behavior is unchanged. This is **declared now, acted on by later
work**: validation and resolution ship first, the failover loop, key rotation and non-Anthropic
adapters follow (#359, #360, #361).

```yaml
spec:
  model:
    provider: anthropic
    name: claude-sonnet-5
    max_tokens: 8192
    effort: high
    fallbacks:                      # tried in order when the primary fails for a reason in `on`
      - {provider: anthropic, name: claude-haiku-4-5, on: [rate_limit, server_error]}
    retry:                          # per-category retry; omitted = framework defaults
      rate_limit: {start_s: 10, attempts: 5}        # optional max_s (>= start_s)
      network:    {start_s: 1,  attempts: 3}
    circuit: {max_failures: 5, reset_s: 60}
```

Only providers the framework knows (currently `anthropic`) validate; a second provider arrives
with the OpenAI-compatible adapter (#361).

- `on` takes `rate_limit`, `server_error`, `context_overflow`; it is required and cannot be empty.
- `max_tokens`, when set, must be a positive integer.
- A fallback's `(provider, name)` must differ from the primary and from every other fallback.
  Unknown keys are refused.

### Resolution — the nearest layer wins

Same rule as resource scoping (local > group > global). Later layers override earlier ones:

1. the agent manifest `spec.model` (the default)
2. a graph node's `nodes[].model` — the same agent can use a different model or fallbacks per node
3. `model_overrides` on `POST /v1/deployments` (keyed by node id) — an operator swaps models
   without editing any declaration file

An override is **partial**: only the keys it names replace the layer below. An explicit empty
value — `fallbacks: []`, `circuit: null` — erases the lower layer's value. Naming `provider`
requires naming `name` too (a model name belongs to its provider). Promoting a fallback to primary
also needs `fallbacks` adjusted — a fallback equal to the primary is refused. Subgraph nodes cannot carry
`model`.

```yaml
# graphs/research-pipeline.yaml (excerpt)
nodes:
  - id: researcher
    agent: agents/researcher@0.1.0
    model:
      name: claude-opus-5
      fallbacks: []                # no fallback on this node
```

The resolved result is delivered to agentd as `TaskRequest.config.model`. **Switching agentd's
calls to that model arrives with the failover-loop work (#359)** — until then node and deployment
overrides are recorded, validated and delivered, but the actual call uses the manifest's model.
Direct requests (outside any graph) use the manifest defaults.

### Deploy-time checks (`model_providers`)

The check is named `model_providers`; its findings carry code `VAL_002`, and the HTTP response is
`400 VAL_001` with the findings in `details.findings`. If any check fails, nothing is started:

- the provider must be one the framework knows (one with a logical host)
- with an egress proxy, the provider must appear in `runtime.egress_proxy.providers` — otherwise a
  fallback would hit a proxy 404 at runtime, silently
- node and deployment overrides may change the provider only **within the set the manifest
  declares** (primary + fallbacks): egress decisions derive from the manifest, so a provider
  outside it would be refused by the proxy. To switch to another provider, declare it in the
  manifest's `fallbacks` first
- `model_overrides` naming a node id that is not in the graph, or a malformed override body
  (provider without name, unknown key), is a direct `400 VAL_002`
- the egress baseline includes the fallback providers' hosts automatically (a manifest
  declaration is the default permission)

`malkuth validate` and `malkuth deploy` print a `models` entry showing, per node, the resolved
model layering.

## Groups

A group scopes resources — quotas, secrets, group memory — for its member agents.
It never affects wiring: same-group agents still need declared `connections` to call
each other. Resource lookup resolves **local > group > global**.

## Compatibility & Versioning

- Agents pin exact module versions; deploy validation cross-checks requirements
  (e.g. skillset `requires.env` ⊆ agent `env_allowlist`)
- Breaking-change guide (semver): tool rename/signature change/removal and required
  prompt-variable changes → **major**; backward-compatible additions → minor;
  graph state schema changes → major; embedding model changes → minor+ with full reindex
