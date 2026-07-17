---
title: Durable SQLite state
status: implemented
last_updated: 2026-07-17
change_log:
  - date: 2026-07-17
    summary: Added atomic parent/child batch queueing, ordered restart-safe membership, stop/cancel policy, and collision-free protected child source spools.
  - date: 2026-07-17
    summary: Added restart reconciliation over durable dispatch proof plus connection-identity-scoped readiness evidence and reconnect counts.
  - date: 2026-07-17
    summary: Migrated to schema version 3 for indexed text-spool paths, committed byte counts, terminal output hashes, and finalization timestamps.
  - date: 2026-07-17
    summary: Migrated to schema version 2 for execution timeout, interrupt intent, and reply-status evidence used by durable kernel workers.
  - date: 2026-07-17
    summary: Added schema migration, profile isolation, one-time legacy import, durable transition records, protected source spools, batches, artifacts, and confirmed pruning.
---

# Durable SQLite state

Better Colab's authoritative local state is
`${XDG_STATE_HOME:-~/.local/state}/better-colab/controller.sqlite3`. The
database uses WAL, foreign keys, a 5000 ms busy timeout, `synchronous=FULL`,
explicit `BEGIN IMMEDIATE` transactions, and `PRAGMA user_version=3`.

The database and payload files are mode `0600`. Better Colab's state, artifact,
source, output, and runtime directories are mode `0700`. The fallback Unix
runtime directory is `$TMPDIR/better-colab-$UID`; when available,
`$XDG_RUNTIME_DIR/better-colab` is preferred.

## Schema

Version 1 creates the core tables:

- `profiles` for normalized configuration/auth namespaces and import evidence;
- `sessions` for current endpoint, protected token, kernel/session identity,
  hardware, keep-alive PID, and lifecycle timestamps;
- `executions` for source/kernel snapshots, idempotency, proof flags,
  deadlines, errors, completeness, and state;
- append-only `execution_transitions`;
- `execution_batches` and ordered `batch_members`;
- ordered `output_chunks` and immutable `artifacts`;
- `kernel_connections` for connection and readiness-probe identity/evidence.

Migration 2 adds the requested execution-timeout duration, persisted interrupt
terminal intent, and validated execute-reply status. New databases apply the
same ordered migrations as existing databases rather than maintaining a
separate creation schema. Migration 3 adds the execution-local text-spool
path, committed byte count, SHA-256, and finalization timestamp.

Session deletion does not own or cascade execution history. Execution pruning
does cascade its journal, output index, batch membership, and artifact
metadata.

## Profiles and upstream import

A profile ID hashes the canonical resolved `--config` path, normalized auth
provider, and canonical OAuth-config path. Profiles are filtered on every
session/execution query, so multiple auth/config namespaces can safely share
one controller.

On first profile access, an existing upstream `sessions.json` is read and
hashed. Its sessions are imported in one transaction and the hash, file mtime,
and import time are stored. The JSON file is never renamed, rewritten, or
deleted. If it later changes, the store returns `LEGACY_STATE_CHANGED` and
continues using SQLite; it never merges the changed file automatically.

## Atomic queueing and source retention

Queueing computes the source and canonical-request SHA-256 values, writes and
fsyncs the exact bytes to a temporary mode-`0600` spool, atomically renames it,
then inserts the execution plus `created` and `queued` transitions in one
transaction. A failure at any database boundary rolls back the rows and
removes the spool.

The profile/idempotency-key index is unique. An identical canonical request
returns its existing execution; different inputs return
`IDEMPOTENCY_CONFLICT`. No second source file is created in either retry path.

Only queued work retains source bytes. A first matching inbound kernel message
confirms dispatch and destroys the spool. A pre-confirmation disconnect that
becomes `unknown`, or a queued cancellation that becomes terminal, also
destroys it. File removal precedes the committing state update so a crash can
lose replayability but cannot permit an unsafe replay.

## Durable batches

Batch creation first fsyncs a distinct protected source spool for every
selected cell, then inserts the parent, all child executions and transitions,
and ordered memberships in one transaction. Any conflict rolls back every row
and removes every newly created spool. A child execution UUID therefore
belongs to exactly one position in exactly one batch.

Parent state is `queued`, `running`, `cancelling`, then terminal `finished`,
`error`, or `interrupted`. Default stop-on-error transitions remaining queued
children to `interrupted` with reason `batch_stopped`; continue-on-error
preserves later dispatch. Restart discovers active parents first and excludes
their children from ordinary queued-work scheduling, so one coordinator
continues the recorded policy.

## Restart evidence

Kernel-connection rows bind readiness evidence to profile, session, kernel ID,
Jupyter session ID, and one controller-generated connection ID. A nonce,
timestamp, latency, and error are stored only while that exact connection is
current. Disconnect and controller startup clear the readiness fields rather
than carrying a stale healthy result across transports.

Restart reconciliation is driven exclusively by durable execution flags.
Unconfirmed `dispatching` becomes terminal `unknown`; complete reply-plus-idle
evidence is finalized immediately; confirmed `running` becomes
`disconnected`; and already-disconnected work remains eligible for reconnect.
Every restart observation gap permanently writes `output_complete=false`.
Successful same-identity connections increment `reconnect_count`; they never
make a state terminal by themselves.

## Pruning

`better-colab execution prune --before TIMESTAMP --format json` is a dry run
unless `--confirm` is supplied. `--dry-run` and `--confirm` are mutually
exclusive. Timestamps must include a timezone.

Only `finished`, `error`, `interrupted`, `timed_out`, and `unknown` records
strictly older than the cutoff match. A session filter is optional. Preview
returns execution IDs and total artifact bytes. Confirmed deletion removes the
same precomputed records transactionally and unlinks only their enumerated
artifact and output-spool paths.

## Testing strategy

- Inspect every required pragma, table, schema version, migration, and
  filesystem mode.
- Open two profile views over one database and assert isolation.
- Import legacy state, mutate the source JSON, reopen, and assert diagnostic
  without re-import.
- Raise after execution insertion and assert row, journal, and spool rollback.
- Validate every permitted/forbidden transition used by current storage.
- Confirm idempotent reuse/conflict and source disposal.
- Delete a session and retain execution history.
- Preview and confirm prune while a queued record remains untouched.
