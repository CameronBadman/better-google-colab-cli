#!/usr/bin/env bash
set -euo pipefail

work="$(mktemp -d)"
export XDG_STATE_HOME="$work/state"
export XDG_RUNTIME_DIR="$work/runtime"

cleanup() {
  better-colab controller stop --force --format json >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

python <<'PY'
import json
import statistics
import time

from better_colab.controller_client import ControllerClient
from better_colab.storage import StatePaths

client = ControllerClient(paths=StatePaths.discover())

cold_started = time.perf_counter()
first = client.ensure_running()
cold_ms = (time.perf_counter() - cold_started) * 1000

samples = []
pids = set()
for _ in range(100):
    started = time.perf_counter()
    status = client.status()
    samples.append((time.perf_counter() - started) * 1000)
    pids.add(status["pid"])

samples.sort()
p95 = samples[max(0, int(len(samples) * 0.95) - 1)]
assert pids == {first["pid"]}

print(
    json.dumps(
        {
            "cold_controller_ready_ms": round(cold_ms, 3),
            "warm_rpc_p50_ms": round(statistics.median(samples), 3),
            "warm_rpc_p95_ms": round(p95, 3),
            "samples": len(samples),
        },
        separators=(",", ":"),
    )
)
PY

better-colab controller stop --format json >/dev/null
