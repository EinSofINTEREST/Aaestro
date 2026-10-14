# Recovery

**[한국어](../../ko/runbooks/recovery.md)** | English

Restoring runs, checkpoints, and memory indexes after a failure. Normative rules:
[05-error-handling.md](../../../.claude/rules/05-error-handling.md).

## Checkpoint Failures

`STOR_001` (save) or `STOR_002` (restore) means run recovery is at risk — a run that
cannot checkpoint cannot resume.

1. Check the checkpointer backend first (Redis/PostgreSQL connectivity, disk space).
2. Runs already in flight keep executing; they lose only the ability to resume.
3. Once the backend is healthy, resume from the last good checkpoint:
   `aaestro run resume <run_id>`.

Do not delete checkpoints to "clear" the error — they are the only path back to a
consistent state.

## Service Run Halted

`GRAPH_005` means a service run exceeded `max_failure_streak` and stopped deliberately,
so a crash loop cannot burn model quota indefinitely.

1. Read the halted run's error-code distribution — the streak has one dominant cause
   far more often than not.
2. Fix that cause (provider quota, MCP server, topology).
3. Resume from the last iteration checkpoint: `aaestro run resume <run_id>`.

The run continues from the **next** iteration; completed iterations are not repeated.
Its state is read back from the checkpointer, even after the control plane restarted: the
output of the last iteration if it finished, or the input of the last iteration if it failed
(a failed iteration's partial results are dropped, as in a live run). A run that left no
iteration checkpoint cannot be resumed (`STOR_002`) — start a new run instead.

## Node Failure Mid-Run

A node failure (`GRAPH_002`) does not corrupt graph state — the failed node's output is
never merged.

1. `aaestro run trace <run_id>` — find the failing node and its `task_id`.
2. Reproduce the agent in isolation with the stored request: `aaestro replay <task>`.
3. After fixing, resume: `aaestro run resume <run_id>`. Nodes that already succeeded do
   not re-execute.

## Memory Index Corruption

`MEM_003` (indexing backlog, or an embedding failure — at indexing or query time, including an embedding response that could not be interpreted) or `MEM_004` (query or search failure / corrupt index).

1. Check `aaestro_memory_index_lag_seconds`. Sustained lag means the indexing queue is
   not draining — usually the embedding provider.
2. Rebuild when the index is corrupt or the embedding model changed:
   `aaestro memory reindex <space>`.
3. Search keeps serving from the **old** index during a rebuild, switching atomically on
   completion — reindexing does not take memory offline.

### After a Memory Service restart

The search index lives in the service's memory; the entries live in the store. On start the
service queues every stored entry for indexing (`memory index warm-up queued`, with the entry
count) and the normal indexing loop rebuilds the index. Until `memory index warm-up completed`
is logged, older memories may be missing from search results. Progress shows in
`aaestro_memory_index_lag_seconds`, measured from the restart rather than from when each entry
was stored. Every restart embeds every entry again — with a paid embedding provider, that cost
grows with the store.

## Backup and Restore

| Asset | Cadence | Notes |
|---|---|---|
| Checkpointer DB | Daily | Required for run resume |
| Module registry | Git | `modules/`, `graphs/`, `agents/`, `groups/` are all committed |
| Config | Git | `configs/` |

Retention: run records and usage 1 year, checkpoints 30 days after completion, logs 30
days. Rehearse restores quarterly — an untested backup is not a backup.

## See Also

- [incident-response.md](incident-response.md) — triage when an alert fires
