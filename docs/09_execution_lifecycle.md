---
title: Durable execution lifecycle
status: implemented
last_updated: 2026-07-17
change_log:
  - date: 2026-07-17
    summary: Required output spool finalization and hashing before every terminal transition, with rich output normalized into durable cursor records.
  - date: 2026-07-17
    summary: Added the pinned low-level kernel adapter, per-kernel FIFO workers, exact-once dispatch boundary, matching reply/idle proof, idempotent start, condition waits, cancellation, deadlines, and the first live execution suite.
---

# Durable execution lifecycle

`better-colab execution start|status|wait|output|cancel|list` and the matching
`BetterColabClient` methods are implemented. `start` accepts exact UTF-8 file
or stdin source; notebook selection is completed with the guarded notebook
milestone. It never allocates a runtime. The named session must already exist
in the selected profile.

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
becomes `disconnected` and permanently sets `output_complete=false`. Reconnect
and restart reconciliation are extended by the recovery milestone.

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
