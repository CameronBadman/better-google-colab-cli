---
title: Session health and proof-safe recovery
status: implemented
last_updated: 2026-07-17
change_log:
  - date: 2026-07-17
    summary: Added raw nonce readiness, connection-scoped health caching, durable restart reconciliation, no-replay reconnect, hard-death election, and live verification.
---

# Session health and proof-safe recovery

`better-colab session status NAME --format json` passively observes the
controller-owned connection. `session probe` is the explicit active check.
Both it and the synchronous `BetterColabClient` return the seven mandatory
health fields even when their values are false or null:

```text
controller_alive
backend_alive
kernel_connected
kernel_execution_ready
kernel_probe_at
kernel_probe_latency_ms
kernel_probe_error
```

## Kernel readiness

The worker serializes a readiness request through the same per-kernel FIFO as
durable execution. The adapter builds a raw `execute_request` with
`store_history=false` and a random nonce in `user_expressions`. Readiness
requires all of:

- a shell `execute_reply` with the matching parent message ID and `ok` status;
- the exact nonce in the named user-expression result; and
- a matching IOPub `status: idle`.

Malformed, missing, mismatched, or error results are reported as health errors.
The result is cached only against the current controller connection ID, kernel
ID, and Jupyter session ID. Closing a transport or restarting the controller
invalidates that evidence.

## Restart reconciliation

Startup examines every active durable record before queueing new work:

- unconfirmed `dispatching` becomes `unknown`;
- complete durable reply-plus-idle proof becomes its proven terminal state;
- confirmed `running` becomes `disconnected`; and
- existing `disconnected` work remains recoverable.

Every observation gap permanently sets `output_complete=false`. Recovery is
eligible only when the current session endpoint, kernel ID, and Jupyter session
ID exactly match the dispatch snapshots. A missing session or changed identity
becomes `unknown`.

The original execute request is never reconstructed or resent. Its source was
discarded when dispatch was confirmed. Instead, the worker restores persisted
proof and sends one new `kernel_info_request` as an observation boundary. Late
original-parent messages can finish the original proof. If the new request
receives both its matching reply and idle while original proof remains
incomplete, the execution becomes `unknown`. Reconnect, idle, empty output, or
one half of terminal proof never implies success.

Persisted cancellation and execution deadlines remain effective during
recovery. A terminal interrupt state still requires matching original
reply/idle evidence; ambiguous interrupt delivery remains unknown.

## Controller death

An orderly stop refuses active work. A forced stop records uncertainty before
closing. `SIGKILL` cannot journal anything, so the next client uses the
startup-election lock and released lifetime lock to remove the stale socket
and elect exactly one replacement. That replacement invalidates health caches,
reconciles recovery work before queued work, and uses the normal kernel FIFO.

## Verification

Deterministic tests cover nonce proof, cache invalidation, durable
reply/idle recovery, ambiguous dispatch, no original send during reconnect,
same-kernel idle boundaries, startup ordering, permanent output gaps, and an
eight-client replacement race after `SIGKILL`.

The live non-Drive test in `integration/repro_health_recovery/test.sh`:

1. allocates one CPU session and validates the nonce mechanism;
2. starts a long state-mutating execution and waits for confirmed running;
3. kills the controller, starts a replacement, and observes proof-safe
   recovery;
4. verifies the kernel-side run counter is exactly one;
5. validates readiness on the replacement connection; and
6. stops the assignment and requires an empty session list.

The live recovery resolved to `unknown`, which is the intended safe result when
the controller missed terminal messages. It did not replay the source or infer
success from kernel idleness.
