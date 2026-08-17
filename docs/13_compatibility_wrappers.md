---
title: Durable compatibility wrappers
status: implemented
last_updated: 2026-08-18
change_log:
  - date: 2026-08-18
    summary: Made retained interactive leases conditional on matching controller endpoint ownership, allowing legacy-only sessions to proceed while still protecting shared kernels.
  - date: 2026-08-15
    summary: Hardened retained auth, Drive, runtime, logging, and JSON compatibility state with private atomic storage, bounded coordination, hard deadlines, and secret-negative regression coverage.
  - date: 2026-07-18
    summary: Routed core flat sessions, execution, run, and install through typed durable state; added exclusive leases, explicit notebook output copies, compatibility isolation, and live CPU verification.
---

# Durable compatibility wrappers

Better Colab retains familiar flat commands without maintaining a second
agent execution contract. The executable selects the implementation boundary:

| Workflow | `better-colab` | Optional `colab` |
|---|---|---|
| `new`, `sessions`, `status`, `stop` | Typed SQLite session API | Upstream JSON/session backend |
| `exec`, piped `repl` | Durable controller execution | Direct `ColabRuntime` |
| `run` | Typed ensure → execution → stop | Direct allocate → runtime → unassign |
| `install` | Durable controller execution | Direct runtime/Contents API |
| TTY REPL, console, VM auth, Drive | Exclusive compatibility lease where retained | Exclusive compatibility lease |
| Drive | Not installed or documented | Retained |

`execution start` itself never allocates. Flat `new` is an explicit adapter to
`session ensure`; source execution requires an existing session. Attached
flat commands wait indefinitely at the controller boundary, render all bounded
cursor pages, and return nonzero for proven terminal user-code failures.

Notebook `exec` reads through `nbformat`. It executes code cells as durable
children and never mutates the input by default. `--write-output` creates and
targets an explicit `*_output.ipynb` copy. Missing IDs are not silently added
to the source notebook.

Session leases prevent the controller and retained direct transports from
owning the same kernel simultaneously. Before a retained interactive command
runs, its legacy session endpoint is compared with the durable sessions in the
same profile. A matching controller-owned endpoint is leased under its durable
name; a legacy-only endpoint proceeds without a controller lease because there
is no controller owner to release. This endpoint comparison also prevents a
stale same-name record for a different runtime from blocking the command.
Acquisition fails while durable work is active. Release reconnects and
nonce-probes unless the operation is session stop, which releases without
reconnecting after unassignment.

The retained compatibility surface treats local session JSON, OAuth tokens,
logs, and history as private data. Managed directories/files use `0700`/`0600`,
state writes are atomic under stable locks, malformed nonblank state fails
without reset, HTTP diagnostics exclude credential-bearing payloads, and
interactive auth/Drive replies are recorded only as redacted metadata.

The deterministic suites cover surface isolation, typed routing, output/error
exits, script argument and `SystemExit` behavior, requirement embedding,
legacy-only lease bypass, endpoint-matched lease ownership, and lease cleanup.
`integration/repro_compatibility_wrappers/test.sh` reuses one
live CPU assignment across flat session creation, piped REPL, exec success and
failure, `run --keep`, install, status, and stop. It finishes by checking both
durable local state and the Colab assignments endpoint for zero sessions.
