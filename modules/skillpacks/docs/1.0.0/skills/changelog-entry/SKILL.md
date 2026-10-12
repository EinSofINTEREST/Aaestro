---
name: changelog-entry
description: Writes a changelog entry in Keep a Changelog style and picks the next semantic version. Use when a change is ready to ship and the user asks for a changelog line, release notes entry, or version bump.
license: Apache-2.0
compatibility: Requires Python 3.12+ to run the bundled script
metadata:
  author: aaestro-examples
  version: "1.0"
---

# Changelog entry

Turn a described change into one changelog line and the version it belongs to.

## Steps

1. Classify the change with the table in [the category reference](references/CATEGORIES.md).
2. Decide the bump: `major` for a breaking change, `minor` for a backwards-compatible
   feature, `patch` for a fix.
3. Compute the next version. Paths in this skill are relative to the skill directory —
   run the bundled script from there, not from the project being edited:

   ```bash
   python scripts/next_version.py 1.4.2 minor
   ```

4. Write the entry under the matching heading, newest first.

## Entry rules

- One line, imperative mood, no trailing period: `Add retry budget to the fetch client`
- Say what changed for the user, not which file moved
- Link the issue or pull request when there is one

## Example

Input: "fetch client now retries three times on 503" (current version `1.4.2`)

```markdown
## [1.5.0] - 2026-10-12

### Added

- Retry transient 503 responses up to three times in the fetch client
```

## Edge cases

- No previous version: start at `0.1.0`
- Several changes at once: one entry per change, one version bump for the highest category
