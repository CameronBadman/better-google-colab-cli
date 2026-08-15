"""One-shot local migrations for credential-bearing compatibility state."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import filelock

from colab_cli.private_files import (
    atomic_write_private_text,
    ensure_private_directory,
    ensure_private_file,
    read_private_text,
)


REDACTED = "<redacted>"


def _redact_event(event: dict[str, Any]) -> bool:
    changed = False
    event_type = event.get("event_type")
    field = {
        "drive_auth_needed": "uri",
        "stdin_request": "prompt",
        "input_reply": "value",
    }.get(event_type)
    if field and event.get(field) != REDACTED:
        event[field] = REDACTED
        changed = True

    if event_type == "keep_alive_error" and "response_body" in event:
        del event["response_body"]
        changed = True

    last_error = event.get("last_error")
    if isinstance(last_error, dict) and "response_body" in last_error:
        del last_error["response_body"]
        changed = True
    return changed


def scrub_legacy_history(
    log_dir: str | os.PathLike[str] = "~/.config/colab-cli/history",
) -> int:
    """Redact known legacy credential fields without altering other history."""

    directory = Path(log_dir).expanduser()
    ensure_private_directory(directory, harden_existing=True)
    migration_lock_path = directory / ".security-migration.lock"
    ensure_private_file(migration_lock_path)
    changed_events = 0

    migration_lock = filelock.ReadWriteLock(
        str(migration_lock_path), is_singleton=False
    )
    with migration_lock.write_lock():
        for path in sorted(directory.glob("*.jsonl")):
            if path.is_symlink():
                raise RuntimeError(f"unsafe history path: {path}")
            ensure_private_file(path, create=False)
            file_lock_path = Path(f"{path}.lock")
            ensure_private_file(file_lock_path)
            file_lock = filelock.ReadWriteLock(str(file_lock_path), is_singleton=False)
            with file_lock.write_lock():
                original = read_private_text(path)
                migrated: list[str] = []
                file_changed = False
                for line_number, line in enumerate(original.splitlines(), start=1):
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise RuntimeError(
                            f"malformed history file {path} at line {line_number}"
                        ) from error
                    if not isinstance(event, dict):
                        raise RuntimeError(
                            f"malformed history file {path} at line {line_number}"
                        )
                    if _redact_event(event):
                        changed_events += 1
                        file_changed = True
                    migrated.append(json.dumps(event))
                if file_changed:
                    atomic_write_private_text(path, "\n".join(migrated) + "\n")
    return changed_events


__all__ = ["scrub_legacy_history"]
