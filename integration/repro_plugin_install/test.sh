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

ROOT=$PWD
CODEX_BIN=${CODEX_BIN:-}
if [ -z "$CODEX_BIN" ]; then
    CODEX_BIN=$(command -v codex || true)
fi
if [ -z "$CODEX_BIN" ] || [ ! -x "$CODEX_BIN" ]; then
    echo "Set CODEX_BIN to a Codex CLI executable." >&2
    exit 1
fi

TMP_DIR=$(mktemp -d)
MARKETPLACE="$TMP_DIR/marketplace"
CODEX_HOME="$TMP_DIR/codex-home"
PROJECT="$TMP_DIR/empty-project"
mkdir -p \
    "$MARKETPLACE/.agents/plugins" \
    "$MARKETPLACE/plugins" \
    "$CODEX_HOME" \
    "$PROJECT"
trap 'rm -rf "$TMP_DIR"' EXIT

cp -a "$ROOT/plugins/better-colab" "$MARKETPLACE/plugins/better-colab"
python3 -c '
import json
from pathlib import Path
import sys

payload = {
    "name": "better-colab-smoke",
    "interface": {"displayName": "Better Colab Smoke"},
    "plugins": [{
        "name": "better-colab",
        "source": {
            "source": "local",
            "path": "./plugins/better-colab",
        },
        "policy": {
            "installation": "AVAILABLE",
            "authentication": "ON_INSTALL",
        },
        "category": "Developer Tools",
    }],
}
Path(sys.argv[1]).write_text(
    json.dumps(payload, indent=2) + "\n",
    encoding="utf-8",
)
' "$MARKETPLACE/.agents/plugins/marketplace.json"

echo "[*] Installing through an isolated temporary marketplace..."
CODEX_HOME="$CODEX_HOME" "$CODEX_BIN" plugin marketplace add \
    "$MARKETPLACE" --json >"$TMP_DIR/marketplace-add.json"
CODEX_HOME="$CODEX_HOME" "$CODEX_BIN" plugin add \
    better-colab@better-colab-smoke --json >"$TMP_DIR/plugin-add.json"
CODEX_HOME="$CODEX_HOME" "$CODEX_BIN" plugin list \
    --json >"$TMP_DIR/plugin-list.json"

python3 -c '
import json
import sys

installed = json.load(open(sys.argv[1], encoding="utf-8"))
listed = json.load(open(sys.argv[2], encoding="utf-8"))
assert installed["pluginId"] == "better-colab@better-colab-smoke", installed
assert installed["version"] == "0.1.0", installed
assert "better-colab@better-colab-smoke" in json.dumps(listed), listed
' "$TMP_DIR/plugin-add.json" "$TMP_DIR/plugin-list.json"

echo "[*] Verifying fresh-context skill pickup outside the source repository..."
(
    cd "$PROJECT"
    CODEX_HOME="$CODEX_HOME" "$CODEX_BIN" debug prompt-input \
        'Use $better-colab to describe the safe detached workflow.' \
        >"$TMP_DIR/prompt.json"
)
python3 -c '
import json
import sys

prompt = json.load(open(sys.argv[1], encoding="utf-8"))
serialized = json.dumps(prompt)
assert "better-colab:better-colab" in serialized, serialized
assert (
    "plugins/cache/better-colab-smoke/better-colab/0.1.0/"
    "skills/better-colab/SKILL.md"
) in serialized, serialized
assert "Use $better-colab" in serialized, serialized
' "$TMP_DIR/prompt.json"

echo "[SUCCESS] Isolated plugin install and fresh-context pickup passed."
