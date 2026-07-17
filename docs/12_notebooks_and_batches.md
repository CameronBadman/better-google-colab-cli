---
title: Guarded notebook documents and durable batches
status: implemented
last_updated: 2026-07-17
change_log:
  - date: 2026-07-17
    summary: Added nbformat-only path-namespaced documents, guarded edits and ID assignment, explicit output writeback, durable ordered batches, and live stateful verification.
---

# Guarded notebook documents and durable batches

Notebook operations use `nbformat` exclusively. A notebook ID is the SHA-256
of its canonical resolved absolute path; renaming the file creates a different
identity. A cell ID is meaningful only together with that notebook ID.

## Inspection and mutation

`notebook cells` returns bounded metadata pages and omits source and outputs.
`notebook cell` selects exactly one cell by path plus ID or zero-based index,
returning exact UTF-8 source and its SHA-256 without outputs. Reads never add
IDs or rewrite a file. Duplicate IDs reject ID selection; a missing ID must be
handled explicitly.

`notebook update` writes one source atomically. Its optional expected hash is
checked against exact `nbformat` source without newline normalization. Agent
workflows always supply the inspected hash. `notebook ids assign` is the only
operation that fills missing IDs and requires the expected SHA-256 of the
entire current notebook file.

Notebook-backed execution resolves and optionally hash-checks the cell before
queueing. The resulting durable source snapshot is independent of later file
edits.

## Explicit output writeback

`notebook write-output EXECUTION_ID` is the only in-place output mutation. It
requires a terminal `finished` or `error` execution, complete output, the
original canonical notebook identity, the original cell ID, and unchanged
source SHA-256. Artifact-backed MIME data is read only after its size and
SHA-256 are verified. The atomic rewrite changes only the selected cell's
outputs.

## Parent and child batches

`execution batch start` selects cells by repeated ID or index and atomically
creates one parent UUID plus one child execution UUID per cell. All children
are visible through parent status in notebook order and use the existing
session's FIFO; batch start never allocates a runtime.

The default policy stops after the first child error. Remaining queued
children become `interrupted` with reason `batch_stopped` and are never sent.
`--continue-on-error` dispatches later children. Parent cancellation propagates
normal queued/running cancellation intent. Attached terminal failure exits 1;
status remains an exit-0 observation and wait timeout exits 124 without
cancellation.

Controller restart submits active parents before standalone queued work and
excludes their member executions from independent scheduling. This preserves
ordering and policy while retaining the same at-most-once child dispatch
contract.

## Verification

Deterministic tests cover path namespacing, missing/duplicate IDs, exact source
hashes, atomic conflicts, guarded writeback, artifact verification, atomic
parent/child creation, stop/continue/cancel behavior, restart scheduling, and
standalone work after a failed batch.

`integration/repro_notebook_batches/test.sh` uses one live CPU assignment to
verify state shared across selected cells, guarded execution, explicit output
writeback, both batch policies, undispatched-child isolation, stale-edit
rejection, ID assignment, and an empty final server session list.
