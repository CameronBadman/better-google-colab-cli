---
name: better-colab
description: Operate durable Colab sessions via better-colab. Use for remote execution, detached jobs, bounded output, guarded notebook cells, artifacts, or cleanup.
---

# Better Colab

Use the shell and `better-colab`; stdout is one JSON object and
diagnostics use stderr.

## Discover and verify

Start with:

```sh
better-colab doctor --format json
better-colab capabilities --format json
```

When syntax is uncertain, scope discovery:

```sh
better-colab capabilities execution.start --format json
```

Read `ok`, then `result` or `error.code`, `retryable`, and `suggested_action`.

## Allocate explicitly

Choose a stable name:

```sh
better-colab session ensure NAME --format json
```

Only `session ensure` allocates. Add `--gpu GPU` or `--tpu TPU` only when
requested. Use `session status NAME` for stored state and `session probe NAME`
for live kernel readiness.

## Execute safely

For a common attached call:

```sh
better-colab execution start --session NAME --file PATH \
  --idempotency-key KEY --format json
```

Code may arrive on stdin. Retry an identical request with its stable key;
changed input produces `IDEMPOTENCY_CONFLICT`.

For long work, add `--detach`, record `result.execution_id`, then:

```sh
better-colab execution wait EXECUTION_ID --timeout 60 \
  --max-bytes 65536 --format json
```

A wait with exit 124 (`wait_timed_out:true`) only observes; repeat without cancelling.
`finished` succeeds. `error`, `interrupted`, `timed_out`, and `unknown` do not;
attached start/wait exits 1 on observed failure, while status is observational.
Never replay `unknown`. For exceptions, retain prior output and inspect:

```sh
better-colab execution status EXECUTION_ID --include traceback --format json
```

Corrected source is a new request and needs a new key.

`wait` includes its first page at `result.output`: when
`result.output.has_more` is true, pass `result.output.next_cursor` unchanged.
Later pages use `result.has_more` and `result.next_cursor`:

```sh
better-colab execution output EXECUTION_ID --cursor CURSOR \
  --max-bytes 65536 --format json
```

Repeat until false; never derive opaque cursors. Find artifacts at
`result.output.events[].artifact` after wait and `result.events[].artifact`
later. Paths are local immutable files retained after stop. Match
`sha256sum PATH` (Linux) or `shasum -a 256 PATH` (macOS) to `sha256:<hex>`
before reading or copying.
Use `execution cancel EXECUTION_ID` only when intended.

## Guard notebook cells

Notebook and source paths are local. Inspect before mutation:

```sh
better-colab notebook cells NOTEBOOK --format json
better-colab notebook cell NOTEBOOK --cell-id ID --format json
```

The list exposes `result.notebook_sha256` and `result.cells[].source_sha256`;
one cell exposes `result.source_sha256`. IDs are path-scoped; never infer one
or select a duplicate. For missing IDs:

```sh
better-colab notebook ids assign NOTEBOOK \
  --expected-notebook-sha256 NOTEBOOK_HASH --format json
```

Execute exactly the inspected cell:

```sh
better-colab execution start --session NAME --notebook NOTEBOOK \
  --cell-id ID --expected-source-sha256 SOURCE_HASH \
  --idempotency-key KEY --format json
```

For edits, always preserve the inspected source guard:

```sh
better-colab notebook update NOTEBOOK --cell-id ID \
  --file SOURCE --expected-sha256 SOURCE_HASH --format json
```

On conflict, re-inspect rather than overwrite. After an edit, re-inspect and
execute with the new hash. Writeback is never implicit; use `notebook
write-output EXECUTION_ID` only when explicitly requested for a finished/error
execution with complete output and unchanged source.

## Finish and recover

Observe or cancel outstanding work before release:

```sh
better-colab session stop NAME --format json
better-colab session list --format json
```

Confirm absence. Keep terminal records/artifacts; `execution prune` is dry-run
unless confirmed. Follow retryable errors' `suggested_action`, preserving a
key only for the same request. Query scoped capabilities for recovery.
