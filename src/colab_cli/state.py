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

import json
import os
from datetime import datetime
from io import StringIO
from typing import Dict, Optional, Tuple

import filelock
from pydantic import BaseModel

from colab_cli.private_files import (
    PrivatePathError,
    atomic_write_private_text,
    ensure_private_directory,
    ensure_private_file,
    read_private_text,
)


class LocalStateError(RuntimeError):
    """A local state file is malformed or unsafe to access."""


class SessionState(BaseModel):
    name: str
    token: str
    url: str
    endpoint: str
    variant: str = "DEFAULT"
    accelerator: str = "NONE"
    kernel_id: Optional[str] = None
    session_id: Optional[str] = None
    last_execution: Optional[Tuple[str, Optional[str], str]] = None
    running: Optional[str] = None
    keep_alive_pid: Optional[int] = None


class Settings(BaseModel):
    update_url: str = "https://pypi.org/pypi/better-google-colab-cli/json"
    last_check: Optional[datetime] = None
    enable_update_check: bool = True
    # Highest version seen on the update source; cached for the banner.
    latest_version: Optional[str] = None


class _LockedFileStore:
    def __init__(self, path: str, *, managed_directory: bool = False):
        self.path = path
        self.lock_path = "%s.lock" % self.path
        # ReadWriteLock gives us shared (concurrent) readers and exclusive
        # writers -- the cross-platform equivalent of fcntl LOCK_SH/LOCK_EX.
        # is_singleton=False keeps each store's lock independent: with the
        # default (True), two StateStore instances for the same path in one
        # process are merged into a single reentrant lock, whose reentrancy
        # guard then raises RuntimeError when two threads contend for the write
        # lock. We want them to actually serialize via the underlying file lock.
        try:
            self._ensure_dir(managed_directory=managed_directory)
            ensure_private_file(self.path)
            ensure_private_file(self.lock_path)
        except PrivatePathError as error:
            raise LocalStateError(f"unsafe local state path: {self.path}") from error
        self._rwlock = filelock.ReadWriteLock(self.lock_path, is_singleton=False)

    def _ensure_dir(self, *, managed_directory: bool):
        ensure_private_directory(
            os.path.dirname(self.path), harden_existing=managed_directory
        )

    def _read_text(self) -> str:
        try:
            return read_private_text(self.path)
        except PrivatePathError as error:
            raise LocalStateError(f"Unable to read local state: {self.path}") from error

    def _write_text(self, data: str) -> None:
        try:
            atomic_write_private_text(self.path, data)
        except (OSError, PrivatePathError) as error:
            raise LocalStateError(
                f"Unable to write local state: {self.path}"
            ) from error

    def _read_locked(self) -> StringIO:
        with self._rwlock.read_lock():
            return StringIO(self._read_text())

    def _invalid(self, kind: str, error: Exception) -> LocalStateError:
        return LocalStateError(f"invalid {kind} state in {self.path}")


class SettingsStore(_LockedFileStore):
    def __init__(self, path: Optional[str] = None):
        managed_directory = path is None
        if not path:
            path = os.path.expanduser("~/.config/colab-cli/settings.json")
        super().__init__(path, managed_directory=managed_directory)

    def load(self) -> Settings:
        f = self._read_locked()
        try:
            content = f.read()
            if not content or content.isspace():
                return Settings()
            data = json.loads(content)
            return Settings.model_validate(data)
        except Exception as error:
            raise self._invalid("settings", error) from error

    def save(self, settings: Settings):
        with self._rwlock.write_lock():
            self._write_text(settings.model_dump_json(indent=2))


class StateStore(_LockedFileStore):
    def __init__(self, path: Optional[str] = None):
        managed_directory = path is None
        if not path:
            path = os.path.expanduser("~/.config/colab-cli/sessions.json")
        super().__init__(path, managed_directory=managed_directory)

    def _load_raw(self, f: StringIO) -> Dict[str, SessionState]:
        content = f.read()
        if not content or content.isspace():
            return {}
        try:
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError("session state must be a JSON object")
            return {k: SessionState(**v) for k, v in data.items()}
        except Exception as error:
            raise self._invalid("session", error) from error

    def _save_raw(self, sessions: Dict[str, SessionState]):
        content = json.dumps({k: v.model_dump() for k, v in sessions.items()}, indent=2)
        self._write_text(content)

    def add(self, state: SessionState):
        with self._rwlock.write_lock():
            sessions = self._load_raw(StringIO(self._read_text()))
            sessions[state.name] = state
            self._save_raw(sessions)

    def get(self, name: str) -> Optional[SessionState]:
        with self._rwlock.read_lock():
            sessions = self._load_raw(StringIO(self._read_text()))
            return sessions.get(name)

    def remove(self, name: str):
        with self._rwlock.write_lock():
            sessions = self._load_raw(StringIO(self._read_text()))
            if name in sessions:
                del sessions[name]
                self._save_raw(sessions)

    def list(self) -> Dict[str, SessionState]:
        with self._rwlock.read_lock():
            return self._load_raw(StringIO(self._read_text()))
