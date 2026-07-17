#!/usr/bin/env bash
set -euo pipefail

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

export XDG_STATE_HOME="$work/state"
export XDG_RUNTIME_DIR="$work/runtime"

output="$(
  better-colab \
    --config "$work/legacy-sessions.json" \
    execution prune \
    --before 2099-01-01T00:00:00Z \
    --format json
)"

python - "$output" "$XDG_STATE_HOME/better-colab/controller.sqlite3" <<'PY'
import json
import os
import stat
import sys

payload = json.loads(sys.argv[1])
assert payload == {
    "schema_version": 1,
    "ok": True,
    "result": {
        "dry_run": True,
        "matched": 0,
        "deleted": 0,
        "execution_ids": [],
        "artifact_bytes": 0,
    },
}
database = sys.argv[2]
assert os.path.isfile(database)
assert stat.S_IMODE(os.stat(database).st_mode) == 0o600
PY
