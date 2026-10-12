# Aaestro Documentation

**[한국어](../ko/README.md)** | English

User and operator documentation for **Aaestro**, a modular multi-agent orchestration
framework built on LangGraph.

> The authoritative development ruleset lives in
> [`.claude/rules/`](../../.claude/rules/README.md). These docs summarize and guide;
> when they disagree with the ruleset, the ruleset wins and the docs need a fix.

## Contents

| Document | What it covers |
|---|---|
| [architecture.md](architecture.md) | System layers, interaction model, execution modes, resource scoping |
| [getting-started.md](getting-started.md) | Prerequisites, environment setup, assembling a first solution |
| [modules.md](modules.md) | Module system — skillsets, promptsets, memorysets, graphs, groups |
| [api.md](api.md) | Control Plane REST API — catalog, authoring, deployments, runs |
| [ui.md](ui.md) | Web UI — assembling, deploying, and running a system in a browser |
| [testing.md](testing.md) | Test strategy, determinism rules, quality gates |
| [ci/conventions.md](ci/conventions.md) | Repository governance and CI design rules |
| [ci/status-checks.md](ci/status-checks.md) | Single source of truth for required status check names |
| [runbooks/incident-response.md](runbooks/incident-response.md) | Alerts, first response, escalation |
| [runbooks/recovery.md](runbooks/recovery.md) | Restoring runs, reindexing memory, backups |
| [runbooks/access-control.md](runbooks/access-control.md) | Emergency revocation and registry outages |

## Commands

Setup, quality gates, images and stacks, the CLI, the long-running processes, and
configuration overrides are documented in the
[root README](../../README.md#commands) — the English front page of the project.

## Language Policy

- `docs/en/` is the source of truth — written first
- `docs/ko/` mirrors the same structure with Korean translations
- Both versions must stay synchronized (`Docs Sync Check` enforces the structure mirror)

## Planned Additions

- Agent Control API reference — the in-container surface `agentd` serves. The Control
  Plane side is documented in [api.md](api.md); this is the layer below it
