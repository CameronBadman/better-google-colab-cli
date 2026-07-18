---
title: Durable compatibility wrappers
status: implemented
last_updated: 2026-07-18
change_log:
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
owning the same kernel simultaneously. Acquisition fails while durable work
is active. Release reconnects and nonce-probes unless the operation is session
stop, which releases without reconnecting after unassignment.

The deterministic suites cover surface isolation, typed routing, output/error
exits, script argument and `SystemExit` behavior, requirement embedding, and
lease cleanup. `integration/repro_compatibility_wrappers/test.sh` reuses one
live CPU assignment across flat session creation, piped REPL, exec success and
failure, `run --keep`, install, status, and stop. It finishes by checking both
durable local state and the Colab assignments endpoint for zero sessions.
