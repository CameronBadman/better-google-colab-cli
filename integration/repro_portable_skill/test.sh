#!/bin/bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -euo pipefail

TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"
JOB="$TMP_DIR/job.py"
ERROR_JOB="$TMP_DIR/error.py"
NOTEBOOK="$TMP_DIR/work.ipynb"
REPLACEMENT="$TMP_DIR/replacement.py"
PAGES="$TMP_DIR/pages"
mkdir -p "$PAGES"
export XDG_STATE_HOME="$TMP_DIR/state"
export XDG_RUNTIME_DIR="$TMP_DIR/runtime"
SESSION_NAME="test-portable-skill-$(date +%s)"

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
    "${BC[@]}" session stop "$SESSION_NAME" --format=json >/dev/null 2>&1 || true
    "${BC[@]}" stop -s "$SESSION_NAME" --format=json >/dev/null 2>&1 || true
    "${BC[@]}" controller stop --force --format=json >/dev/null 2>&1 || true
    "${BC[@]}" session stop "$SESSION_NAME" --format=json >/dev/null 2>&1 || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

"${PY[@]}" -c '
from pathlib import Path
import sys

import nbformat

Path(sys.argv[1]).write_text(
    "import base64\n"
    "from IPython.display import display\n"
    "print(\"skill-page-\" * 10000)\n"
    "display({\"image/png\": "
    "base64.b64encode(b\"\\x89PNG\\r\\n\\x1a\\nskill-artifact\").decode()}, "
    "raw=True)\n",
    encoding="utf-8",
)
Path(sys.argv[2]).write_text(
    "print(\"before-skill-error\", flush=True)\n"
    "raise RuntimeError(\"skill failure\")\n",
    encoding="utf-8",
)
Path(sys.argv[4]).write_text(
    "value = 42\nprint(\"notebook-value\", value)\n",
    encoding="utf-8",
)
notebook = nbformat.v4.new_notebook(cells=[
    nbformat.v4.new_code_cell(
        "value = 1\nprint(\"notebook-value\", value)",
        id="work",
    ),
])
nbformat.write(notebook, sys.argv[3])
' "$JOB" "$ERROR_JOB" "$NOTEBOOK" "$REPLACEMENT"

echo "[*] Validating discovery and allocating one CPU session..."
DOCTOR=$("${BC[@]}" doctor --format=json)
CAPABILITIES=$("${BC[@]}" capabilities execution.start --format=json)
"${PY[@]}" -c '
import json, sys
doctor = json.loads(sys.argv[1])
capabilities = json.load(sys.stdin)
assert doctor["schema_version"] == 1 and doctor["ok"] is True, doctor
assert capabilities["schema_version"] == 1 and capabilities["ok"] is True
assert capabilities["result"]["commands"][0]["name"] == "execution start"
' "$DOCTOR" <<<"$CAPABILITIES"

"${BC[@]}" session ensure "$SESSION_NAME" --format=json
PROBE=$("${BC[@]}" session probe "$SESSION_NAME" --format=json)
"${PY[@]}" -c '
import json, sys
result = json.load(sys.stdin)["result"]
assert result["kernel_execution_ready"] is True, result
' <<<"$PROBE"

echo "[*] Forward-testing detached idempotency, cursors, and artifacts..."
DETACHED=$(
    "${BC[@]}" execution start \
        --session "$SESSION_NAME" \
        --file "$JOB" \
        --idempotency-key portable-skill-large-output \
        --detach \
        --format=json
)
EXECUTION_ID=$(
    "${PY[@]}" -c \
        'import json,sys; print(json.load(sys.stdin)["result"]["execution_id"])' \
        <<<"$DETACHED"
)
RETRIED=$(
    "${BC[@]}" execution start \
        --session "$SESSION_NAME" \
        --file "$JOB" \
        --idempotency-key portable-skill-large-output \
        --detach \
        --format=json
)
"${PY[@]}" -c '
import json, sys
assert json.load(sys.stdin)["result"]["execution_id"] == sys.argv[1]
' "$EXECUTION_ID" <<<"$RETRIED"

"${BC[@]}" execution wait "$EXECUTION_ID" \
    --timeout 60 \
    --max-bytes 65536 \
    --format=json >"$PAGES/000.json"
CURSOR=$(
    "${PY[@]}" -c '
import json, sys
result = json.load(open(sys.argv[1], encoding="utf-8"))["result"]
assert result["state"] == "finished", result
assert result["output"]["has_more"] is True, result["output"]
print(result["output"]["next_cursor"])
' "$PAGES/000.json"
)

PAGE=1
while [ -n "$CURSOR" ]; do
    PAGE_FILE=$(printf '%s/%03d.json' "$PAGES" "$PAGE")
    "${BC[@]}" execution output "$EXECUTION_ID" \
        --cursor "$CURSOR" \
        --max-bytes 65536 \
        --format=json >"$PAGE_FILE"
    CURSOR=$(
        "${PY[@]}" -c '
import json, sys
result = json.load(open(sys.argv[1], encoding="utf-8"))["result"]
print(result.get("next_cursor", ""))
' "$PAGE_FILE"
    )
    PAGE=$((PAGE + 1))
done

"${PY[@]}" -c '
import hashlib
import json
import sys
from pathlib import Path

expected = ("skill-page-" * 10000 + "\n").encode()
text = bytearray()
artifacts = []
seen_cursors = set()
pages = sorted(Path(sys.argv[1]).glob("*.json"))
for index, path in enumerate(pages):
    raw = path.read_bytes()
    assert len(raw) <= 262144, (path, len(raw))
    payload = json.loads(raw)
    assert payload["schema_version"] == 1 and payload["ok"] is True, payload
    page = payload["result"]["output"] if index == 0 else payload["result"]
    for event in page["events"]:
        assert event["cursor"] not in seen_cursors, event["cursor"]
        seen_cursors.add(event["cursor"])
        text.extend(event.get("text", "").encode())
        if "artifact" in event:
            artifacts.append(event["artifact"])
    assert page.get("has_more", False) is (index + 1 < len(pages)), page

assert bytes(text) == expected, (len(text), len(expected))
images = [item for item in artifacts if item["media_type"] == "image/png"]
assert len(images) == 1, artifacts
artifact = images[0]
artifact_path = Path(artifact["path"])
data = artifact_path.read_bytes()
assert len(data) == artifact["byte_size"]
assert "sha256:" + hashlib.sha256(data).hexdigest() == artifact["sha256"]
' "$PAGES"

echo "[*] Forward-testing exception recovery without replay..."
ERROR_START=$(
    "${BC[@]}" execution start \
        --session "$SESSION_NAME" \
        --file "$ERROR_JOB" \
        --idempotency-key portable-skill-error \
        --detach \
        --format=json
)
ERROR_ID=$(
    "${PY[@]}" -c \
        'import json,sys; print(json.load(sys.stdin)["result"]["execution_id"])' \
        <<<"$ERROR_START"
)
set +e
ERROR_WAIT=$(
    "${BC[@]}" execution wait "$ERROR_ID" --timeout 60 --format=json
)
ERROR_EXIT=$?
set -e
test "$ERROR_EXIT" -eq 1
ERROR_STATUS=$(
    "${BC[@]}" execution status "$ERROR_ID" \
        --include traceback \
        --format=json
)
"${PY[@]}" -c '
import json, sys
waited = json.loads(sys.argv[1])["result"]
status = json.load(sys.stdin)["result"]
assert waited["state"] == "error", waited
text = "".join(event.get("text", "") for event in waited["output"]["events"])
assert "before-skill-error" in text, text
assert status["state"] == "error", status
assert status["error_name"] == "RuntimeError", status
assert any("skill failure" in line for line in status["traceback"]), status
' "$ERROR_WAIT" <<<"$ERROR_STATUS"

echo "[*] Forward-testing guarded notebook editing and writeback..."
CELLS=$("${BC[@]}" notebook cells "$NOTEBOOK" --format=json)
CELL=$("${BC[@]}" notebook cell "$NOTEBOOK" --cell-id work --format=json)
OLD_HASH=$(
    "${PY[@]}" -c \
        'import json,sys; print(json.load(sys.stdin)["result"]["source_sha256"])' \
        <<<"$CELL"
)
"${PY[@]}" -c '
import json, sys
result = json.load(sys.stdin)["result"]
assert result["notebook_sha256"], result
assert result["cells"][0]["source_sha256"], result
' <<<"$CELLS"
"${BC[@]}" notebook update "$NOTEBOOK" \
    --cell-id work \
    --file "$REPLACEMENT" \
    --expected-sha256 "$OLD_HASH" \
    --format=json >/dev/null
UPDATED=$("${BC[@]}" notebook cell "$NOTEBOOK" --cell-id work --format=json)
NEW_HASH=$(
    "${PY[@]}" -c \
        'import json,sys; print(json.load(sys.stdin)["result"]["source_sha256"])' \
        <<<"$UPDATED"
)
NOTEBOOK_RUN=$(
    "${BC[@]}" execution start \
        --session "$SESSION_NAME" \
        --notebook "$NOTEBOOK" \
        --cell-id work \
        --expected-source-sha256 "$NEW_HASH" \
        --idempotency-key portable-skill-notebook \
        --format=json
)
NOTEBOOK_EXECUTION_ID=$(
    "${PY[@]}" -c '
import json, sys
result = json.load(sys.stdin)["result"]
assert result["state"] == "finished", result
text = "".join(event.get("text", "") for event in result["output"]["events"])
assert "notebook-value 42" in text, text
print(result["execution_id"])
' <<<"$NOTEBOOK_RUN"
)
"${BC[@]}" notebook write-output "$NOTEBOOK_EXECUTION_ID" --format=json \
    >/dev/null
"${PY[@]}" -c '
import nbformat
import sys
notebook = nbformat.read(sys.argv[1], as_version=4)
assert notebook.cells[0].source.startswith("value = 42")
assert "notebook-value 42" in notebook.cells[0].outputs[0].text
' "$NOTEBOOK"

echo "[*] Releasing the session while retaining durable history..."
"${BC[@]}" session stop "$SESSION_NAME" --format=json
SESSIONS=$("${BC[@]}" session list --format=json)
"${PY[@]}" -c '
import json, sys
assert json.load(sys.stdin)["result"]["sessions"] == []
' <<<"$SESSIONS"
for ID in "$EXECUTION_ID" "$ERROR_ID" "$NOTEBOOK_EXECUTION_ID"; do
    "${BC[@]}" execution status "$ID" --format=json >/dev/null
done
"${BC[@]}" controller stop --format=json

trap - EXIT
rm -rf "$TMP_DIR"
echo "[SUCCESS] Portable skill live integration passed."
