#!/bin/bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
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
SESSION_NAME="test-durable-execution-$(date +%s)"

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
    "${BC[@]}" controller stop --force --format=json >/dev/null 2>&1 || true
    "${BC[@]}" stop -s "$SESSION_NAME" --format=json >/dev/null 2>&1 || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

echo "[*] Allocating one live CPU session..."
"${BC[@]}" new -s "$SESSION_NAME" --format=json

echo "[*] Verifying silent execution requires reply plus idle..."
SILENT=$(
    printf 'value_for_later = 41\n' |
        "${BC[@]}" execution start \
            --session "$SESSION_NAME" \
            --idempotency-key silent-state \
            --format=json
)
python -c '
import json, sys
payload = json.load(sys.stdin)
result = payload["result"]
assert payload["ok"] is True
assert result["state"] == "finished", result
assert result["dispatch_confirmed"] is True
assert result["reply_received"] is True
assert result["idle_received"] is True
assert result["output"]["events"] == []
' <<<"$SILENT"

echo "[*] Verifying attached exception exit and preserved preceding output..."
set +e
FAILED=$(
    printf 'print("before-error")\nraise ValueError("controlled")\n' |
        "${BC[@]}" execution start \
            --session "$SESSION_NAME" \
            --format=json
)
FAILED_EXIT=$?
set -e
test "$FAILED_EXIT" -eq 1
python -c '
import json, sys
payload = json.load(sys.stdin)
result = payload["result"]
assert payload["ok"] is True
assert result["state"] == "error", result
assert result["error_name"] == "ValueError", result
text = "".join(event.get("text", "") for event in result["output"]["events"])
assert "before-error" in text, result["output"]
' <<<"$FAILED"

echo "[*] Verifying detached work is visible to a later CLI process..."
DETACHED=$(
    printf 'import time\nprint(value_for_later)\ntime.sleep(1)\nprint("done")\n' |
        "${BC[@]}" execution start \
            --session "$SESSION_NAME" \
            --idempotency-key detached-state \
            --detach \
            --format=json
)
EXECUTION_ID=$(
    python -c 'import json,sys; print(json.load(sys.stdin)["result"]["execution_id"])' \
        <<<"$DETACHED"
)
"${BC[@]}" execution status "$EXECUTION_ID" --format=json
RETRIED=$(
    printf 'import time\nprint(value_for_later)\ntime.sleep(1)\nprint("done")\n' |
        "${BC[@]}" execution start \
            --session "$SESSION_NAME" \
            --idempotency-key detached-state \
            --detach \
            --format=json
)
RETRIED_ID=$(
    python -c 'import json,sys; print(json.load(sys.stdin)["result"]["execution_id"])' \
        <<<"$RETRIED"
)
test "$RETRIED_ID" = "$EXECUTION_ID"

WAITED=$(
    "${BC[@]}" execution wait "$EXECUTION_ID" --timeout 10 --format=json
)
python -c '
import json, sys
payload = json.load(sys.stdin)
result = payload["result"]
assert result["state"] == "finished", result
text = "".join(event.get("text", "") for event in result["output"]["events"])
assert "41" in text and "done" in text, result["output"]
' <<<"$WAITED"

echo "[*] Cleaning the live assignment and checking for orphans..."
"${BC[@]}" controller stop --format=json
"${BC[@]}" stop -s "$SESSION_NAME" --format=json
SESSIONS=$("${BC[@]}" sessions --format=json)
python -c '
import json, sys
payload = json.load(sys.stdin)
assert payload["ok"] is True
assert payload["result"]["sessions"] == [], payload
' <<<"$SESSIONS"

trap - EXIT
rm -rf "$TMP_DIR"
echo "[SUCCESS] Durable execution live integration passed."
