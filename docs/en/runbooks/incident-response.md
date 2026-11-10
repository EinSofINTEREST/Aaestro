# Incident Response

**[한국어](../../ko/runbooks/incident-response.md)** | English

How to respond when an Aaestro alert fires. Normative rules:
[05-error-handling.md](../../../.claude/rules/05-error-handling.md).

## Severity

| Level | Meaning | Examples |
|---|---|---|
| **P0** | Whole-system failure | All runs failing, checkpoint loss |
| **P1** | Core capability down | Key agent down, >50% failure rate |
| **P2** | Degraded | Single agent slow, one tool failing |
| **P3** | Minor | Small quality regression |

## First Five Minutes

1. Open the **Overview** dashboard — is this one agent, one graph, or the host?
2. Filter logs by `run_id` — one id links orchestrator → runtime → agentd → protocol.
3. Check the **error code distribution**. The prefix decides the response:
   `LLM_*` (provider), `RT_*` (container), `MCP_*` / `A2A_*` (protocol),
   `GRAPH_*` (topology/state), `STOR_*` (checkpoint).

## By Alert

### AgentHighFailureRate

Task failures exceed 10% for one agent.

1. `aaestro agent logs <agent>` — read the dominant `error_code`.
2. `LLM_001` (rate limit) → see [ModelRateLimited](#modelratelimited).
   `LLM_005` (max turns) → the prompt is likely looping; check the promptset version.
   `MCP_003` → a tool is failing; see the Protocol dashboard.
3. If a recent deploy correlates, roll back the module version — module versioning
   makes this immediate.

### AgentDown

`aaestro_agent_health == 0` for 3 minutes.

1. `aaestro agent inspect <agent>` — compare the manifest against what actually loaded.
2. A failed `initialize()` keeps the container from reaching Ready. The usual causes are
   `MCP_001` (a required MCP server failed to start) and `CFG_002` (a secret key does
   not resolve in any scope).
3. Restart only the affected agent — running graphs resume from their checkpoints.

### ContainerRestartLoop

More than five restarts in ten minutes.

1. Check `reason` on `aaestro_container_restarts_total`: `unhealthy` (replaced after failing
   health checks), `failed` (the replacement container did not start — retried after the next
   backoff), `gave_up` (the ceiling was hit — see [AgentFailed](#agentfailed)). OOM kills are not
   classified yet — the runtime does not log `RT_003` on a restart, and the replaced container is
   removed — so memory pressure shows up as `unhealthy`. Look for `oom` in
   `docker events --filter event=oom`; if it is there, raise `runtime.resources.memory` or lower
   concurrency.
2. Repeated `failed` means the replacement keeps failing with an error that a retry could clear —
   a retryable `RT_001` (the container did not start) or `RT_004` (the image could not be fetched
   because the daemon or the registry did not answer). While it retries, the replica stays listed
   as **Unhealthy** and receives no tasks. A missing image or a spec the runtime refuses is not
   retried at all — it ends in [AgentFailed](#agentfailed) with `reason="permanent"`.
3. A restart whose container fails to start with a retryable error still counts. After five
   restarts in ten minutes the runtime marks the agent **Failed** and stops retrying. Fix the
   cause, then redeploy.
4. An agent that is restarted **while it is still starting** — never healthy, restarted at
   `interval_s × unhealthy_threshold` — needs a longer startup grace, not a fix to the agent. Raise
   `runtime.health_check.startup_grace_s` and `orchestrator.deployment_ready_timeout_s` together;
   raising only the grace lets the deployment give up first (`RT_002`).

### AgentFailed

A replica's restarts ended in **Failed** — the runtime will not restart it again.

1. Check `reason` on `aaestro_container_restarts_total`: `gave_up` (five restarts in ten minutes
   — the cause is in [ContainerRestartLoop](#containerrestartloop)) or `permanent` (the
   replacement failed with a non-retryable error, so it was not retried).
2. For `permanent`, the `agent restart failed permanently` log carries the `error_code`. `RT_004`
   means the image exists neither locally nor in its registry — build or push it. `RT_001` means
   the container spec or the agent network does not match the runtime. Retrying would fail the
   same way.
3. Fix the cause, then redeploy. A Failed replica receives no tasks and is no longer health-checked.

### ModelRateLimited

The provider is rejecting requests.

1. Reduce the per-agent semaphore so fewer calls are in flight.
2. Switch to a fallback model if the graph tolerates it.
3. `RATE_LIMIT_RETRY` already backs off up to 300s — sustained alerts mean the quota
   itself is too small, not that retries are missing.

### DecisionModelMostlyUncertain

A decisionset question answers `uncertain` more than half the time, so the decision model is
mostly handing work back to the LLM path.

1. Check `aaestro_decision_bands_total` per question — one question, or all of them? One
   question means its wording or bands; all of them means the provider or the locale.
2. Re-run the calibration for that decisionset version (06 Calibration — the labeled set under
   the module's `calibration/`) and compare the report with the declared `act_at` / `reject_at`.
3. Publish a new decisionset version with corrected bands (patch) or wording (minor). The
   system keeps working meanwhile — `uncertain` always takes the original path.

### DecisionModelUnavailable

Decisions are falling back because the provider times out, errors, or its circuit is open.

1. Nothing is broken for users: every use treats `unavailable` as `uncertain`. The cost is
   more LLM work and less filtering, not wrong answers.
2. Check the egress proxy logs for the provider's base URL (`DEC_001` / `DEC_002` / `TO_004`)
   and the provider's status page.
3. If the provider is down for long, lower `decision.timeout_s` or disable `spec.decision`
   on the busiest agents so tasks stop paying the timeout on every judgment.

### ServiceRunStalled

A service run is active but has made no progress for 30 minutes.

1. Check `aaestro_service_idle_delay_seconds` — a run sitting at the idle ceiling is
   **working as designed** if there is genuinely no input.
2. If input exists, the watcher node is likely failing silently. Read the iteration logs
   (`iteration` field) and confirm the `is_idle` predicate is not always true.

### AccessRegistryUnreachable

An enforcement point (Memory Service, egress proxy, or an A2A server) cannot reach the access
registry in the control plane.

1. While this fires, decisions that are not already cached are **denied** and permissions
   that were already allowed keep working — until the decision's `valid_until` when it has one
   (a grant behind it), with no deadline otherwise (a declaration). A
   revocation made now is **not applied** until the registry is reachable again. A registry that
   answers but **rejects** the enforcement point (a wrong enforcer token) is not an outage: the
   enforcement point drops its cache and denies everything — fix the token.
2. Check the control plane process and the network between it and the `component` in the
   alert.
3. If a revocation is urgent, stop the affected agent's containers — that does not depend on
   the registry. See [access-control.md](access-control.md#when-the-registry-is-unreachable).

### AccessGrantRefusalsSpike

The registry refused many grants in ten minutes: requests above an expansion ceiling, a
permission agent granting to itself, or a worker agent calling the grant API directly.

1. Group the refusals by requester in the logs (`decided_by`, `resource`, `target`).
2. A single worker agent behind most refusals is likely being steered by untrusted input.
   Revoke its outstanding grants and inspect the run that triggered the requests.
3. Refusals are the ceiling doing its job. Raise a ceiling only by changing the group
   declaration, never to silence this alert.

## Escalation

Page for P0/P1. For P2/P3, file an issue with the `run_id` and the error-code
distribution attached — those two make the failure reproducible.

## See Also

- [recovery.md](recovery.md) — restoring runs and reindexing memory
- [access-control.md](access-control.md) — emergency revocation and registry outages
