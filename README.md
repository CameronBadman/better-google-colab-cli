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

The agent-first interface is being implemented on the
`agent-first-interface` branch. Until its schemas stabilize, commands retained
from upstream should be treated as compatibility behavior rather than the
durable JSON contract described in [DESIGN.md](DESIGN.md).

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
better-colab new -s demo
printf "print('Hello from Colab')\n" | better-colab exec -s demo
better-colab stop -s demo
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

During the controller migration, retained non-interactive session, file, and
install commands also accept `--format json`. Every JSON response uses schema
version 1 and is hard-capped at 262,144 bytes.

## Documentation

- [Agent-first architecture and public contracts](DESIGN.md)
- [Fork identity and packaging boundary](docs/00_fork_identity.md)
- [Upstream session and keep-alive design](docs/01_session_management.md)
- [Upstream execution and interactive design](docs/02_execution_and_interactive.md)
- [Upstream file-management design](docs/03_file_management.md)
- [Authentication and non-Drive automation](docs/04_automation_and_utility.md)
- [Upstream ephemeral runner design](docs/05_run_command.md)
- [JSON v1 and typed Python API](docs/06_json_and_typed_api.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Changes are developed test-first and
must retain upstream attribution where upstream code is modified.
