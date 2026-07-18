---
title: Portable Better Colab skill
status: implemented
last_updated: 2026-07-18
change_log:
  - date: 2026-07-18
    summary: Packaged the canonical skill in the Codex plugin and added byte-for-byte CI enforcement.
  - date: 2026-07-18
    summary: Added and forward-tested the compact vendor-neutral skill, removed the stale bundled operator manual, and verified its complete workflow live on one CPU session.
---

# Portable Better Colab skill

The canonical agent guide is
`.agents/skills/better-colab/SKILL.md`. It is a shell-only operational layer
over the same typed runtime and JSON v1 contract available to every caller.
The skill contains no hidden logic, scripts, references, provider-specific
instructions, Drive workflow, or MCP dependency.

`agents/openai.yaml` supplies optional catalog metadata without changing the
portable instructions. Its default prompt names `$better-colab` explicitly so
an installed skill can be selected directly.

## Content and budget

The skill is constrained by a 4,096-byte hard budget and remains well below
the planned approximate 1,500-token budget. It teaches only:

- passive `doctor` and scoped `capabilities` discovery;
- explicit `session ensure`, readiness checks, and final cleanup;
- durable starts with stable idempotency keys;
- detached waits, exit 124, terminal failure, and unknown-state handling;
- bounded wait/output pages with exact cursor field paths;
- local artifact paths and portable SHA-256 verification;
- path-scoped notebook IDs, exact hash fields, guarded edits, and explicit
  writeback; and
- dry-run retention/pruning behavior.

The former `skills/colab-operator/SKILL.md` duplicated a long upstream manual
and included stale control-plane and Drive guidance. It and the bundled
`colab skill` compatibility command were removed. Machine-readable capability
discovery now owns the detailed command schema.

## Testing strategy

`tests/test_better_colab_skill.py` enforces metadata, the 4,096-byte limit,
required safety contracts, vendor neutrality, the absence of extra resource
directories, catalog metadata, and removal of the stale bundled skill.

The skill creator's structural validator is run against the canonical
directory. Three fresh-agent forward tests cover detached idempotent execution,
exception recovery, guarded notebook replacement, complete cursor traversal,
artifact verification, and cleanup without pruning. Their first pass exposed
missing JSON field paths and artifact-location details; those findings were
encoded as failing tests before the skill was tightened.

`integration/repro_portable_skill/test.sh` then forward-tests the same workflow
against one reusable live CPU assignment. It verifies idempotent detached
retry, bounded output, artifact checksums, nonzero exception waits and
traceback retrieval, source-hash-guarded notebook mutation, explicit
writeback, retained execution history after session release, and empty final
session listings.

The canonical file is copied into `plugins/better-colab` by the deterministic
package tool. CI compares the two copies byte-for-byte so plugin packaging
cannot introduce a divergent operational manual.
