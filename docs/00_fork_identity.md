---
title: Fork identity and packaging
status: implemented
last_updated: 2026-08-18
change_log:
  - date: 2026-08-18
    summary: Pinned distributable metadata to the released kernel client whose private transport boundary is exercised by the CLI, and added clean-wheel install smoke checks to CI and release builds.
  - date: 2026-07-18
    summary: Removed the stale bundled operator skill and its compatibility command in favor of one canonical portable skill under .agents.
  - date: 2026-07-17
    summary: Split the Better Colab core executable from the optional upstream compatibility shim.
---

# Fork identity and packaging

`better-google-colab-cli` is the core distribution. It contains both the
fork-specific `better_colab` package and the retained upstream `colab_cli`
implementation package, but its only console script is `better-colab`.

`better-google-colab-cli-compat` is a separate wheel whose only console script
is `colab`. Its build metadata derives the same version from Git as the core
wheel and declares an exact dependency on that version. This prevents a legacy
entry point from silently targeting a different core implementation.

The core wheel declares `jupyter-kernel-client==1.0.1`. This is intentionally
exact because the controller and retained compatibility runtime use a tested
private transport boundary. Source tests and build workflows verify the
released dependency's class name, websocket queues, subprotocol selection,
headers, and extra query-parameter support. CI and release builds install the
built wheel into a clean environment before accepting the artifact, so a
source-only override cannot mask invalid wheel metadata.

The Better surface is constructed from the retained command modules without
registering `drivemount`. The compatibility surface retains that command.
The stale bundled operator skill and `colab skill` command are removed from
both distributions; the canonical portable guide lives under `.agents` and
is packaged separately by the plugin. Neither executable performs an implicit
update request; the explicit `update` command remains available.

The fork preserves the Apache-2.0 licence, Google copyright headers on upstream
sources, upstream history, and a repository link back to the original project.
The README identifies the fork as unofficial and unaffiliated.

## Testing strategy

- Parse both project files and assert distribution names and console scripts.
- Assert the core wheel packages `better_colab` and `colab_cli`.
- Compare Better and compatibility help output to enforce the Drive boundary.
- Assert neither executable registers the removed stale skill command.
- Invoke an ordinary command while spying on update code.
- Build both wheel/sdist pairs and inspect entry points, versions, and the
  compatibility wheel's exact dependency.
- Install the core wheel in a clean environment and exercise the exact
  private kernel-client surface used at runtime.
