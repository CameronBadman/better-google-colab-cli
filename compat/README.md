# Better Colab compatibility shim

This optional Apache-2.0 package installs the historical `colab` executable
and depends on the exact same version of `better-google-colab-cli`.

The core package installs only `better-colab`. Install this shim only when a
script or human workflow still needs the upstream-compatible command name or
legacy-only commands.
