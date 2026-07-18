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

execution_id="00000000-0000-4000-8000-000000000999"
config="$work/sessions.json"
python - "$config" "$execution_id" <<'PY'
import sys

from better_colab.models import ExecutionState
from better_colab.storage import DurableStore, ProfileSpec, StatePaths

config, execution_id = sys.argv[1:]
profile = ProfileSpec.from_values(
    config_path=config,
    auth_provider="oauth2",
    oauth_config_path=None,
)
store = DurableStore(paths=StatePaths.discover(), profile=profile)
store.upsert_session(
    name="benchmark",
    endpoint="local-benchmark",
    backend_url="https://runtime.invalid",
    runtime_token="protected",
    hardware="CPU",
)
source = b"pass\n"
store.create_execution(
    execution_id=execution_id,
    session_name="benchmark",
    source=source,
    provenance={"kind": "stdin"},
    request={
        "session": "benchmark",
        "source_sha256": store.sha256(source),
    },
)
store.finalize_output(execution_id)
store.transition_execution(execution_id, ExecutionState.INTERRUPTED)
store.close()
PY

python - "$config" "$execution_id" <<'PY'
import json
import statistics
import subprocess
import sys
import time

config, execution_id = sys.argv[1:]
command = [
    "better-colab",
    "--config",
    config,
    "execution",
    "status",
    execution_id,
    "--format=json",
]
samples = []
response_sizes = []
for _ in range(50):
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
    )
    samples.append((time.perf_counter() - started) * 1000)
    response_sizes.append(len(completed.stdout))
    payload = json.loads(completed.stdout)
    assert payload["result"]["state"] == "interrupted", payload

samples.sort()
p95 = samples[max(0, int(len(samples) * 0.95) - 1)]
assert max(response_sizes) < 2048
print(
    json.dumps(
        {
            "warm_execution_status_cli_p50_ms": round(
                statistics.median(samples),
                3,
            ),
            "warm_execution_status_cli_p95_ms": round(p95, 3),
            "execution_status_response_bytes": max(response_sizes),
            "samples": len(samples),
        },
        separators=(",", ":"),
    )
)
PY

better-colab controller stop --format json >/dev/null
