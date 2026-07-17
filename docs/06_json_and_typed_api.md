---
title: JSON v1 and typed Python API
status: implemented
last_updated: 2026-07-17
change_log:
  - date: 2026-07-17
    summary: Added typed notebook inspection/mutation/writeback and parent/child batch start/status/wait/cancel operations.
  - date: 2026-07-17
    summary: Added typed session status/probe results whose JSON form always retains the seven mandatory health fields.
  - date: 2026-07-17
    summary: Extended typed output pages with normalized stream, MIME, display, error, metadata, and immutable artifact records while preserving bounded cursor responses.
  - date: 2026-07-17
    summary: Added typed execution start/status/wait/output/cancel/list operations and their observation-versus-attached exit behavior.
  - date: 2026-07-17
    summary: Added schema-v1 envelopes, public models, capability pagination, response caps, doctor, and legacy JSON adapters.
---

# JSON v1 and typed API

The `better_colab` package is the public synchronous API. It currently exposes
`BetterColabClient`, strict immutable result models, `BetterColabError`, and
the stable `ExitCode` enum. It contains no terminal rendering; Typer command
functions adapt returned models at the outermost boundary.

## Envelope and exit contract

Success has exactly three top-level fields:

```json
{"schema_version":1,"ok":true,"result":{}}
```

Failure replaces `result` with an error containing `code`, `message`,
`retryable`, and `suggested_action`. Optional structured details are omitted
when absent. JSON is compact UTF-8 with one trailing newline. The serialized
response—including errors and that newline—cannot exceed 262,144 bytes. When a
result or remote error is too large, the renderer substitutes the bounded
`RESPONSE_TOO_LARGE` error.

Exit codes are `0` for successful operations/observations, `1` for attached
execution failure, `2` for usage, `3` for authentication/permission, `4` for
not found, `5` for conflict, `6` for retryable unavailability, and `124` for an
observational wait timeout.

## Capability discovery and cursors

`better-colab capabilities --format json` returns supported schema/internal
protocol versions, byte/page limits, and at most 20 concise command schemas by
default. `--cursor` reads the next stable page and `--limit` is bounded to
1–100. Supplying a scoped name such as `execution.start` returns only that
command. Drive commands are absent by construction.

Cursors are versioned short strings. They are intentionally opaque at the
public boundary: consumers persist and return them but never derive or edit
them. Reusing a cursor returns the same page.

## Doctor

`better-colab doctor --format json` reports paths, package/Python/platform
versions, and the seven mandatory health fields. Before the controller
milestone it reports only passive local socket evidence and makes no
authentication or backend call. It also skips legacy file logging, so
capability discovery and doctor work in read-only/sandboxed homes.

`BetterColabClient.session_status(name)` and `session_probe(name, timeout=...)`
return `SessionHealthResult`. The matching `better-colab session status|probe`
commands use the same model. Unlike optional fields elsewhere in compact JSON,
the following are always serialized: `controller_alive`, `backend_alive`,
`kernel_connected`, `kernel_execution_ready`, `kernel_probe_at`,
`kernel_probe_latency_ms`, and `kernel_probe_error`.

## Flat-command migration

The retained `new`, `sessions`, `status`, `restart-kernel`, `stop`, `ls`, `rm`,
`upload`, `download`, and `install` commands accept `--format json` on both
console surfaces. Text remains their default. JSON mode:

- writes one envelope and no progress text to stdout;
- never returns runtime URLs or bearer tokens;
- structures file listings rather than parsing display lines;
- suppresses remote installer stream rendering;
- maps missing sessions/files and remote failures to stable exit classes;
- applies the same hard response cap as agent-native commands.

Interactive edit, REPL, console, VM auth, and compatibility-only Drive
operations deliberately remain outside this adapter.

## Durable execution models

The synchronous client now implements execution start, status, wait, output,
cancel, and list. `ExecutionResult`, `ExecutionWaitResult`,
`ExecutionListResult`, and `OutputPage` are shared directly with JSON mode;
the typed layer has no Typer, Rich, or terminal-rendering dependency.

`OutputPage` contains ordered `OutputEvent` values. Text is bounded and
cursor-readable; binary and large MIME representations contain an `Artifact`
with a protected local path, media type, byte size, and `sha256:<hex>`.
Large complete text output is also emitted as an artifact event after its
cursor-readable chunks. Stream names, MIME types, display IDs/counts and
metadata, clear-output behavior, and structured error fields are retained.

Execution status is an observation and returns normally even for `error` or
`unknown`. Attached start/wait still returns the typed observation but the CLI
exits 1 for a proven terminal failure. Caller wait timeout returns current
state with `wait_timed_out=true` and the CLI exits 124; it is not an exception
and does not request cancellation.

## Notebook and batch models

`NotebookCellsResult` provides bounded metadata pages without source or
outputs. `NotebookCell` adds exact source for one path-namespaced cell.
`NotebookIdsResult` and `NotebookWriteResult` report explicit guarded
mutations. The synchronous client exposes the same inspect, update, ID
assignment, execution-selection, and output-writeback behavior as the CLI.

`BatchResult` contains the parent UUID, policy, state, and ordered child
`ExecutionResult` values. `BatchWaitResult` adds the observational
`wait_timed_out` field. Attached terminal batch failure exits 1 while
status remains an exit-0 observation; wait timeout exits 124 without
cancelling children.

## Testing strategy

- Invoke the CLI through Typer and parse exactly one stdout line.
- Call the same operations through `BetterColabClient` and assert typed models.
- Exercise stable not-found/usage errors and exit codes.
- Page/reuse cursors and reject malformed/out-of-range values.
- Serialize oversized successes and errors and assert the hard cap.
- Assert all seven health fields survive null/default omission.
- Assert notebook and batch CLI results round-trip through the public models.
- Run retained upstream tests to protect text-mode compatibility.
