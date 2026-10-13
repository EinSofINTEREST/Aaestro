# docs skillpack

Two small [Agent Skills](https://agentskills.io/specification) kept exactly as the
standard lays them out — nothing in `skills/` is Aaestro-specific.

| Skill | Use it when |
|---|---|
| `changelog-entry` | a change needs a changelog line and a version bump |
| `doc-review` | a Markdown document needs a structured review |

`scripts/` holds code a skill may ask an agent to run. Aaestro never runs or imports it
while loading or validating the pack.
