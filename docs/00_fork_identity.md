---
title: Fork identity and packaging
status: implemented
last_updated: 2026-07-17
change_log:
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

The Better surface is constructed from the retained command modules without
registering `drivemount` or the stale upstream skill resource. The compatibility
surface registers both. Neither surface performs an implicit update request;
the explicit `update` command remains available.

The fork preserves the Apache-2.0 licence, Google copyright headers on upstream
sources, upstream history, and a repository link back to the original project.
The README identifies the fork as unofficial and unaffiliated.

## Testing strategy

- Parse both project files and assert distribution names and console scripts.
- Assert the core wheel packages `better_colab` and `colab_cli`.
- Compare Better and compatibility help output to enforce the Drive boundary.
- Invoke an ordinary command while spying on update code.
- Build both wheel/sdist pairs and inspect entry points, versions, and the
  compatibility wheel's exact dependency.

