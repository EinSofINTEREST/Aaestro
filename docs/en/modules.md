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
loop are in place, and fallbacks may cross providers (see [Providers](#providers)). Key rotation
within a provider (#360) follows.

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

Only providers the framework knows (`anthropic`, `openai-compat`) validate.

### Providers

`spec.model.provider` (and each fallback's `provider`) names an adapter. Two are bound; any other
name is refused with `CFG_001` — it never falls back to a default provider.

| `provider` | API | Reached through |
|---|---|---|
| `anthropic` | Anthropic Messages API | `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY` |
| `openai-compat` | OpenAI Chat Completions and servers that speak it (vLLM, Ollama, gateways) | `OPENAI_COMPAT_BASE_URL`, `OPENAI_COMPAT_API_KEY` |

```yaml
spec:
  model:
    provider: openai-compat
    name: gpt-5-mini               # whatever model id the server at the base URL serves
    fallbacks:
      - {provider: anthropic, name: claude-haiku-4-5, on: [rate_limit, server_error]}
```

Fallbacks may cross providers in either direction. A turn that another provider's model started
(a tool call waiting for its result) is continued by the fallback: the conversation is rebuilt in
the fallback's message format.

What is specific to `openai-compat`:

- **Where it points is configuration, not code.** There is no default address. Behind the egress
  proxy the runtime sets `OPENAI_COMPAT_BASE_URL` to the proxy and puts the agent's identity where
  the key goes; the proxy holds the real key and the upstream address
  ([api.md](api.md#egress-enforcement)). Without a proxy, set both in `runtime.agent_env` / the agent's
  secrets. An agent that declares the provider without both settings fails to start (`CFG_001`).
- **The base URL stops before `/v1`.** The adapter and the proxy append `v1/chat/completions`.
  `https://api.openai.com`, `http://vllm:8000`, `http://ollama:11434`, and a gateway with a path
  prefix such as `https://gateway.example.com/llm` are right; `https://api.openai.com/v1` is not —
  the proxy refuses to start with it (`CFG_001`).
- **`effort` is not sent.** A fallback inherits the primary's `effort`, and models without
  reasoning support reject `reasoning_effort` with a 400 — a fallback that fails on that field
  defeats the declaration. The adapter logs a WARN (`effort is not forwarded to this provider`)
  once when it is built with an `effort`, so the declaration does not disappear silently.
- **Tool names are sent as declared.** OpenAI restricts function names to
  `^[a-zA-Z0-9_-]{1,64}$`. Skill names and `mcp__{server}__` prefixes fit, but a remote MCP tool
  whose name is long or contains other characters makes the provider answer 400, which fails the
  task as a configuration error. The adapter does not shorten or rewrite names — the name the model
  calls must be the name in the tool registry. Deployment validation does not check this yet.
- **Errors** follow the Anthropic adapter: 429 → `LLM_001`, a context-length 400 → `LLM_002`, 5xx →
  `LLM_003`, connection failure → `NET_001`, timeout → `NET_002`, an uninterpretable answer →
  `LLM_004`. Other 4xx (bad credentials, unknown model, invalid arguments) are configuration errors
  and are not retried or failed over. Context overflow is recognized by `error.code` or by wording;
  a server that words it differently is treated as a configuration error, and a server that
  silently truncates the prompt cannot be detected.
- **Lenient where compatible servers differ**: empty tool-call `arguments` mean a call without
  arguments. Token usage never fails a task: a `usage` that is missing, empty, or has a missing,
  null, non-integer or negative field counts as zero for that field and logs a WARN
  (`provider response carries no readable token usage, counting zero`).
- A tool result marked as an error is sent with a leading `[tool error]` line — Chat Completions
  has no error flag on tool messages.

Known limits:

- **`max_tokens` is sent under that name, and only when declared.** Some models — reasoning models
  in particular — may reject `max_tokens` and ask for `max_completion_tokens` instead (not verified
  against a live API). Do not declare `max_tokens` for a model that does not accept that name; the
  server's default then applies. A fallback inherits the primary's `max_tokens`.
- **The path is fixed at `v1/chat/completions`.** A gateway that serves the API under another
  version prefix (`/v1beta/openai`, for example) is not supported; a prefix *before* `/v1`
  (`https://gateway.example.com/api`) is.
- **Response `content` must be text or null.** A response whose `content` is an array of parts is
  `LLM_004`.
- **Only the headers a model call needs reach the provider** — `content-type`, `accept`,
  `accept-encoding`, `user-agent` (plus `anthropic-version` / `anthropic-beta` for `anthropic`).
  Organization, project or alternative key headers set inside the agent are dropped by the proxy,
  and cookies do not cross it in either direction.

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
- A provider without an adapter is refused with `CFG_001` when the chain is built. Rotating keys
  within one provider is #360.

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
  graph state schema changes → major; embedding model changes → minor+ with full reindex
