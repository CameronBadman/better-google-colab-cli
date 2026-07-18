---
title: Better Colab Codex plugin
status: implemented
last_updated: 2026-07-18
change_log:
  - date: 2026-07-18
    summary: Packaged the canonical portable skill as a strict-semver, skill-only Codex plugin with reproducible archives and isolated install validation.
---

# Better Colab Codex plugin

`plugins/better-colab/.codex-plugin/plugin.json` defines the optional Codex
plugin. Version `0.1.0` matches the first schema-stable Better Colab CLI
release. The release workflow rejects a `v*` tag whose value does not match
that manifest version.

The manifest is prominently unofficial and unaffiliated, uses Apache-2.0
metadata, points back to the fork repository, and declares only
`skills: "./skills/"`. It has no MCP server, app, hook, Drive capability, or
marketplace metadata.

## Package contents

The plugin archive contains exactly:

- `.codex-plugin/plugin.json`;
- the repository's Apache-2.0 `LICENSE`;
- `skills/better-colab/SKILL.md`; and
- `skills/better-colab/agents/openai.yaml`.

`tools/package_plugin.py sync` copies the licence and canonical
`.agents/skills/better-colab` files byte-for-byte. Its `check` command rejects
missing, changed, or additional package files. CI runs that check, so an edit
to the canonical skill cannot silently leave the plugin copy stale.

`tools/package_plugin.py build` writes
`better-colab-plugin-0.1.0.zip` with sorted entries, fixed timestamps, stable
permissions, and a single `better-colab/` archive root. Repeated builds from
identical source produce the same SHA-256. The release workflow uploads this
ZIP separately from the Python wheels and source distributions, preventing
the plugin archive from being sent to PyPI.

## Validation strategy

`tests/test_plugin_package.py` enforces strict semver, metadata, the skill-only
manifest, exact package contents, byte equality, deterministic ZIP output,
release-tag matching, and the absence of a repository marketplace entry.

The plugin creator validator is run against both the source directory and a
fresh extraction of the ZIP. The skill validator is also run on the packaged
copy.

`integration/repro_plugin_install/test.sh` performs the local install and
fresh-context smoke test. Its temporary marketplace fixture and isolated
`CODEX_HOME` live under a temporary directory; it installs version `0.1.0`
and renders a new model prompt outside the source repository. The prompt must
discover `better-colab:better-colab` from the installed plugin cache rather
than from the repository's `.agents` directory.

The fixture and isolated home are removed on exit. No marketplace entry is
created or mutated in the repository or the user's Codex configuration, and
no marketplace product entry is part of this milestone.
