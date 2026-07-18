# Better Colab CLI (Unofficial)

> [!IMPORTANT]
> Better Colab CLI is an independent, unofficial fork of
> [Google Colab CLI](https://github.com/googlecolab/google-colab-cli). It is
> not affiliated with or endorsed by Google. Google Colab is a Google service
> and remains subject to Google's terms, quotas, and availability.

Better Colab is an agent-first command-line interface and synchronous Python
client for Google Colab. It keeps the useful upstream runtime, authentication,
and file-transfer implementation while adding durable execution records,
bounded machine-readable output, guarded notebook operations, and a persistent
per-user controller.

The project preserves the upstream Apache-2.0 licence, Google source headers,
attribution, and Git history. New fork-specific code is also Apache-2.0.

> [!NOTE]
> Linux and macOS are the initial targets. Windows and non-Unix controller
> transports are deferred.

## Status

The agent-first interface is implemented on the `agent-first-interface`
branch. JSON schema v1 and plugin version 0.1.0 are ready for review and
release. Retained flat commands are compatibility adapters over durable
behavior where documented in [DESIGN.md](DESIGN.md).

## Installation

The core distribution installs only `better-colab`:

```bash
uv tool install better-google-colab-cli
# or
pip install better-google-colab-cli
```

The optional, version-locked compatibility distribution installs only the
historical `colab` executable:

```bash
uv tool install better-google-colab-cli-compat
# or
pip install better-google-colab-cli-compat
```

The compatibility executable retains upstream-oriented workflows, including
the legacy `drivemount` command. Drive is deliberately absent from
`better-colab`, the typed API, capability discovery, agent skill, and plugin.

## Current command-line use

Provision a CPU runtime, execute code, and release it:

```bash
better-colab session ensure demo --format json
printf "print('Hello from Colab')\n" | \
  better-colab execution start --session demo \
    --idempotency-key readme-demo --format json
better-colab session stop demo --format json
```

Authentication for the CLI control plane is selected globally with
`--auth=oauth2|adc`. OAuth2 is the default and starts its remote copy-paste
consent flow automatically when no cached token exists. The separate hidden
VM-side `auth` command configures credentials inside an already-running
runtime; it is not a prerequisite for CLI authentication.

Update checks are explicit:

```bash
better-colab update
```

Normal commands never perform an update request or print an update banner.

Discover the compact machine contract or inspect passive local health:

```bash
better-colab capabilities --format json
better-colab capabilities execution.start --format json
better-colab doctor --format json
```

Retained non-interactive file commands and compatibility adapters also accept
`--format json`. Every JSON response uses schema version 1 and is hard-capped
at 262,144 bytes.

The familiar flat core workflows now share durable state: `new`, `sessions`,
`status`, and `stop` adapt the typed session API, while `exec`, piped `repl`,
`run`, and `install` dispatch through the controller. The optional `colab`
executable retains upstream direct-runtime compatibility. Notebook exec is
read-only unless `--write-output` explicitly requests an `*_output.ipynb`
copy.

Durable state uses a private profile-isolated SQLite database. Terminal history
is retained indefinitely unless pruning is explicitly confirmed:

```bash
better-colab execution prune \
  --before 2026-01-01T00:00:00Z \
  --format json
# Re-run with --confirm only after inspecting the dry-run result.
```

The persistent local controller autostarts for durable operations and can be
managed explicitly:

```bash
better-colab controller status --format json
better-colab controller start --format json
better-colab controller stop --format json
```

Normal stop refuses active durable work. `controller stop --force` is an
explicit recovery action that records affected work as uncertain.

Inspect the controller-owned connection passively or verify kernel execution
with a no-history nonce:

```bash
better-colab session status demo --format json
better-colab session probe demo --format json
```

Readiness is cached only for the current kernel connection. A disconnect or
controller restart invalidates it. Confirmed work reconnects only to collect
matching proof from the same endpoint/kernel/session identity; Better Colab
never replays an ambiguously sent request or infers success from idle alone.

Durable execution snapshots source before queueing, never allocates a runtime,
and waits for matching Jupyter reply plus idle proof:

```bash
better-colab new -s demo
printf 'print("durable")\n' |
  better-colab execution start \
    --session demo \
    --idempotency-key example-1 \
    --format json
better-colab stop -s demo
```

Attached waits include the first bounded output page. If it reports
`has_more:true`, pass its opaque `next_cursor` to
`better-colab execution output`. Binary and large MIME values—and large
complete text output—are returned as immutable artifacts with byte size,
media type, and SHA-256.

Use `--detach` to return after queueing, then pass the returned execution UUID
to `execution status` or `execution wait`. `--wait-timeout`/`wait --timeout`
only bound the caller and exit 124 without cancelling remote work.

Inspect and execute notebook cells without mutating the document:

```bash
better-colab notebook cells analysis.ipynb --format json
better-colab notebook cell analysis.ipynb --cell-id setup --format json
better-colab execution start \
  --session demo \
  --notebook analysis.ipynb \
  --cell-id setup \
  --expected-source-sha256 sha256:<inspected-hash> \
  --format json
```

Notebook edits are atomic and can be source-hash guarded. Missing cell IDs are
assigned only by the explicit notebook-hash-guarded `notebook ids assign`
command. `notebook write-output EXECUTION_ID` is the only in-place output
writeback and rejects changed source, incomplete output, or a different
notebook identity.

Run selected cells in one ordered parent batch:

```bash
better-colab execution batch start \
  --session demo \
  --notebook analysis.ipynb \
  --cell-id setup \
  --cell-id train \
  --format json
```

Batches stop after the first failed child by default; use
`--continue-on-error` explicitly when later cells should still run.

## Portable agent skill

The compact, vendor-neutral
[Better Colab skill](.agents/skills/better-colab/SKILL.md) teaches a generic
shell-capable agent the safe durable workflow without duplicating the command
manual. It uses `doctor` and scoped `capabilities` for discovery, explicit
session allocation, idempotent execution, bounded cursor output, guarded
notebook edits, verified artifacts, and final cleanup. The skill has no
scripts, Drive workflow, or MCP dependency.

The optional [skill-only Codex plugin](plugins/better-colab) packages those
same bytes with no MCP server, app, or hooks. Its deterministic release ZIP is
versioned with the CLI; no marketplace entry is created by default.

## Documentation

- [Agent-first architecture and public contracts](DESIGN.md)
- [Fork identity and packaging boundary](docs/00_fork_identity.md)
- [Upstream session and keep-alive design](docs/01_session_management.md)
- [Upstream execution and interactive design](docs/02_execution_and_interactive.md)
- [Upstream file-management design](docs/03_file_management.md)
- [Authentication and non-Drive automation](docs/04_automation_and_utility.md)
- [Upstream ephemeral runner design](docs/05_run_command.md)
- [JSON v1 and typed Python API](docs/06_json_and_typed_api.md)
- [Durable SQLite state](docs/07_durable_state.md)
- [Local controller and startup election](docs/08_controller.md)
- [Durable execution lifecycle](docs/09_execution_lifecycle.md)
- [Bounded output and artifacts](docs/10_bounded_output.md)
- [Session health and proof-safe recovery](docs/11_health_and_recovery.md)
- [Guarded notebook documents and durable batches](docs/12_notebooks_and_batches.md)
- [Compatibility wrappers and exclusive leases](docs/13_compatibility_wrappers.md)
- [Portable Better Colab skill](docs/14_portable_skill.md)
- [Skill-only Codex plugin](docs/15_codex_plugin.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Changes are developed test-first and
must retain upstream attribution where upstream code is modified.
