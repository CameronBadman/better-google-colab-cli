---
title: Better Colab agent-first architecture
status: in-progress
schema_version: 1
last_updated: 2026-07-17
change_log:
  - date: 2026-07-17
    summary: Implemented the typed Python facade, JSON v1 envelopes, bounded capability discovery, cursor primitives, and flat-command JSON adapters.
  - date: 2026-07-17
    summary: Established the unofficial fork identity, distribution boundary, and durable-interface architecture.
---

# Better Colab design

Better Colab is an unofficial Apache-2.0 fork of Google Colab CLI. The fork
keeps upstream history, attribution, the `colab_cli` implementation package,
and practical command compatibility. Its primary products are the
`better-google-colab-cli` distribution, the `better-colab` executable, and the
synchronous `better_colab.BetterColabClient` API.

An optional `better-google-colab-cli-compat` distribution is version-locked to
the core package and installs the historical `colab` executable. The core
wheel never installs `colab`. Drive support is confined to that compatibility
surface.

## Design goals

The common path must work for a generic automation agent with only shell
access and one compact, vendor-neutral skill. Agent latency, command round
trips, serialized bytes, and context usage are product budgets. The interface
therefore uses compact schema-versioned JSON, stable error codes, durable
idempotency, bounded cursor pages, and explicit opt-in expansion.

The implementation must never claim execution success from incomplete
evidence. It records the exact Jupyter message ID before sending, dispatches
once, and requires both the matching `execute_reply` and matching IOPub
`status: idle`. Disconnects and recovery gaps are represented explicitly
rather than hidden behind replay or optimistic inference.

## Public boundary

The primary command groups are:

- `capabilities` and `doctor`
- `session ensure|list|status|probe|stop`
- `execution start|status|wait|output|cancel|list|prune`
- `execution batch start|status|wait|cancel`
- `notebook cells|cell|update|ids assign|write-output`

All machine-facing commands support `--format json` and write exactly one
compact object to stdout:

```json
{"schema_version":1,"ok":true,"result":{}}
```

Errors use the same envelope and include a stable code, human message,
retryability, and a suggested action. Serialized responses, including their
trailing newline, are capped at 262,144 bytes. Output pages default to 65,536
bytes. Execution lists default to 20 records; notebook-cell lists default to
100; collection pages never exceed 100 items.

The implemented JSON v1 foundation lives in `better_colab.models`,
`better_colab.protocol`, and `better_colab.errors`. Models are strict,
immutable Pydantic values. Their wire form omits optional null/default fields,
except for the seven session health fields, which are always present.
`better-colab capabilities` is scoped and cursor-paged; it reports this
contract and the complete planned durable command vocabulary without embedding
a long manual in an agent skill. `better-colab doctor` is side-effect-free and
does not initialize logging, authentication, controller state, or a network
client.

During migration, retained non-interactive flat session, file, and install
commands also accept `--format json`. Their default remains human-readable
text for upstream compatibility. JSON mode suppresses progress and kernel
installer output, omits protected runtime tokens, and routes errors through
the same stable envelope and exit-code mapping.

The typed Python layer returns the same public models without importing Typer,
Rich, or terminal rendering. `BetterColabClient.capabilities()` and
`BetterColabClient.doctor()` are the first implemented operations; durable
session/execution/notebook methods are added with their corresponding
controller milestones. `execution start` never allocates a runtime:
`session ensure` is the only compound operation allowed to allocate.

## Controller

One long-lived per-user controller owns persistent kernel connections and
serves local clients over a mode-`0600` Unix socket. It uses a versioned,
length-prefixed JSON protocol with request IDs and a 16 MiB request limit.
An `asyncio` server coordinates one dedicated blocking worker and FIFO per
kernel because the pinned `jupyter-kernel-client` transport is synchronous.
Different kernels may execute concurrently.

The socket lives under `$XDG_RUNTIME_DIR/better-colab`, falling back to a
UID-owned mode-`0700` directory under `$TMPDIR`. A lifetime lock distinguishes
the active controller; a separate election lock serializes startup and stale
socket removal. Foreground clients perform interactive OAuth bootstrap. A
detached controller consumes cached credentials only and returns
`AUTH_INTERACTION_REQUIRED` when consent is needed.

Waits are condition-driven controller requests rather than CLI status polling.
Normal controller stop refuses while work is active. Forced stop marks affected
executions uncertain. Interactive legacy commands take an exclusive session
lease and temporarily release the controller's kernel connection.

## Durable state

SQLite is authoritative and uses WAL, foreign keys, `busy_timeout`,
`synchronous=FULL`, explicit transactions, and `PRAGMA user_version`. It is
stored at `${XDG_STATE_HOME:-~/.local/state}/better-colab/controller.sqlite3`;
protected output/source spools and immutable artifacts are siblings under
`artifacts/`.

The schema records profiles, sessions, executions, append-only transitions,
batches, ordered output chunks, artifacts, and kernel-connection evidence.
Profile identity incorporates normalized config path, authentication provider,
and OAuth configuration path.

On first access, the matching upstream `sessions.json` is imported
non-destructively and retained as a backup. The import hash and timestamp are
recorded; later legacy-file changes produce a diagnostic, never an automatic
second import.

## Execution proof and recovery

The durable state graph is:

```text
created -> queued
queued -> dispatching | interrupted
dispatching -> running | unknown
running -> finished | error | interrupted | timed_out | disconnected
disconnected -> running | finished | error | interrupted | timed_out | unknown
```

The controller prepares the exact Jupyter request and message ID, commits
`dispatching`, then sends once. The first matching inbound message confirms
dispatch. Reply and idle may arrive in either order; both are required.
Output or idle alone, malformed replies, mismatched parents, and reconnect
idleness never imply completion.

A pre-confirmation disconnect becomes terminal `unknown` and is never replayed.
A confirmed disconnect remains recoverable using the same kernel and Jupyter
session identities, but any observation gap permanently makes
`output_complete=false`. On restart, only `created` and `queued` work can
resume. Ambiguous dispatch becomes `unknown`; confirmed running work reconnects
without replay.

Idempotency keys hash the canonical request. Identical reuse returns the
existing execution; different reuse returns `IDEMPOTENCY_CONFLICT`. A wait
timeout is observational, exits 124, and never mutates or cancels work.
Execution deadlines begin only after confirmed running and become `timed_out`
only after a verified interrupt.

## Output, notebooks, and batches

Text output is appended to an execution-local spool and indexed transactionally
by cursor. Binary and large MIME payloads become atomic immutable artifacts
with byte size, media type, and SHA-256. Terminal transitions occur only after
spool fsync and hashing. Cursor reuse is stable, and advancing never duplicates
bytes.

Notebook access uses `nbformat` exclusively. Notebook identity is the SHA-256
of its canonical resolved absolute path; cell identity is always namespaced by
that notebook and its cell ID. Reads never mutate. Missing IDs require an
explicit, notebook-hash-guarded assignment command. Cell edits are atomic and
source-hash guarded. Output writeback is an explicit operation requiring the
original notebook identity, source hash, complete output, and a terminal
finished/error execution.

Batches create one durable child execution per selected cell. They stop on the
first error unless `continue_on_error` is selected; undispatched children
become interrupted with reason `batch_stopped`.

## Validation strategy

Every behavior begins with a failing deterministic test. Conformance tests pin
all private kernel-client access to one transport adapter. Crash-boundary tests
cover each durable transition. Socket-election tests exercise concurrent
startup, profile isolation, and controller replacement. Output tests assert
caps, stable cursors, checksums, and no duplication. Notebook tests cover path
namespacing, missing/duplicate IDs, stale hashes, and guarded writeback.

Live non-Drive integration tests reuse one CPU assignment where possible and
cover silent success, controlled exceptions, detach/observe, controller
restart, large output, readiness probes, and stateful notebook cells. Every
live run ends by listing sessions, directly unassigning orphans, and verifying
that no assignment remains.
