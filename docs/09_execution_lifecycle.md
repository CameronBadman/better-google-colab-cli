---
title: Durable execution lifecycle
status: implemented
last_updated: 2026-07-17
change_log:
  - date: 2026-07-17
    summary: Added exact notebook-cell source capture and restart-safe parent batch scheduling with default stop, continue-on-error, and cancellation policy.
  - date: 2026-07-17
    summary: Added proof-preserving same-kernel reconnect, kernel-info idle boundaries, restart reconciliation, permanent gap tracking, and live no-replay verification.
  - date: 2026-07-17
    summary: Required output spool finalization and hashing before every terminal transition, with rich output normalized into durable cursor records.
  - date: 2026-07-17
    summary: Added the pinned low-level kernel adapter, per-kernel FIFO workers, exact-once dispatch boundary, matching reply/idle proof, idempotent start, condition waits, cancellation, deadlines, and the first live execution suite.
---

# Durable execution lifecycle

`better-colab execution start|status|wait|output|cancel|list` and the matching
`BetterColabClient` methods are implemented. `start` accepts exact UTF-8 file
or stdin source, or one guarded notebook cell selected by path plus ID/index.
It never allocates a runtime. The named session must already exist in the
selected profile.

## Dispatch and proof

The only private `jupyter-kernel-client` access is in
`better_colab.kernel_transport`. Its conformance test pins version `0.8.0` at
commit `f18e982c3265df5e923aa9def101ab3fd737e139` and verifies the session,
channel-queue, connection-event, and raw-send shape used by the controller.

For each request, the adapter constructs one complete `execute_request`. The
worker commits `queued -> dispatching`, the actual kernel/Jupyter identities,
and that request's message ID before passing the same message object to
`shell_channel.send`. It never calls the library's convenience `execute`
method, which would generate a second message.

The first inbound shell or IOPub message with the matching parent ID commits
`dispatching -> running`, starts any execution deadline, and destroys the
queued source spool. Completion requires both:

- a matching, structurally valid `execute_reply`; and
- a matching IOPub `status` whose execution state is `idle`.

They may arrive in either order. Output, idle alone, reply alone, an empty or
malformed reply, and a mismatched parent never prove success. An `ok` reply
plus idle proves `finished`, including silent execution with no output. An
error reply plus idle proves `error` and retains the error name, value,
traceback, and preceding output.

Before any terminal transition, the worker fsyncs the text spool, verifies its
committed length, records its SHA-256 and finalization timestamp, and promotes
large complete text to an immutable artifact. Storage rejects a terminal
transition that bypasses this gate.

A disconnect before the first matching inbound message becomes terminal
`unknown`; the request is never replayed. A disconnect after confirmation
becomes `disconnected` and permanently sets `output_complete=false`.

## Reconnect and restart

On controller restart, a record left in `dispatching` is terminally unknown
because the send boundary cannot be reconstructed. A confirmed running record
with already-durable matching reply and idle is finalized from that evidence.
Any other confirmed running record becomes `disconnected`; its exact output can
never again be claimed complete.

The recovery worker never calls `prepare_execution` and never reads queued
source bytes. It reconnects only when endpoint, kernel ID, and Jupyter session
ID exactly match the dispatch snapshots. It restores persisted reply, idle,
error, traceback, cancellation, and deadline evidence, then sends a distinct
`kernel_info_request`. Original-parent messages observed on the new connection
may supply missing proof in either order. If the kernel-info reply and its
matching idle arrive while original terminal proof is still incomplete, the
kernel has crossed a known idle boundary and the execution becomes `unknown`,
not successful. Transport loss before that boundary retries the same-identity
connection without replay.

## Workers and waits

The controller owns one dedicated blocking thread, persistent kernel
connection, and FIFO for each `(profile, session)` pair. Same-kernel work is
serialized; separate sessions execute concurrently. The controller, rather
than each CLI process, is the only shell/IOPub queue consumer.

Worker transitions publish onto an asyncio condition keyed by profile and
execution UUID. `execution wait` holds one local RPC until another transition
or its caller timeout. It does not issue status polls. `--timeout` and
`--wait-timeout` are observations only: a timeout returns current state with
`wait_timed_out:true`, exits 124, and never changes or cancels the execution.

`--detach` returns the durably queued record. A later process can call status
and wait. Attached start waits indefinitely unless `--wait-timeout` is
provided. Observing a terminal error through `status` exits 0; attached start
or wait exits 1 when it observes `error`, `interrupted`, `timed_out`, or
`unknown`. These are successful JSON observations, not API error envelopes.

## Idempotency, cancellation, and deadlines

The foreground API generates the public execution UUID before dispatch.
SQLite uniquely indexes optional idempotency keys within a profile. The
canonical request includes session, source SHA-256, provenance, and execution
timeout. Identical reuse returns the original UUID and never queues a second
send; conflicting reuse returns `IDEMPOTENCY_CONFLICT`.

Queued cancellation atomically removes the source and records `interrupted`
without connecting to the kernel. Running cancellation records intent and
sends one kernel-wide interrupt. It becomes `interrupted` only when the
original execution later supplies matching reply/idle proof consistent with
interruption. If an ordinary `ok` reply wins the race, the result is
`finished`.

`--execution-timeout` is persisted as a duration but does not become an
absolute deadline until dispatch is confirmed by matching inbound evidence.
At expiry the worker records `timed_out` intent and sends one interrupt.
Matching interruption reply/idle proves `timed_out`; ambiguous interrupt
delivery becomes `unknown`.

## Notebook cells and batches

Notebook execution resolves the selected cell with `nbformat`, verifies an
optional exact-source hash, and includes path identity, cell ID/index, and
source SHA-256 in provenance. The source bytes are captured before the durable
queue operation, so later file edits cannot change what the kernel receives.

A batch reserves one parent UUID and one child execution UUID per selected
cell. The parent and every queued child are committed atomically before the
kernel FIFO receives one batch work item. The worker applies the normal
dispatch/proof lifecycle to each child in order. By default, its first
non-success terminal child stops the batch and marks later queued children
`interrupted` with `batch_stopped`; `--continue-on-error` dispatches them.
Cancellation uses each child's existing queued/running cancellation contract.
On restart, active parent membership prevents children from being submitted as
unrelated executions.

## Verification

Deterministic tests cover both evidence orderings, missing/malformed/mismatched
proof, silent success, prior stdout plus exception metadata, dispatch commit
before send, one-send idempotency, FIFO/concurrent workers, pre/post-confirm
disconnect, queued/running cancellation, completion races, and deadline start.

`integration/repro_durable_execution/test.sh` allocates one live CPU runtime
and verifies silent proof, controlled exception exit/output, detached
cross-process observation, idempotent retry, and persistent state across
executions. It stops the controller and runtime, then requires the live session
list to be empty.

`integration/repro_health_recovery/test.sh` validates the raw nonce probe on a
live CPU kernel, starts one state-mutating long execution, hard-kills the
controller after confirmation, elects a replacement, and waits for proof-safe
reconciliation. The observed restart intentionally resolved to `unknown`
because terminal messages crossed the observation gap; output remained
incomplete. A subsequent execution read the kernel counter as exactly one,
proving the original source was not replayed. The test re-probes readiness,
stops the assignment, and requires an empty live session list.

`integration/repro_notebook_batches/test.sh` reuses one live CPU kernel to
verify stateful guarded cell execution, explicit output writeback, stop and
continue batch policies, undispatched-child isolation, stale-edit rejection,
guarded ID assignment, and final assignment cleanup.
