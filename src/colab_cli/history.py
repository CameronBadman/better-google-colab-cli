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

import datetime
import json
import os
from pathlib import Path
from typing import Any, Dict, List

import filelock

from colab_cli.private_files import (
    PrivatePathError,
    append_private_text,
    ensure_private_directory,
    ensure_private_file,
    read_private_text,
)


class HistoryLogger:
    def __init__(self, log_dir: str = "~/.config/colab-cli/history"):
        is_default = log_dir == "~/.config/colab-cli/history"
        self.log_dir = os.path.expanduser(log_dir)
        ensure_private_directory(self.log_dir, harden_existing=is_default)

    def _get_log_path(self, session_name: str) -> str:
        if (
            not session_name
            or session_name in {".", ".."}
            or "\x00" in session_name
            or "/" in session_name
            or "\\" in session_name
        ):
            raise ValueError("Invalid history session name")
        candidate = Path(self.log_dir) / f"{session_name}.jsonl"
        if candidate.parent != Path(self.log_dir):
            raise ValueError("Invalid history session name")
        return str(candidate)

    def _lock(self, log_path: str) -> filelock.ReadWriteLock:
        lock_path = f"{log_path}.lock"
        ensure_private_file(lock_path)
        return filelock.ReadWriteLock(lock_path, is_singleton=False)

    def log_event(self, session_name: str, event_type: str, data: Dict[str, Any]):
        """
        Appends a structured event to the session's history file.

        event_types:
          - session_created
          - session_terminated
          - execution (code + outputs)
          - input_requested (stdin prompts/replies)
          - file_operation (ls, rm, upload, download)
          - automation (auth, install, drivemount)
        """
        reserved = {"timestamp", "event_type"}.intersection(data)
        if reserved:
            raise ValueError(
                f"History data contains reserved fields: {sorted(reserved)}"
            )
        log_path = self._get_log_path(session_name)
        event = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "event_type": event_type,
            **data,
        }
        lock = self._lock(log_path)
        with lock.write_lock():
            append_private_text(log_path, json.dumps(event) + "\n")

    def list_sessions(self) -> List[str]:
        if not os.path.exists(self.log_dir):
            return []
        sessions = []
        for name in os.listdir(self.log_dir):
            path = Path(self.log_dir) / name
            if name.endswith(".jsonl") and path.is_file() and not path.is_symlink():
                sessions.append(name[:-6])
        return sessions

    def get_history(self, session_name: str) -> List[Dict[str, Any]]:
        log_path = self._get_log_path(session_name)
        if not os.path.exists(log_path):
            return []

        lock = self._lock(log_path)
        history = []
        try:
            with lock.read_lock():
                contents = read_private_text(log_path)
        except PrivatePathError as error:
            raise RuntimeError(f"Unsafe history path: {log_path}") from error
        for line in contents.splitlines():
            if line.strip():
                history.append(json.loads(line))
        return history
