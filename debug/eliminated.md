# Eliminated hypotheses

## H-5 — Reinstalling the fork or merging current upstream fixes auth

Eliminated. The installed CLI and fork `main` both resolve to `9cc61d1`.
Google upstream `b04e83a` contains SSH, environment, high-memory, dependency,
and kernel-client compatibility changes, but no auth or Drive-flow fix. The
installed `jupyter-kernel-client` is 0.8.0 and exposes only `KernelClient`, so
upstream's `ColabKernelClient` compatibility patch is not active here.

## H-2 — VM-side gcloud hangs before consuming input

Eliminated. A live VM probe ran the same gcloud command with a deliberately
invalid verification value; it consumed stdin and returned `invalid_grant` in
0.619 seconds. Inspection of `jupyter_client` then showed that custom stdin
hooks must send their own reply and that ordinary return values are ignored.
The VM-side gcloud process was still waiting for input, not hanging after it.
