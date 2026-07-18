---
name: better-colab
description: Operate durable Google Colab sessions through the better-colab CLI. Use for remote Python execution, detached jobs, bounded output retrieval, guarded notebook-cell work, artifacts, or session cleanup.
---

# Better Colab

Use the shell and the `better-colab` executable. Treat its compact JSON as the
contract; stdout contains one object and diagnostics use stderr.

## Discover and verify

Start every workflow with:

```sh
better-colab doctor --format json
better-colab capabilities --format json
```

If syntax is uncertain, scope discovery instead of guessing:

```sh
better-colab capabilities execution.start --format json
```

Read `ok`, then either `result` or `error.code`, `error.retryable`, and
`error.suggested_action`. Do not parse human terminal output.

## Allocate explicitly

Choose a stable session name and allocate only through:

```sh
better-colab session ensure NAME --format json
```

Add `--gpu GPU` or `--tpu TPU` only when requested. `execution start` never
allocates a runtime. Use `session status NAME` for stored state and
`session probe NAME` when live kernel readiness matters.

## Execute safely

For a common attached call:

```sh
better-colab execution start --session NAME --file PATH \
  --idempotency-key KEY --format json
```

Code may instead arrive on stdin. Use a stable, operation-specific key and
reuse it only with the identical request. A retry with that key returns the
same execution; changed input produces `IDEMPOTENCY_CONFLICT`.

For long work, add `--detach`. Record the returned execution UUID, then:

```sh
better-colab execution wait EXECUTION_ID --timeout 60 \
  --max-bytes 65536 --format json
```

A wait with exit 124 is only an observation (`wait_timed_out:true`); it does
not cancel or alter execution. Attached terminal user-code failure exits 1.
`execution status` remains observational. Never replay an execution whose
state is `unknown`; inspect it and report the uncertainty.

`wait` includes the first bounded output page. When `has_more` is true, pass
the returned `next_cursor` unchanged:

```sh
better-colab execution output EXECUTION_ID --cursor CURSOR \
  --max-bytes 65536 --format json
```

Repeat until `has_more` is false. An artifact event provides a protected path,
media type, byte size, and `sha256:<hex>`; verify the checksum before consuming
or moving it. Use `execution cancel EXECUTION_ID` only when cancellation is
intended.

## Guard notebook cells

Inspect before mutation:

```sh
better-colab notebook cells NOTEBOOK --format json
better-colab notebook cell NOTEBOOK --cell-id ID --format json
```

Cell IDs are scoped to the notebook path. If IDs are missing, first obtain the
notebook hash, then run `notebook ids assign` with
`--expected-notebook-sha256`. Never infer an ID or select a duplicate.

Execute the inspected source with both its selector and guard:

```sh
better-colab execution start --session NAME --notebook NOTEBOOK \
  --cell-id ID --expected-source-sha256 HASH \
  --idempotency-key KEY --format json
```

For edits, always send replacement source by `--file PATH` or stdin and keep
the inspection guard:

```sh
better-colab notebook update NOTEBOOK --cell-id ID \
  --file SOURCE --expected-sha256 HASH --format json
```

On a hash conflict, re-inspect instead of overwriting. Output writeback is
never implicit; use `notebook write-output EXECUTION_ID` only when explicitly
requested and its guards pass.

## Finish and recover

Observe or cancel outstanding work before releasing its runtime:

```sh
better-colab session stop NAME --format json
better-colab session list --format json
```

Confirm the session is gone. Keep terminal execution records by default;
`execution prune` is dry-run unless explicitly confirmed. If an error is
retryable, follow its suggested action and retry the same canonical request
with the same idempotency key. For unfamiliar recovery paths, query scoped
capabilities rather than improvising.
