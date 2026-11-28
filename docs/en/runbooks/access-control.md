# Access Control Operations

**[한국어](../../ko/runbooks/access-control.md)** | English

Revoking permissions from running agents, and what happens when the access registry is unreachable.
Background: [Access control](../architecture.md#access-control--decided-outside-the-container).

## Emergency Revocation

Something an agent can reach must stop now — a leaked memory space, an outside host, a remote MCP
tool, a connection to another agent.

1. **Revoke the permission.** In the UI: **권한** tab → pick the agent → write the reason → press
   **회수** (or **쓰기 회수** / **전체 회수** for memory) on the declared permission. Through the API:

   ```bash
   curl -X POST -H "Authorization: Bearer $AAESTRO_CONTROL_TOKEN" -H 'content-type: application/json' \
     -d '{"agent": "researcher", "kind": "egress", "target": "api.search.example.com",
          "reason": "incident 311"}' \
     http://127.0.0.1:8700/v1/access/revocations
   ```

   | What to stop | `kind` | `target` | `mode` |
   |---|---|---|---|
   | writing a memory space, keep reading | `memory` | space id, e.g. `group:research:knowledge` | `rw` |
   | any use of a memory space | `memory` | space id | omit |
   | an outside host | `egress` | `host` or `host:port` (`:443` omitted) | omit |
   | one remote MCP tool | `mcp_tool` | `server/tool` | omit |
   | a whole remote MCP server | `egress` | the server's host | omit |
   | calls to another agent | `a2a` | the callee's name | omit |
   | model API calls (the operator's key) | `model` | the provider name, e.g. `anthropic` | omit |

   A revocation beats declarations and grants, and applies to the agent's **next request**. No
   restart, no redeploy.

2. **Confirm it applies.** The agent's next attempt appears under **최근 거부** with
   `decided_by: operator`, or in `GET /v1/access/agents/{name}` → `denials`. Enforcement points
   log the refusal (`decision: deny`), and `aaestro_access_grants_total{op="revoke"}` counts the
   revocation.

3. **If it must stop everything the agent does,** tear its deployment down
   (`DELETE /v1/deployments/{id}` or **해체**). The agents drain and stop, then the deployment's
   identities are revoked — so even a container that failed to stop is refused by every
   enforcement point. Draining waits for in-flight tasks, so revoke the specific permission first
   (step 1) when seconds matter.

4. **Undo when resolved.** **되돌리기** in the tab, or `DELETE /v1/access/rules/{rule_id}`. The
   record stays with `lifted_at` set.

### What a revocation does not reach

- Data the agent already has: memory it read, responses it received, files it wrote inside its
  container.
- Effects that stay inside the container, including a stdio MCP server's local actions.
- Secrets injected as environment variables. Revoke them by rotating the secret and redeploying.
  The model key and remote MCP credentials are not among them — the egress proxy holds those.
- A widened permission's source. If a permission agent granted it, revoke it here and check
  [AccessGrantRefusalsSpike](incident-response.md#accessgrantrefusalsspike) for the requests that
  led there.

## When the Registry Is Unreachable

Alert: [AccessRegistryUnreachable](incident-response.md#accessregistryunreachable).

While an enforcement point cannot reach the registry:

| Decision | Behaviour |
|---|---|
| not yet cached | **denied** (`MEM_001`, `A2A_004`, `ACC_002` / `503` at the proxy) |
| cached allow from a declaration | keeps working |
| cached allow from a grant | keeps working until its `valid_until`, then denied while the outage lasts |
| cached deny from a temporary revocation | stays denied until its `valid_until`, then denied anyway while the outage lasts |
| a revocation made during the outage | **not applied** until the registry is reachable |

This is deliberate: agents keep doing what they were already allowed to do, and nothing new is
allowed. The cost is that revoking does not work during the outage.

1. Restore the control plane, or the network between it and the `component` named in the alert.
   Enforcement points reconnect on their own. If the registry's version moved while they were cut
   off — a revocation made during the outage — they drop their cached decisions, and the
   revocation applies from the next request.
2. If a revocation cannot wait, stop the affected agents without the control plane:
   `docker stop aaestro-<agent>-<replica>`. When the control plane is back, tear the deployment
   down so its record matches.
3. A registry that answers but **rejects** an enforcement point (a wrong enforcer token) is not an
   outage: that enforcement point drops its cache and denies everything. Fix the token.

## Closing the Host Gateway

With the egress proxy on, agents sit on an internal network with no route out — except to the host
itself, which is the network's gateway. Any host service listening on all interfaces is reachable
from agents without passing through the proxy. Close it once per agent network, on the host:

```bash
sudo deployments/docker/isolate-agent-network.sh apply aaestro-net     # runtime.network
sudo deployments/docker/isolate-agent-network.sh status aaestro-net
```

- It adds a chain to `INPUT` for that network's bridge: connections agents **open** to the host are
  dropped; replies to connections the host opened (the control plane calling an agent's Control API)
  pass. IPv6 gets the same rule when `ip6tables` is present.
- Apply it **after** the network exists (the control plane creates it on the first isolated
  deployment) and again after a reboot or a firewall reload — the rule is not persistent.
- Services agents must reach (the egress proxy, the Memory Service, whatever answers
  `orchestrator.access_agent_url`) belong **on the agent network**, not on the host. After this rule
  they cannot be reached through the gateway.
- `remove` takes it out. The script refuses to `apply` to a network that is not internal.

The control plane warns at startup (`control plane is reachable from isolated agents through the
network gateway`) when the proxy is on and it binds anything but loopback. Bind it to loopback, or
apply the rule.

## Model Provider Records from Before #399

Before #399 the model termination was decided as `egress` on the provider's logical host
(`api.anthropic.com`, `openai-compat.invalid`). It is now `model` on the provider name. Records already in
the access store are not rewritten:

- **Revocations** (`effect: deny`) of `egress` on one of those two hosts still cut the model
  termination — the registry counts them as a `model` revocation of that provider, so an upgrade does
  not silently lift them. They also still cut a CONNECT tunnel to `api.anthropic.com`. To move to the
  new form, revoke `model` / `anthropic` (or `openai-compat`) for the same agent, then lift the old
  record (**되돌리기** or `DELETE /v1/access/rules/{rule_id}`).
- **Grants** (`effect: allow`) of `egress` on those hosts no longer open the model termination — that
  was the defect. They expire on their own (every grant has a TTL); until then they only open a CONNECT
  tunnel to `api.anthropic.com`, as before. Nothing to do, but lift them if the tunnel is not wanted.
- An agent that reached the model only through `runtime.egress` or such a grant, without declaring the
  provider in `spec.model`, now gets `403 ACC_001` on its model calls. Declare the provider and redeploy.
- Remove `api.anthropic.com` / `openai-compat.invalid` from a ceiling's `egress` unless the CONNECT
  tunnel is what you mean — it no longer has anything to do with model access.

This holds for an `egress` revocation on `api.anthropic.com` or `openai-compat.invalid` **whenever it
was made**, before or after the upgrade: it also cuts that provider's `model` decision. There is no
revocation that cuts only the tunnel. To stop just the tunnel, take the host out of the agent's
`runtime.egress` (a declaration change) and lift any grant of it — and leave the model alone.

When a model call is refused and you look for the cause: **최근 거부** / `denials` show kind `model`,
target the provider name, `decided_by: operator`, but `rules` has no `model` record — the revocation
is the `egress` record on the provider's old host.

To find the records: `GET /v1/access/agents/{name}` → `rules`, kind `egress`, target one of the two hosts.

## Widening a Permission

Operators do not grant. A worker agent asks the permission agent over A2A, and the registry limits
what it can grant to the expansion ceiling in the group's `spec.access.ceiling` (and `global`'s).
To allow more, change the ceiling in the group declaration — the permission agent cannot change it,
and neither can a request. See [The permission agent](../api.md#the-permission-agent).

## See Also

- [incident-response.md](incident-response.md) — alerts and first response
- [Access registry API](../api.md#access-registry)
