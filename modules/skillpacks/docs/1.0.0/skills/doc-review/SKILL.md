---
name: doc-review
description: Reviews a Markdown document for structure, accuracy of commands, and reader fit, and reports findings by severity. Use when the user asks to review, proofread, or tighten documentation, a README, or a runbook.
---

# Document review

Read the whole document before commenting. Report findings; do not rewrite unless asked.

## Steps

1. State who the reader is and what they should be able to do afterwards.
2. Walk the [checklist](references/CHECKLIST.md) top to bottom.
3. Report with the [findings template](assets/findings-template.md) — highest severity first.

## Severity

- **blocker** — a reader following the document fails or does damage
- **major** — a reader gets there, but is misled or loses time
- **minor** — wording, order, formatting

## Edge cases

- A command you cannot verify: say so; do not mark it correct
- A document with no stated reader: that is the first finding
