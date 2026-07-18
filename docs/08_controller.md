---
title: Local controller and startup election
status: implemented
last_updated: 2026-07-18
change_log:
  - date: 2026-07-18
    summary: Added a lazy machine-status entry point and benchmarked cold readiness, warm socket RPCs, warm execution-status CLI latency, and compact response bytes.
  - date: 2026-07-17
    summary: Added parent-batch condition topics and short-lived query-only SQLite metadata snapshots so stale WAL readers cannot poison controller handshakes.
  - date: 2026-07-17
    summary: Added startup health-cache invalidation, durable recovery-before-queue scheduling, same-kernel reconnect workers, and hard-kill replacement coverage.
  - date: 2026-07-17
    summary: Added execution RPCs, persistent per-kernel blocking workers/FIFOs, thread-safe condition publication, queued-work resume, and queued-work stop protection.
  - date: 2026-07-17
    summary: Added the framed Unix protocol, single-instance locks, detached autostart, profile RPCs, condition waits, lifecycle commands, and forced-stop reconciliation.
---

# Local controller and startup election

Better Colab runs one persistent controller per OS user. It owns long-lived
kernel transports and serves all configuration/auth profiles over a
private Unix-domain socket.

## Runtime paths and permissions

The controller uses `$XDG_RUNTIME_DIR/better-colab` when available; otherwise
it uses `$TMPDIR/better-colab-$UID`. The directory is mode `0700`.

- `controller.sock`: mode `0600` Unix socket
- `controller.pid`: mode `0600` observed PID
- `controller.log`: mode `0600` detached-process diagnostics
- `controller.lock`: mode `0600` lifetime lock
- `startup.lock`: mode `0600` election lock

The lifetime lock is held from before socket bind until cleanup. Startup
clients first connect optimistically. On failure they contend for the election
lock, recheck the handshake, and inspect the lifetime lock. Only an election
winner that proves the lifetime lock free may unlink a stale socket/PID and
spawn a detached controller. Losers recheck after acquiring the election lock
and connect to the winner.

## Internal protocol

Every frame is:

```text
4-byte unsigned big-endian length | compact UTF-8 JSON object
```

The maximum body is 16 MiB. The reader rejects an oversized declared length
without waiting for its body. Requests contain `protocol_version`,
`request_id`, `method`, and object `params`; responses repeat the version and
request ID and contain either `result` or a stable error. A mismatch never
causes automatic controller replacement.

Implemented methods cover handshake/status, profile registration/session
listing, execution start/status/wait/output/cancel/list, batch
start/status/wait/cancel, condition wait/notification, and controller stop.
Profile RPCs accept the normalized config/auth/OAuth inputs and filter every
response to that profile. Tokens and backend URLs are never returned.

Controller-wide profile discovery and active-execution counts open a fresh
query-only SQLite snapshot for each operation. This keeps kernel connections
persistent without retaining a WAL reader across unrelated worker commits.
Live testing caught and now covers the prior failure mode in which such a
reader returned `SQLITE_NOTADB` even though a fresh integrity check succeeded.

## Waits

A wait request subscribes to a named in-memory condition revision. If the
revision is not newer than the caller's cursor, the server holds that request
on `asyncio.Condition.wait_for`. Notification increments the revision, stores
the bounded payload, and wakes waiters. Timeout returns one response with
`wait_timed_out:true`; it does not mutate any durable record.

Execution workers publish every durable change onto a profile/execution topic.
An execution wait checks state while holding that topic's condition, then
releases the condition only inside `wait`; this closes the lost-wakeup window
without database polling. The same mechanism is the foundation for batch
waits, which subscribe to a separate profile/parent-batch topic.

## Kernel workers

The controller owns one persistent transport and blocking worker thread per
profile/session kernel. Each worker is the sole consumer of the pinned
client's shell and IOPub queues and drains a FIFO. Different session workers
run concurrently. The asyncio server remains responsive while remote
connection, execution, and interrupt calls block in their owner threads.

The worker publishes state back to the event loop with
`call_soon_threadsafe`; controller wait responses are therefore server-pushed.
Controller startup invalidates stale connection health, reconciles
already-dispatched records from durable evidence, schedules confirmed
reconnects first, and then submits durably safe queued work. Recovery and new
execution for one kernel therefore share the same FIFO and queue consumer.

## Lifecycle safety

`better-colab controller status` is passive. `controller start` performs
election/autostart. `controller stop` waits for socket shutdown.

Normal stop refuses when durable state contains `created`, `queued`,
`dispatching`, `running`, or `disconnected` work. `--force` first sets
`output_complete=false` and journals
affected work to `unknown`. A running record passes through `disconnected` so
the public state graph remains valid. Only then does the server close its
listener, clients, database connection, PID file, socket, and lifetime lock.

Signals use the same forced-uncertainty path. A hard process death cannot run
cleanup; the next election sees the released OS lock, removes the stale socket,
and starts one replacement. Tests kill the detached controller with `SIGKILL`
and race eight clients; every client observes the same newly elected PID.

## Testing and benchmark strategy

- Round-trip frames; reject oversized/non-object input.
- Start a real detached subprocess and inspect socket/PID modes.
- Race 12 clients and assert one spawn and one PID.
- Preserve a stale socket while a test process holds the lifetime lock.
- Reject protocol mismatch without replacing the original PID.
- Wake a blocking condition request from a second connection and time out
  another without polling.
- Route same-named sessions in OAuth2 and ADC profiles independently.
- Refuse normal stop with active work; force and inspect transition journal.
- Report cold election/readiness and warm RPC p95 without flaky CI thresholds.

The executable uses a lazy machine-status entry point for the exact
`execution status ... --format=json` observation path. It parses only the
global profile flags and named expansions needed for that command, then uses
the same typed client, controller RPC, error mapping, and schema-v1 renderer.
All other invocations fall through to the complete Typer command graph. The
public `better_colab` facade also resolves exports lazily, and notebook support
loads `nbformat` only when a notebook operation is requested.

`integration/repro_controller/test.sh` reports timing without flaky CI
thresholds. On the 2026-07-18 reference run it measured:

```json
{"cold_controller_ready_ms":228.141,"warm_rpc_p95_ms":0.358}
{"warm_execution_status_cli_p95_ms":125.812,"execution_status_response_bytes":377}
```

These results meet the targets of sub-second cold readiness, warm socket RPC
p95 below 10 ms, warm status CLI p95 below 150 ms, and status responses below
2 KiB.
