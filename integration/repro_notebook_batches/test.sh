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
NOTEBOOK="$TMP_DIR/stateful.ipynb"
MISSING_IDS="$TMP_DIR/missing-ids.ipynb"
export XDG_STATE_HOME="$TMP_DIR/state"
export XDG_RUNTIME_DIR="$TMP_DIR/runtime"
SESSION_NAME="test-notebook-batches-$(date +%s)"

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
PY=(uv run python)

cleanup() {
    "${BC[@]}" stop -s "$SESSION_NAME" --format=json >/dev/null 2>&1 || true
    "${BC[@]}" controller stop --force --format=json >/dev/null 2>&1 || true
    "${BC[@]}" stop -s "$SESSION_NAME" --format=json >/dev/null 2>&1 || true
    "${BC[@]}" controller stop --force --format=json >/dev/null 2>&1 || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

"${PY[@]}" -c '
import nbformat
import sys

notebook = nbformat.v4.new_notebook(cells=[
    nbformat.v4.new_code_cell(
        "shared_value = 40\nprint(\"setup\", shared_value)",
        id="setup",
    ),
    nbformat.v4.new_code_cell(
        "shared_value += 2\nprint(\"value\", shared_value)",
        id="use",
    ),
    nbformat.v4.new_code_cell(
        "print(\"before-batch-error\")\nraise ValueError(\"batch stop\")",
        id="fail",
    ),
    nbformat.v4.new_code_cell(
        "batch_marker = globals().get(\"batch_marker\", 0) + 1\n"
        "print(\"marker\", batch_marker)",
        id="marker",
    ),
    nbformat.v4.new_code_cell(
        "print(\"continued\", shared_value)",
        id="continue",
    ),
])
nbformat.write(notebook, sys.argv[1])
' "$NOTEBOOK"

echo "[*] Allocating one live CPU session..."
"${BC[@]}" new -s "$SESSION_NAME" --format=json

echo "[*] Inspecting bounded metadata and exact cell source..."
CELLS=$("${BC[@]}" notebook cells "$NOTEBOOK" --format=json)
SETUP=$(
    "${BC[@]}" notebook cell "$NOTEBOOK" --cell-id setup --format=json
)
SETUP_HASH=$(
    "${PY[@]}" -c 'import json,sys; print(json.load(sys.stdin)["result"]["source_sha256"])' \
        <<<"$SETUP"
)
"${PY[@]}" -c '
import json, sys
result = json.load(sys.stdin)["result"]
assert len(result["cells"]) == 5, result
assert all("source" not in cell for cell in result["cells"]), result
assert all("outputs" not in cell for cell in result["cells"]), result
' <<<"$CELLS"

echo "[*] Executing stateful cells from guarded source snapshots..."
SETUP_RUN=$(
    "${BC[@]}" execution start \
        --session "$SESSION_NAME" \
        --notebook "$NOTEBOOK" \
        --cell-id setup \
        --expected-source-sha256 "$SETUP_HASH" \
        --format=json
)
USE=$("${BC[@]}" notebook cell "$NOTEBOOK" --cell-id use --format=json)
USE_HASH=$(
    "${PY[@]}" -c 'import json,sys; print(json.load(sys.stdin)["result"]["source_sha256"])' \
        <<<"$USE"
)
USE_RUN=$(
    "${BC[@]}" execution start \
        --session "$SESSION_NAME" \
        --notebook "$NOTEBOOK" \
        --cell-id use \
        --expected-source-sha256 "$USE_HASH" \
        --format=json
)
USE_EXECUTION_ID=$(
    "${PY[@]}" -c 'import json,sys; print(json.load(sys.stdin)["result"]["execution_id"])' \
        <<<"$USE_RUN"
)
"${PY[@]}" -c '
import json, sys
setup = json.loads(sys.argv[1])["result"]
use = json.load(sys.stdin)["result"]
assert setup["state"] == "finished", setup
assert use["state"] == "finished", use
text = "".join(event.get("text", "") for event in use["output"]["events"])
assert "value 42" in text, text
' "$SETUP_RUN" <<<"$USE_RUN"

echo "[*] Writing complete output back only through the explicit guard..."
WRITE=$(
    "${BC[@]}" notebook write-output "$USE_EXECUTION_ID" --format=json
)
"${PY[@]}" -c '
import json
import nbformat
import sys

result = json.loads(sys.argv[1])["result"]
notebook = nbformat.read(sys.argv[2], as_version=4)
assert result["cell_id"] == "use", result
assert result["outputs_written"] == 1, result
assert notebook.cells[0].outputs == [], notebook.cells[0].outputs
assert notebook.cells[1].source.startswith("shared_value += 2")
assert "value 42" in notebook.cells[1].outputs[0].text
' "$WRITE" "$NOTEBOOK"

echo "[*] Verifying default batch stop leaves later children undispatched..."
set +e
STOPPED=$(
    "${BC[@]}" execution batch start \
        --session "$SESSION_NAME" \
        --notebook "$NOTEBOOK" \
        --cell-id fail \
        --cell-id marker \
        --format=json
)
STOPPED_EXIT=$?
set -e
if [ "$STOPPED_EXIT" -ne 1 ]; then
    echo "$STOPPED" >&2
    find "$TMP_DIR" -maxdepth 3 -type f -print >&2
    if [ -f "$XDG_RUNTIME_DIR/better-colab/controller.log" ]; then
        tail -100 "$XDG_RUNTIME_DIR/better-colab/controller.log" >&2
    fi
    exit 1
fi
"${PY[@]}" -c '
import json, sys
result = json.load(sys.stdin)["result"]
assert result["state"] == "error", result
assert [item["state"] for item in result["executions"]] == [
    "error",
    "interrupted",
], result
' <<<"$STOPPED"

set +e
MARKER_CHECK=$(
    printf 'print("marker-present", "batch_marker" in globals())\n' |
        "${BC[@]}" execution start \
            --session "$SESSION_NAME" \
            --format=json
)
MARKER_EXIT=$?
set -e
if [ "$MARKER_EXIT" -ne 0 ]; then
    echo "$MARKER_CHECK" >&2
    find "$TMP_DIR" -maxdepth 3 -type f -print >&2
    if [ -f "$XDG_RUNTIME_DIR/better-colab/controller.log" ]; then
        tail -100 "$XDG_RUNTIME_DIR/better-colab/controller.log" >&2
    fi
    exit 1
fi
"${PY[@]}" -c '
import json, sys
result = json.load(sys.stdin)["result"]
text = "".join(event.get("text", "") for event in result["output"]["events"])
assert "marker-present False" in text, text
' <<<"$MARKER_CHECK"

echo "[*] Verifying continue-on-error dispatches the later child..."
set +e
CONTINUED=$(
    "${BC[@]}" execution batch start \
        --session "$SESSION_NAME" \
        --notebook "$NOTEBOOK" \
        --cell-id fail \
        --cell-id continue \
        --continue-on-error \
        --format=json
)
CONTINUED_EXIT=$?
set -e
test "$CONTINUED_EXIT" -eq 1
CONTINUED_CHILD=$(
    "${PY[@]}" -c '
import json, sys
result = json.load(sys.stdin)["result"]
assert result["state"] == "error", result
assert [item["state"] for item in result["executions"]] == [
    "error",
    "finished",
], result
print(result["executions"][1]["execution_id"])
' <<<"$CONTINUED"
)
CONTINUED_OUTPUT=$(
    "${BC[@]}" execution output "$CONTINUED_CHILD" --format=json
)
"${PY[@]}" -c '
import json, sys
result = json.load(sys.stdin)["result"]
text = "".join(event.get("text", "") for event in result["events"])
assert "continued 42" in text, text
' <<<"$CONTINUED_OUTPUT"

echo "[*] Exercising stale-edit rejection and guarded ID assignment..."
printf 'shared_value += 3\nprint("value", shared_value)\n' |
    "${BC[@]}" notebook update "$NOTEBOOK" \
        --cell-id use \
        --expected-sha256 "$USE_HASH" \
        --format=json >/dev/null
set +e
STALE=$(
    printf 'stale = True\n' |
        "${BC[@]}" notebook update "$NOTEBOOK" \
            --cell-id use \
            --expected-sha256 "$USE_HASH" \
            --format=json
)
STALE_EXIT=$?
set -e
test "$STALE_EXIT" -eq 5
"${PY[@]}" -c '
import json, sys
assert json.load(sys.stdin)["error"]["code"] == "SOURCE_HASH_MISMATCH"
' <<<"$STALE"

"${PY[@]}" -c '
import json
import sys

with open(sys.argv[1], "w", encoding="utf-8") as file:
    json.dump({
        "cells": [{
            "cell_type": "code",
            "execution_count": None,
            "metadata": {},
            "outputs": [],
            "source": "missing = True",
        }],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }, file)
' "$MISSING_IDS"
MISSING_LIST=$(
    "${BC[@]}" notebook cells "$MISSING_IDS" --format=json
)
MISSING_HASH=$(
    "${PY[@]}" -c 'import json,sys; print(json.load(sys.stdin)["result"]["notebook_sha256"])' \
        <<<"$MISSING_LIST"
)
ASSIGNED=$(
    "${BC[@]}" notebook ids assign "$MISSING_IDS" \
        --expected-notebook-sha256 "$MISSING_HASH" \
        --format=json
)
"${PY[@]}" -c '
import json, sys
result = json.load(sys.stdin)["result"]
assert len(result["assigned"]) == 1, result
' <<<"$ASSIGNED"

echo "[*] Cleaning the live assignment and checking for orphans..."
"${BC[@]}" stop -s "$SESSION_NAME" --format=json
"${BC[@]}" controller stop --format=json
SESSIONS=$("${BC[@]}" sessions --format=json)
"${PY[@]}" -c '
import json, sys
payload = json.load(sys.stdin)
assert payload["ok"] is True
assert payload["result"]["sessions"] == [], payload
' <<<"$SESSIONS"

trap - EXIT
rm -rf "$TMP_DIR"
echo "[SUCCESS] Notebook and batch integration passed."
