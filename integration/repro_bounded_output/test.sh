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
PAGES_DIR="$TMP_DIR/pages"
mkdir -p "$PAGES_DIR"
export XDG_STATE_HOME="$TMP_DIR/state"
export XDG_RUNTIME_DIR="$TMP_DIR/runtime"
SESSION_NAME="test-bounded-output-$(date +%s)"

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

echo "[*] Executing large Unicode, HTML, and binary output..."
printf '%s\n' \
    'import base64' \
    'from IPython.display import display' \
    'text = "line-🙂-αβγ\n" * 20000' \
    'print(text, end="")' \
    'display({"text/html": "<b>" + ("h" * 40000) + "</b>", "image/png": base64.b64encode(b"\x89PNG\r\n\x1a\nlive-binary").decode()}, raw=True)' |
    "${BC[@]}" execution start \
        --session "$SESSION_NAME" \
        --idempotency-key bounded-output \
        --format=json >"$PAGES_DIR/000.json"

EXECUTION_ID=$(
    python -c '
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
result = payload["result"]
assert payload["ok"] is True
assert result["state"] == "finished", result
assert result["output"]["has_more"] is True, result["output"]
print(result["execution_id"])
' "$PAGES_DIR/000.json"
)
CURSOR=$(
    python -c '
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
print(payload["result"]["output"]["next_cursor"])
' "$PAGES_DIR/000.json"
)

echo "[*] Verifying stable cursors and reading every bounded page..."
PAGE=1
FIRST_CURSOR="$CURSOR"
while [ -n "$CURSOR" ]; do
    PAGE_FILE=$(printf '%s/%03d.json' "$PAGES_DIR" "$PAGE")
    "${BC[@]}" execution output "$EXECUTION_ID" \
        --cursor "$CURSOR" \
        --max-bytes 65536 \
        --format=json >"$PAGE_FILE"
    if [ "$CURSOR" = "$FIRST_CURSOR" ]; then
        "${BC[@]}" execution output "$EXECUTION_ID" \
            --cursor "$CURSOR" \
            --max-bytes 65536 \
            --format=json >"$TMP_DIR/replayed.json"
        cmp "$PAGE_FILE" "$TMP_DIR/replayed.json"
    fi
    CURSOR=$(
        python -c '
import json, sys
page = json.load(open(sys.argv[1], encoding="utf-8"))["result"]
print(page.get("next_cursor", ""))
' "$PAGE_FILE"
    )
    PAGE=$((PAGE + 1))
done

python -c '
import hashlib
import json
import stat
import sys
from pathlib import Path

page_files = sorted(Path(sys.argv[1]).glob("*.json"))
expected = ("line-🙂-αβγ\n" * 20000).encode()
seen_cursors = set()
text = bytearray()
artifacts = []
for index, page_file in enumerate(page_files):
    raw = page_file.read_bytes()
    assert len(raw) <= 262144, (page_file, len(raw))
    payload = json.loads(raw)
    assert payload["schema_version"] == 1 and payload["ok"] is True
    result = payload["result"]
    page = result["output"] if index == 0 else result
    for event in page["events"]:
        cursor = event["cursor"]
        assert cursor not in seen_cursors, cursor
        seen_cursors.add(cursor)
        text.extend(event.get("text", "").encode())
        if "artifact" in event:
            artifacts.append(event["artifact"])
    if index + 1 < len(page_files):
        assert page["has_more"] is True, page
    else:
        assert page.get("has_more", False) is False, page

assert bytes(text) == expected, (len(text), len(expected))
media_types = {artifact["media_type"] for artifact in artifacts}
assert "text/html" in media_types, media_types
assert "image/png" in media_types, media_types
assert "text/plain; charset=utf-8" in media_types, media_types
complete = [
    artifact
    for artifact in artifacts
    if artifact.get("purpose") == "complete_text_output"
]
assert len(complete) == 1, artifacts
for artifact in artifacts:
    path = Path(artifact["path"])
    data = path.read_bytes()
    assert len(data) == artifact["byte_size"]
    assert "sha256:" + hashlib.sha256(data).hexdigest() == artifact["sha256"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
assert Path(complete[0]["path"]).read_bytes() == expected
' "$PAGES_DIR"

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
echo "[SUCCESS] Bounded output live integration passed."
