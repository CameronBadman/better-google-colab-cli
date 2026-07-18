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
SCRIPT="$TMP_DIR/job.py"
export XDG_STATE_HOME="$TMP_DIR/state"
export XDG_RUNTIME_DIR="$TMP_DIR/runtime"
SESSION_NAME="test-compat-wrappers-$(date +%s)"

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
COLAB=(
    uv run colab
    "${AUTH_FLAGS[@]}"
    --config "$TMP_DIR/legacy-sessions.json"
)
PY=(uv run python)

cleanup() {
    "${BC[@]}" stop -s "$SESSION_NAME" --format=json >/dev/null 2>&1 || true
    "${BC[@]}" controller stop --force --format=json >/dev/null 2>&1 || true
    "${BC[@]}" stop -s "$SESSION_NAME" --format=json >/dev/null 2>&1 || true
    "${BC[@]}" controller stop --force --format=json >/dev/null 2>&1 || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

echo "[*] Allocating through the flat durable session wrapper..."
"${BC[@]}" new -s "$SESSION_NAME" --format=json

echo "[*] Sharing state across piped repl and flat exec..."
REPL=$(
    printf '%s\n' \
        'compat_value = 40' \
        'print("repl-ready", compat_value)' |
        "${BC[@]}" repl -s "$SESSION_NAME"
)
grep -q "repl-ready 40" <<<"$REPL"

EXEC=$(
    printf 'compat_value += 1\nprint("exec-value", compat_value)\n' |
        "${BC[@]}" exec -s "$SESSION_NAME"
)
grep -q "exec-value 41" <<<"$EXEC"

echo "[*] Preserving preceding output and a nonzero user-code exit..."
set +e
ERROR_OUTPUT=$(
    printf '%s\n' \
        'print("before-flat-error")' \
        'raise ValueError("flat boom")' |
        "${BC[@]}" exec -s "$SESSION_NAME" 2>&1
)
ERROR_EXIT=$?
set -e
test "$ERROR_EXIT" -eq 1
grep -q "before-flat-error" <<<"$ERROR_OUTPUT"
grep -q "ValueError" <<<"$ERROR_OUTPUT"

echo "[*] Running a local script durably while retaining the same session..."
"${PY[@]}" -c '
from pathlib import Path
import sys

Path(sys.argv[1]).write_text(
    "import sys\n"
    "compat_value += 1\n"
    "print(\"run-argv\", sys.argv[1:])\n"
    "print(\"run-value\", compat_value)\n",
    encoding="utf-8",
)
' "$SCRIPT"
RUN=$(
    "${BC[@]}" run -s "$SESSION_NAME" --keep "$SCRIPT" alpha --script-flag
)
grep -q "run-argv \['alpha', '--script-flag'\]" <<<"$RUN"
grep -q "run-value 42" <<<"$RUN"

echo "[*] Installing through the durable controller path..."
INSTALL=$("${BC[@]}" install -s "$SESSION_NAME" packaging)
grep -q "Installation Complete" <<<"$INSTALL"

echo "[*] Observing and stopping through flat typed session wrappers..."
STATUS=$("${BC[@]}" status -s "$SESSION_NAME" --format=json)
"${PY[@]}" -c '
import json, sys
result = json.load(sys.stdin)["result"]
assert result["name"] == sys.argv[1], result
assert result["controller_alive"] is True, result
' "$SESSION_NAME" <<<"$STATUS"

"${BC[@]}" stop -s "$SESSION_NAME" --format=json
"${BC[@]}" controller stop --format=json

LOCAL_SESSIONS=$("${BC[@]}" sessions --format=json)
"${PY[@]}" -c '
import json, sys
assert json.load(sys.stdin)["result"]["sessions"] == []
' <<<"$LOCAL_SESSIONS"

SERVER_SESSIONS=$("${COLAB[@]}" sessions --format=json)
"${PY[@]}" -c '
import json, sys
assert json.load(sys.stdin)["result"]["sessions"] == []
' <<<"$SERVER_SESSIONS"

trap - EXIT
rm -rf "$TMP_DIR"
echo "[SUCCESS] Durable compatibility-wrapper integration passed."
