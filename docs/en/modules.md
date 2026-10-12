# Module System

**[한국어](../ko/modules.md)** | English

Everything deployable in Aaestro is a module: a solution is assembled by wiring versioned
modules, never by editing framework code. Normative rules:
[04-module-system.md](../../.claude/rules/04-module-system.md) and
[09-memory-context.md](../../.claude/rules/09-memory-context.md).

## Module Types

| Type | Declares | Location |
|---|---|---|
| **Skillset** | Python tools (capabilities) | `modules/skillsets/{name}/{version}/` |
| **Promptset** | Jinja2 templates + variable schemas | `modules/promptsets/{name}/{version}/` |
| **Memoryset** | Memory policy — scope, index, retention, recall | `modules/memorysets/{name}/{version}/` |
| **Skillpack** | Agent Skills directories (`SKILL.md` + files), unmodified | `modules/skillpacks/{name}/{version}/` |
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
    from aaestro.core.skill import SkillContext

# Right
from aaestro.core.skill import SkillContext, skill
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

## Skillpacks

A skillpack carries [Agent Skills](https://agentskills.io/specification) directories
**unmodified**: each skill is a directory with a `SKILL.md` (YAML frontmatter + Markdown
instructions) and optional `scripts/`, `references/`, `assets/`. A skillpack does not replace
a skillset — they are different things:

| | Skillset | Skillpack |
|---|---|---|
| What it is | Python tools the framework imports and registers | Instructions a model reads, plus the files they point to |
| Format | Aaestro-specific (`skillset.yaml` + `@skill` functions) | The Agent Skills standard, as published |
| Loading | Code is imported inside the owning agent's container | Files are read and validated — **nothing is imported or run** |
| What the model sees | A tool schema generated from the signature | `name` + `description` up front, the body on activation (#374) |
| Manifest field | `spec.skillsets` | `spec.skillpacks` |

```
modules/skillpacks/docs/
└── 1.0.0/
    ├── skillpack.yaml            # apiVersion / kind: Skillpack / metadata — no skill list
    └── skills/
        ├── changelog-entry/      # directory name == frontmatter `name`
        │   ├── SKILL.md
        │   ├── scripts/
        │   └── references/
        └── doc-review/
            ├── SKILL.md
            ├── references/
            └── assets/
```

`skillpack.yaml` does not list the skills: the directories under `skills/` are the list. Drop
a standard skill directory in and it is part of the pack.

```yaml
# agents/<name>/manifest.yaml
spec:
  skillpacks:
    - ref: skillpacks/docs@1.0.0
```

An agent declares a pack once: the same ref twice, or two versions of the same pack, is
rejected when the manifest is validated.

### Validation

A pack that breaks any rule below is not published: it is listed under `problems` by
`GET /v1/modules/skillpacks`, the single-pack lookup returns the same error, `aaestro validate`
fails, and an agent referencing it fails deploy validation. Violations are `MOD_003` and name
the skill and the field
(`invalid skill 'doc-review' in skillpacks/docs@1.0.0: name: does not match its directory name`).

| Field | Rule |
|---|---|
| `name` | required string; 1–64 characters; `a-z`, `0-9`, `-` only; no leading, trailing, or consecutive hyphens; **equals the directory name** |
| `description` | required string; 1–1024 characters; not blank |
| `license` | optional string |
| `compatibility` | optional string; 1–500 characters; not blank |
| `metadata` | optional map of string keys to values, at most 64 entries. Scalar values are taken **as written** — `version: 1.0` becomes `"1.0"`, `1.10` stays `"1.10"`, `true` becomes `"true"`. Lists, maps, and empty values are rejected |
| `allowed-tools` | optional string — experimental in the standard, checked for type only and not interpreted |

- `SKILL.md` must be a regular file (not a link) and start with a `---` line; the frontmatter
  ends at the **first** following `---` line and may be at most 16 KiB — larger frontmatter is
  rejected before it is parsed. A BOM and CRLF line endings are accepted; an empty body is
  accepted.
- Error messages carry field names and reasons, never frontmatter values or body text.

**Warnings** do not reject a pack. It is published, and the warnings appear under `warnings`
(API) and in `aaestro validate` / `aaestro deploy` output:

- a `SKILL.md` longer than 500 lines (the standard's recommendation)
- a top-level frontmatter key the standard does not define (`version`, `argument-hint`, …) —
  many published skills carry them; the key is ignored and named in the warning
- a regular file directly under `skills/` (`README.md`, `.DS_Store`, `.gitkeep`) — skipped

**Not accepted**, deliberately — a skill that does this needs fixing before it can be packed:

| Rejected | Why |
|---|---|
| `name: 123`, `description: 123` (not strings) | only `metadata` values are converted; a name that YAML reads as a number is a mistake, and the directory would have to match it |
| a non-ASCII `name` (`démo`) | the standard's own examples and character list are `a-z`, `0-9`, `-`; the name is also a directory name and a ref component |
| `allowed-tools` as a YAML list | the standard defines a space-separated string |
| a `SKILL.md` that does not start with `---` | the standard requires frontmatter; without `name` and `description` nothing can select the skill |
| a `SKILL.md` that is a symbolic link | it is read through a file descriptor so that what is checked is what is read (see below) |
| hard-linked files | there is no way to tell what else they are a name for |
| a broken pack no agent references | published packs are validated as a whole, like every other module type — otherwise the breakage surfaces the day someone references it |

### Containment and limits

A skill directory may come from outside, so its file names and link targets are untrusted
input. Nothing inside a skill may point outside that skill's directory:

- **Symbolic links** must be relative and stay inside the skill root at every step. A link is
  rejected when it is absolute (even pointing inside), leaves the root and comes back, passes
  through a link that leaves or through something that is not a directory, dangles, loops, or
  names a special file.
- **Directory links that lead back to themselves** are rejected even though they stay inside:
  a link to its own ancestor (`self -> .`, `up -> ..`), or directory links that reach each
  other in a cycle. Copying or mounting the tree while following links would never finish.
- `SKILL.md` must not be a link at all. `skills/` and each skill directory must be real
  directories, not links.
- **Hard-linked files** are rejected — there is no way to tell what else they are a name for.
- **Special files** (FIFO, socket, device) are rejected.
- **File names** with control or format characters, a backslash, or undecodable bytes are
  rejected.
- **Limits**, per skill: 200 entries, 1 MiB per file, 8 MiB in total, 8 directory levels,
  16 KiB of frontmatter, 64 `metadata` entries. At most 50 entries under `skills/`. Limits are
  checked before the expensive step: a directory is read one entry past its limit, never in
  full, and frontmatter is measured before it is parsed.

**What this guarantees, exactly.** Validation describes the tree **as it was when it was
loaded**. `SKILL.md` — the one file whose content is read — is opened through directory
descriptors without following links, so a file swapped for a link between the check and the
read is refused, not followed. Everything else is only `lstat`ed. Nothing here protects against
someone who can modify the module tree **after** validation; published version directories are
immutable by rule, not by enforcement. Whatever later copies or mounts a pack into a container
(#374) must validate the tree it actually copies.

### `scripts/` is not code inside the trust boundary

Loading a skillpack — in the registry, the catalog, `aaestro validate`, or deploy validation —
**never runs or imports** anything under a skill directory. The loader opens exactly one file
per skill, `SKILL.md`, and only `lstat`s the rest. Treat `scripts/` as what it is: code that
came with instructions, to be run (if ever) by an agent inside its own container under that
agent's permissions, not by the framework.

### Status — declaration and validation only

This release covers the declaration: the module type, the registry, the catalog, the manifest
field, and deploy validation. **agentd does not load skillpacks yet** — an agent that declares
`spec.skillpacks` deploys and starts, the skills are not exposed to the model, and agentd logs
`skillpacks declared but not loaded by this executor` at WARN on startup so the declaration is
not silently ignored. Exposure to the model and mounting into the container are tracked in #374.

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
  (`aaestro.graphs.conditions:needs_research`) still work for one version. They may only
  point into `aaestro.graphs`, and validation logs a warning for each import-reference
  condition.

## Agents — model resilience

An agent's `spec.model` may declare what to do when the primary model fails. Every field below is
optional; leave them out and behavior is unchanged. Validation, resolution and the runtime failover
loop are in place. Key rotation within a provider (#360) and non-Anthropic adapters (#361) follow.

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

The resolved result is delivered to agentd as `TaskRequest.config.model`. agentd **pins the model
chain from that declaration when the task starts** and calls that model from the primary onward; a
reload never mixes into a running task (the same rule as the module bundle). Without a
declaration — direct requests outside any graph — it uses the manifest defaults.

### Failover at runtime

Failover is per **turn**. When the primary's retry policy is exhausted (the `retry` declaration, or
the defaults: rate limit starts at 10 s for 5 attempts, network at 1 s for 3), agentd sends the
**same conversation** to the next fallback whose `on` contains the failure class. If the class is
not in `on`, the task fails without switching. When the last fallback is exhausted too, the last
error is returned.

| `on` class | Failure it covers |
|---|---|
| `rate_limit` | provider rate limit (`LLM_001`) |
| `server_error` | provider 5xx (`LLM_003`), and network errors or timeouts (the provider could not answer; the adapter maps connection failures to `NET_001` and request timeouts to `NET_002`, which the `network` retry policy retries before the switch) |
| `context_overflow` | `LLM_002` — switches immediately without retrying ("the same request to a model with a longer context") |

An unparseable response (`LLM_004`) and configuration errors never trigger a switch.

- **`circuit`**: every model (provider/name) has its own circuit breaker. A model whose breaker is
  open is **skipped without a call**. The skipped slot counts as the failure class that opened
  that breaker (`rate_limit` if it opened on rate limits, otherwise `server_error`), so a fallback
  whose `on` holds the same class takes over. If a breaker opens mid-retry, the remaining retries
  are not sent and the call goes straight to the next candidate. The same model shares one breaker
  across tasks and chains. When all candidates are open the call fails with the failure that
  opened the last breaker (`LLM_001` or `LLM_003`), retryable. State is exported as
  `aaestro_circuit_state{target="model:<provider>/<name>"}`.
- A fallback inherits `max_tokens` and `effort` from the primary.
- **Observability**: `TaskResult.usage.model` / `.provider` name the model that **actually
  answered the **last** turn; token totals are summed across turns. Per-attempt accuracy lives in
  the `aaestro_model_requests_total` / `aaestro_model_tokens_total` labels.
  `aaestro_model_requests_total` carries a `fallback_index` label (0 = primary). Each
  switch logs a WARN with `model`, `provider`, `fallback_index`, `error_code`, `next_model`,
  `next_provider`. Skipping an open circuit also logs a WARN with `opened_by`.
- Out of scope: switching to a non-Anthropic provider needs the adapters (#361) — until then the
  chain is refused with `CFG_001` when it is built. Rotating keys within one provider is #360.

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

`aaestro validate` and `aaestro deploy` print a `models` entry showing, per node, the resolved
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
  graph state schema changes → major; embedding model changes → minor+ with full reindex;
  skillpack: removing or renaming a skill → **major**, adding a skill or changing a
  `description` → minor, body/reference/script edits → patch
