---
title: Bounded output and artifacts
status: implemented
last_updated: 2026-07-17
change_log:
  - date: 2026-07-17
    summary: Added protected text spools, transactional byte-range indexes, stable bounded cursors, normalized rich output, immutable artifacts, terminal hashing, and live large-output verification.
---

# Bounded output and artifacts

Durable executions never place unbounded kernel output in SQLite or one JSON
response. Text is appended exactly as UTF-8 to a mode-`0600`,
execution-local spool under the private output directory. Each fsynced append
commits ordered byte ranges of at most 512 bytes to `output_chunks`. If the
database transaction fails, the spool is truncated back to its prior committed
offset.

`execution wait` includes the first page. `execution output` resumes from its
opaque `next_cursor`; replaying a cursor returns the same page, while advancing
never repeats a byte. Page budgets default to 65,536 bytes, accept
512–131,072 bytes, and leave headroom beneath the 262,144-byte
complete-response cap. Capability discovery reports all three values. The
reader stops after the first event that would exceed the requested budget and
does not materialize all remaining output indexes.

## Normalized events

The ordered event stream supports:

- stdout/stderr `stream` text;
- textual or artifact-backed `display_data` and `execute_result`;
- structured `error` names, values, tracebacks, and cursor-readable text;
- `clear_output` including its `wait` flag; and
- display-ID-addressed `update_display_data`.

MIME type, display ID, execution count, and display metadata are retained.
Small textual MIME representations use the text spool. Binary values and text
representations above 32 KiB become immutable mode-`0600` artifacts written
with atomic rename and directory fsync. Artifact records expose `path`,
`media_type`, `byte_size`, and `sha256:<hex>`.

## Terminal durability

Before `finished`, `error`, `interrupted`, `timed_out`, or `unknown` can be
committed, the spool is fsynced, its length is checked against SQLite, and its
SHA-256 plus finalization timestamp are persisted. Text over 64 KiB is also
published as a cursor-indexed `complete_text_output` artifact without removing
the ordinary cursor-readable chunks. Finalization is idempotent.

Any transport observation gap permanently keeps `output_complete=false`.
Later proof can establish a terminal state but cannot restore completeness.
Confirmed pruning removes both immutable artifact files and the execution text
spool; dry-run pruning changes neither.

## Verification

Deterministic tests cover Unicode chunk boundaries, private file modes,
transactional indexes, cursor replay and advancement, binary/large-text MIME
artifacts, error/clear/update ordering, terminal finalization, checksum
promotion, schema-2 migration, and spool pruning.

`integration/repro_bounded_output/test.sh` uses one live CPU assignment. It
executes more than 200 KiB of Unicode text plus large HTML and PNG output,
walks and replays bounded pages from separate CLI invocations, reconstructs
the exact text without duplicate cursors, verifies every artifact checksum and
mode, validates the whole-text artifact, then stops the controller and runtime
and requires an empty live session list.
