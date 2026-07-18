#!/bin/bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -euo pipefail

TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"
export XDG_STATE_HOME="$TMP_DIR/state"
export XDG_RUNTIME_DIR="$TMP_DIR/runtime"
SESSION_NAME="test-health-recovery-$(date +%s)"

if [ -f "$HOME/.config/colab-cli/token.json" ]; then
    AUTH_FLAGS=(--auth=oauth2)
elif command -v gcloud >/dev/null &&
    gcloud auth application-default print-access-token >/dev/null 2>&1; then
    AUTH_FLAGS=(--auth=adc)
else
    echo "No non-interactive OAuth2 or ADC credentials are available." >&2
    exit 1
fi

BC=(uv run better-colab "${AUTH_FLAGS[@]}" --config "$SESSION_FILE")

cleanup() {
    "${BC[@]}" stop -s "$SESSION_NAME" --format=json >/dev/null 2>&1 || true
    "${BC[@]}" controller stop --force --format=json >/dev/null 2>&1 || true
    "${BC[@]}" stop -s "$SESSION_NAME" --format=json >/dev/null 2>&1 || true
    "${BC[@]}" controller stop --force --format=json >/dev/null 2>&1 || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

echo "[*] Allocating one live CPU session..."
"${BC[@]}" new -s "$SESSION_NAME" --format=json

echo "[*] Verifying a raw no-history nonce readiness proof..."
PROBE=$(
    "${BC[@]}" session probe "$SESSION_NAME" --timeout 20 --format=json
)
python -c '
import json, sys
result = json.load(sys.stdin)["result"]
mandatory = {
    "controller_alive",
    "backend_alive",
    "kernel_connected",
    "kernel_execution_ready",
    "kernel_probe_at",
    "kernel_probe_latency_ms",
    "kernel_probe_error",
}
assert mandatory <= result.keys(), result
assert result["controller_alive"] is True, result
assert result["backend_alive"] is True, result
assert result["kernel_connected"] is True, result
assert result["kernel_execution_ready"] is True, result
assert result["kernel_probe_at"], result
assert result["kernel_probe_latency_ms"] >= 0, result
assert result["kernel_probe_error"] is None, result
' <<<"$PROBE"

echo "[*] Killing the controller during one confirmed execution..."
DETACHED=$(
    printf '%s\n' \
        'import time' \
        'recovery_runs = globals().get("recovery_runs", 0) + 1' \
        'print("before-controller-death", flush=True)' \
        'time.sleep(12)' \
        'print("after-controller-death", flush=True)' |
        "${BC[@]}" execution start \
            --session "$SESSION_NAME" \
            --idempotency-key live-controller-death \
            --detach \
            --format=json
)
EXECUTION_ID=$(
    python -c 'import json,sys; print(json.load(sys.stdin)["result"]["execution_id"])' \
        <<<"$DETACHED"
)

STATE=""
for _ in $(seq 1 100); do
    STATUS=$(
        "${BC[@]}" execution status "$EXECUTION_ID" --format=json
    )
    STATE=$(
        python -c 'import json,sys; print(json.load(sys.stdin)["result"]["state"])' \
            <<<"$STATUS"
    )
    if [ "$STATE" = "running" ]; then
        break
    fi
    sleep 0.1
done
test "$STATE" = "running"

CONTROLLER_STATUS=$("${BC[@]}" controller status --format=json)
CONTROLLER_PID=$(
    python -c 'import json,sys; print(json.load(sys.stdin)["result"]["pid"])' \
        <<<"$CONTROLLER_STATUS"
)
kill -9 "$CONTROLLER_PID"

echo "[*] Electing a replacement and recovering without replay..."
"${BC[@]}" controller start --format=json
set +e
WAITED=$(
    "${BC[@]}" execution wait "$EXECUTION_ID" --timeout 30 --format=json
)
WAIT_EXIT=$?
set -e
test "$WAIT_EXIT" -eq 0 -o "$WAIT_EXIT" -eq 1
RECOVERED_STATE=$(
    python -c '
import json, sys
result = json.load(sys.stdin)["result"]
assert result.get("wait_timed_out", False) is False, result
assert result["state"] in {"finished", "unknown"}, result
assert result["output_complete"] is False, result
print(result["state"])
' <<<"$WAITED"
)

RECOVERY_STATUS=$(
    "${BC[@]}" execution status "$EXECUTION_ID" \
        --include transitions \
        --format=json
)
python -c '
import json, sys
result = json.load(sys.stdin)["result"]
states = [item["to_state"] for item in result["transitions"]]
assert "disconnected" in states, states
assert states[-1] in {"finished", "unknown"}, states
assert result["completion_source"] == "recovery", result
assert result["output_complete"] is False, result
' <<<"$RECOVERY_STATUS"

echo "[*] Proving the original source ran exactly once..."
COUNTER=$(
    printf 'print("recovery-runs", recovery_runs)\n' |
        "${BC[@]}" execution start \
            --session "$SESSION_NAME" \
            --idempotency-key verify-no-replay \
            --format=json
)
python -c '
import json, sys
result = json.load(sys.stdin)["result"]
assert result["state"] == "finished", result
text = "".join(event.get("text", "") for event in result["output"]["events"])
assert "recovery-runs 1" in text, text
' <<<"$COUNTER"

echo "[*] Re-probing readiness on the replacement connection..."
REPROBE=$(
    "${BC[@]}" session probe "$SESSION_NAME" --timeout 20 --format=json
)
python -c '
import json, sys
result = json.load(sys.stdin)["result"]
assert result["kernel_execution_ready"] is True, result
assert result["kernel_probe_error"] is None, result
' <<<"$REPROBE"

echo "[*] Cleaning the live assignment and checking for orphans..."
"${BC[@]}" stop -s "$SESSION_NAME" --format=json
"${BC[@]}" controller stop --format=json
SESSIONS=$("${BC[@]}" sessions --format=json)
python -c '
import json, sys
payload = json.load(sys.stdin)
assert payload["ok"] is True
assert payload["result"]["sessions"] == [], payload
' <<<"$SESSIONS"

trap - EXIT
rm -rf "$TMP_DIR"
echo "[SUCCESS] Health/recovery integration passed ($RECOVERED_STATE)."
