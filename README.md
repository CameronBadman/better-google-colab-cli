# Better Colab (Unofficial)

**Durable, agent-operated compute on Google Colab.**

Better Colab gives Codex and other shell-capable agents a durable control plane
over transient Colab runtimes: start work once, detach, reconnect, page through
bounded output, verify artifacts, and change notebook cells without unsafe
replay or overwrite.

> [!IMPORTANT]
> Better Colab CLI is an independent, unofficial fork of
> [Google Colab CLI](https://github.com/googlecolab/google-colab-cli). It is
> not affiliated with or endorsed by Google. Google Colab is a Google service
> and remains subject to Google's terms, quotas, and availability.

## Why Better Colab?

Google's official Colab CLI brings runtime provisioning, remote execution, file
transfer, and headless automation to the terminal, including workflows driven
by AI agents. Its execution connection is owned by the invoking terminal
process, however. If that process exits, a later client has no durable
execution ID, status/wait lifecycle, idempotent retry contract, bounded output
cursor, or proof-aware way to distinguish “still running” from “unsafe to
replay.”

That distinction matters for coding agents, whose shell clients, context
windows, and supervising processes routinely change during long work. Better
Colab retains the useful upstream Colab implementation and adds a persistent
local controller, durable execution records, at-most-once dispatch, guarded
notebook operations, and compact machine-readable responses.

## The durable workflow

Start a named CPU session and queue one exact source snapshot:

```bash
better-colab session ensure trainer --format json

better-colab execution start \
  --session trainer \
  --file train.py \
  --idempotency-key training-v1 \
  --detach \
  --format json
```

Record `result.execution_id`. The initiating command has now exited; from a
fresh terminal or agent process:

```bash
better-colab execution wait EXECUTION_ID \
  --timeout 60 \
  --format json

better-colab session stop trainer --format json
```

Exit 124 means only that this observer's 60-second wait expired. It does not
cancel the execution; repeat the wait from any later process. Stop the session
after the execution reaches a terminal state.

## Google Colab CLI and Better Colab

This comparison describes the official CLI implementation inherited at this
fork's branch point, not the Google Colab service itself.

| Concern | Google Colab CLI | Better Colab |
| --- | --- | --- |
| Colab access | Runtime allocation, authentication, execution, files, REPL and console | Retains the upstream implementation and adds an agent-facing control plane |
| Execution owner | The invoking CLI process owns the kernel connection | A private per-user controller owns durable kernel connections |
| Work identity | No durable execution UUID or detached status/wait lifecycle | UUID-backed `start`, `status`, `wait`, `output`, `cancel`, and `list` |
| Retry behavior | A retry is a new invocation | Identical request + key returns the original execution; changed input returns `IDEMPOTENCY_CONFLICT` before dispatch |
| Client loss | Remote work may outlive the client, but later observation is not recoverable through an execution record | Fresh clients reconnect to the same record; ambiguous sends are never replayed |
| Output | Attached streaming/collection in one client | Stable bounded pages, opaque cursors, immutable large/MIME artifacts, size and SHA-256 metadata |
| Notebooks | Whole-notebook execution with an output copy | Read-only inspection, source-hash guards, selected cells, ordered batches, and explicit guarded writeback |

The core distribution intentionally installs only `better-colab`. A separate,
version-locked [compatibility distribution](compat/) can install the historical
`colab` executable for upstream-oriented workflows. Drive mounting remains on
that optional compatibility surface, not the Better Colab agent interface.

## Install and judge it

Requirements: Python 3.12 or newer, `uv`, Linux or macOS, a Google account with
Colab access, and available Colab quota.

The reproducible Build Week path installs the real package from the submission
branch rather than importing modules from the source tree:

```bash
git clone \
  --branch agent-first-interface \
  --single-branch \
  https://github.com/CameronBadman/better-google-colab-cli.git
cd better-google-colab-cli
uv tool install .

better-colab version
better-colab --help
better-colab doctor --format json
better-colab capabilities execution.start --format json
```

`doctor` and `capabilities` are passive: they do not authenticate, allocate a
runtime, or start the controller. The first live command uses OAuth2 by default
and, when no cached token exists, asks for one remote copy-and-paste consent
flow. ADC is also available through the global `--auth adc` option; see the
[authentication design](docs/04_automation_and_utility.md) for its required
scopes.

For a deterministic live check, save this as `train.py`:

```python
import time

print("better-colab:start", flush=True)
for step in range(1, 4):
    time.sleep(5)
    print(f"better-colab:progress:{step}", flush=True)
print("x" * 70_000)
print("better-colab:complete", flush=True)
```

Run the durable workflow above, close the initiating shell after `start`, and
wait from a fresh shell. The output exceeds the default 65,536-byte page and is
also promoted to a complete-text artifact. Follow `result.output.next_cursor`
unchanged while `result.output.has_more` is true:

```bash
better-colab execution output EXECUTION_ID \
  --cursor NEXT_CURSOR \
  --max-bytes 65536 \
  --format json
```

For output pages after the first, read `result.next_cursor` and continue until
`result.has_more` is false. Artifact records provide a protected local `path`,
`media_type`, `byte_size`, and `sha256` for independent verification.

After the terminal result and `session stop`, a disposable judge run can verify
that no session remains and stop its idle local controller:

```bash
better-colab session list --format json
better-colab controller stop --format json
```

Normal controller stop refuses active durable work. It does not prune terminal
execution history or artifacts.

## Execution safety

Better Colab is conservative at the boundaries where distributed execution
becomes ambiguous:

1. It writes and fsyncs the exact source snapshot before durable queueing.
2. A stable idempotency key hashes the canonical request. Same request means
   same execution; changed request means a conflict before another dispatch.
3. Before sending, the controller records the Jupyter message ID and
   `dispatching` state, then sends that request at most once.
4. Success requires both a structurally valid, matching `execute_reply` and a
   matching IOPub `status: idle`. Output or idleness alone is not success.
5. Recovery observes only the same endpoint, kernel, and Jupyter session. It
   never reconstructs and resends confirmed or ambiguously sent work.

If the evidence cannot prove a terminal result, the execution becomes
`unknown` and `output_complete` remains false. That is an intentional safety
result, not a synthetic success. Durably queued work can resume after a
controller restart; ambiguously dispatched work cannot.

Machine-facing commands emit one schema-v1 JSON object on stdout and send
diagnostics to stderr. Complete responses are capped at 262,144 bytes; output
pages default to 65,536 bytes and are traversed with stable opaque cursors.
Errors include a stable `code`, `retryable` flag, and `suggested_action`.

## Guarded notebook cells and batches

Inspection is read-only and excludes notebook outputs:

```bash
better-colab notebook cells analysis.ipynb --format json
better-colab notebook cell analysis.ipynb \
  --cell-id setup \
  --format json
```

Execute exactly the inspected source by returning its hash:

```bash
better-colab execution start \
  --session trainer \
  --notebook analysis.ipynb \
  --cell-id setup \
  --expected-source-sha256 SOURCE_HASH \
  --idempotency-key analysis-setup-v1 \
  --format json
```

Edits are atomic and can reject stale source instead of overwriting it:

```bash
better-colab notebook update analysis.ipynb \
  --cell-id setup \
  --file setup.py \
  --expected-sha256 SOURCE_HASH \
  --format json
```

Selected cells can run as one ordered durable batch:

```bash
better-colab execution batch start \
  --session trainer \
  --notebook analysis.ipynb \
  --cell-id setup \
  --cell-id train \
  --detach \
  --format json
```

Batches stop after the first failed child by default;
`--continue-on-error` is explicit. Reads and executions never mutate the source
notebook. Missing IDs require guarded `notebook ids assign`; output mutation
requires an explicit `notebook write-output EXECUTION_ID`, which refuses
changed source, incomplete output, or an unrelated notebook.

## Portable agent skill and Codex integration

The repository includes one compact, vendor-neutral
[Better Colab skill](.agents/skills/better-colab/SKILL.md). A generic agent
needs only shell access and the installed executable: the skill delegates exact
syntax to `doctor` and scoped `capabilities`, then teaches allocation,
idempotent execution, detached waits, cursor traversal, artifact verification,
notebook guards, and cleanup.

There is no hidden MCP dependency or provider-specific execution path. The
optional [Codex plugin](plugins/better-colab/) packages the same skill bytes as
a skill-only plugin—no MCP server, app, hook, Drive capability, or marketplace
mutation. See the [portable skill](docs/14_portable_skill.md) and
[Codex plugin](docs/15_codex_plugin.md) designs.

## Live validation evidence

Release validation on 2026-07-19 tested a non-editable installation of commit
`d65b9dcfa8f0ffc6c58ede5f13842feffc49d8a5` against a real CPU Colab runtime.
No README or design claim was accepted as evidence.

| Check | Observed result |
| --- | --- |
| Deterministic suite | 426 passed, 0 failed, 0 skipped in 17.53 seconds; Ruff passed |
| Detach and fresh client | Execution `56e36b5f-f77b-4339-a5ca-5e6d6d50cde6` finished after the initiating CLI exited; a short wait exited 124 without cancellation |
| Bounded output | 81,759 exact bytes traversed in 13 pages at an 8,192-byte budget, with no gaps or duplicates; start, progress, and completion markers appeared once |
| Artifacts | PNG and complete-text artifacts matched declared media type, byte size, and SHA-256 |
| At-most-once behavior | Dispatch count remained one across identical idempotent retry, observer loss, and recovery checks; changed source with the same key failed before dispatch |
| Client and controller loss | Killing only an attached waiting client did not cancel work; unexpected controller death produced honest `unknown`/incomplete evidence and did not replay the kernel-side effect |
| Failures and notebooks | Exception output, deadline, cancellation, large binary MIME, cursor reuse, stale notebook guards, writeback guards, and both batch error policies behaved as specified |
| Cleanup | Zero allocated test sessions; no validation controller process or socket remained |

No P0 correctness or P1 demo-reliability defect was found, so release
validation made no source-code fix.

## Built during OpenAI Build Week

Build Week work is the agent control plane, not a reinvention of Colab. It
added the `better-colab` product boundary and typed Python facade, schema-v1
JSON, profile-isolated SQLite durability, the local controller, proof-aware
execution and recovery, bounded output and artifacts, guarded notebook cells,
durable batches, the portable skill, the Codex plugin, and deterministic/live
integration coverage.

Runtime allocation, authentication, kernel transport foundations, file
operations, interactive workflows, source headers, and Git history come from
the official Google Colab CLI and remain visibly attributed.

## Built with GPT-5.6 Sol and Codex

Better Colab was developed as a human–agent engineering collaboration. The
maintainer set the product thesis, safety invariants, architecture constraints,
and acceptance bar. GPT-5.6 Sol and OpenAI Codex helped investigate the
upstream and Jupyter behavior, turn failure boundaries into deterministic
tests, implement reviewable milestones, inspect diffs, and run the live release
validation.

The collaboration was evidence-driven: generated explanations were not treated
as proof, ambiguous recovery was not relabelled as success, and the final
claims were checked against the installed package and an actual Colab runtime.

## Documentation

- [Architecture and public contracts](DESIGN.md)
- [JSON v1 and typed Python API](docs/06_json_and_typed_api.md)
- [Durable state and local controller](docs/07_durable_state.md)
- [Controller lifecycle](docs/08_controller.md)
- [Execution lifecycle](docs/09_execution_lifecycle.md)
- [Bounded output and artifacts](docs/10_bounded_output.md)
- [Health and proof-safe recovery](docs/11_health_and_recovery.md)
- [Guarded notebooks and batches](docs/12_notebooks_and_batches.md)
- [Compatibility wrappers](docs/13_compatibility_wrappers.md)
- [Fork identity and packaging](docs/00_fork_identity.md)
- [Upstream demo walkthroughs](docs/demos.md)

## Platform, licence, and contributing

Better Colab currently targets Linux and macOS with Python 3.12 or newer. The
durable controller uses a Unix-domain socket; native Windows is not supported.

The project is licensed under the [Apache License 2.0](LICENSE). It preserves
the upstream Apache-2.0 licence, Google source headers, attribution, upstream
links, and Git history. New fork-specific code uses the same licence.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the current contribution policy and
use the [Better Colab issue tracker](https://github.com/CameronBadman/better-google-colab-cli/issues)
for fork-specific defects and proposals.
