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

import logging
import logging.handlers
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from colab_cli.auth import AuthProvider, get_credentials
from colab_cli.client import Client, Prod
from colab_cli.history import HistoryLogger
from colab_cli.private_files import ensure_private_directory, ensure_private_file
from colab_cli.state import StateStore, SettingsStore


class State:
    def __init__(self):
        self.client_oauth_config = os.path.expanduser("~/.colab-cli-oauth-config.json")
        self.config_path = None
        self.logtostderr = False
        self.auth_provider = AuthProvider.OAUTH2
        self.durable_wrappers = False
        self._client = None
        self._store = None
        self._settings_store = None
        self._history = None
        self._sessions = None

    @property
    def store(self):
        if self._store is None:
            self._store = StateStore(self.config_path)
        return self._store

    @property
    def settings_store(self):
        if self._settings_store is None:
            # We don't currently allow overriding settings path via CLI,
            # but we could if needed. For now, use default.
            self._settings_store = SettingsStore()
        return self._settings_store

    @property
    def history(self):
        if self._history is None:
            self._history = HistoryLogger()
        return self._history

    @property
    def client(self):
        if self._client is None:
            creds = get_credentials(
                self.client_oauth_config, provider=self.auth_provider
            )
            self._client = Client(Prod(), creds)
        return self._client

    def prune_session(self, name: str):
        """Removes a session from local state and kills its keep-alive process."""
        s = self.store.get(name)
        if s and s.keep_alive_pid:
            kill_process(s.keep_alive_pid)
        self.store.remove(name)
        if self._sessions and name in self._sessions:
            del self._sessions[name]
        self.history.log_event(name, "session_terminated", {"reason": "pruned"})

    def sync_sessions(self, *, emit_diagnostics: bool = True):
        if self._sessions is not None:
            return self._sessions, self.client.list_assignments()

        # Check local store first. If it's empty, we don't necessarily need to hit the backend
        # unless we are specifically looking for server-side assignments (e.g. 'colab sessions').
        local_sessions = self.store.list()
        if not local_sessions:
            self._sessions = {}
            # We still need to return assignments for 'colab sessions' to work
            # But we only trigger client creation (and thus auth) if we have to.
            assignments = self.client.list_assignments()
            return self._sessions, assignments

        assignments = self.client.list_assignments()
        active_endpoints = {a.endpoint for a in assignments}

        self._sessions = local_sessions
        pruned = 0
        for name, s in list(self._sessions.items()):
            if s.endpoint not in active_endpoints:
                self.prune_session(name)
                pruned += 1

        if pruned > 0 and emit_diagnostics:
            typer.echo(f"[colab] Pruned {pruned} stale local session(s).")

        return self._sessions, assignments

    def resolve_session(self, session_name: Optional[str]) -> str:
        if session_name:
            return session_name

        # Check local store first to avoid hitting the backend (and triggering auth) if we don't have to
        local_sessions = self.store.list()
        if not local_sessions:
            typer.echo(
                "[colab] Error: No active sessions found. Create one with 'colab new'."
            )
            raise typer.Exit(1)

        # If we have local sessions, we need to sync to make sure they are still valid.
        # This will trigger auth if valid credentials are not present.
        sessions, _ = self.sync_sessions()
        active_names = list(sessions.keys())

        if len(active_names) == 1:
            name = active_names[0]
            typer.echo(f"[colab] Using unique session '{name}'.")
            return name
        elif len(active_names) > 1:
            typer.echo(
                f"[colab] Error: Multiple active sessions found. Specify one with -s: {', '.join(active_names)}"
            )
            raise typer.Exit(1)
        else:
            typer.echo(
                "[colab] Error: No active sessions found. Create one with 'colab new'."
            )
            raise typer.Exit(1)


state = State()


def kill_process(pid: int):
    """Safely terminates a process by PID."""
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
        # Give it a moment to exit
        for _ in range(5):
            time.sleep(0.1)
            os.kill(pid, 0)
    except OSError:
        # Already dead
        pass
    except Exception:
        logging.debug(f"Failed to kill process {pid}")


class _CredentialRedactionFilter(logging.Filter):
    _patterns = (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
        re.compile(
            r"(?i)([?&](?:access_token|refresh_token|id_token|token|code|"
            r"colab-runtime-proxy-token|x-goog-colab-token)=)[^&#\s]+"
        ),
        re.compile(
            r"(?i)((?:authorization|proxy-authorization|cookie|set-cookie|"
            r"access_token|refresh_token|id_token|client_secret|token|code)"
            r"\s*[:=]\s*)[^,\s}\]]+"
        ),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        for pattern in self._patterns:
            replacement = r"\1<redacted>" if pattern.groups else "<redacted>"
            rendered = pattern.sub(replacement, rendered)
        record.msg = rendered
        record.args = ()
        return True


class _PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    def _open(self):
        ensure_private_file(self.baseFilename)
        return super()._open()

    def doRollover(self):
        super().doRollover()
        for index in range(1, self.backupCount + 1):
            backup = f"{self.baseFilename}.{index}"
            if os.path.exists(backup):
                ensure_private_file(backup, create=False)


def setup_logging(log_to_stderr: bool):
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    logger = logging.getLogger()
    logger.setLevel(logging.WARNING)
    logging.getLogger("colab_cli").setLevel(logging.DEBUG)
    logging.getLogger("better_colab").setLevel(logging.DEBUG)
    for noisy_logger in ("urllib3", "requests", "websocket", "google.auth"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    log_dir = os.path.expanduser("~/.config/colab-cli")
    ensure_private_directory(log_dir, harden_existing=True)
    log_path = str(Path(log_dir) / "colab.log")

    file_handler = next(
        (
            handler
            for handler in logger.handlers
            if getattr(handler, "_colab_cli_handler", False)
            and getattr(handler, "baseFilename", None) == log_path
        ),
        None,
    )
    if file_handler is None:
        file_handler = _PrivateRotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler._colab_cli_handler = True
        file_handler.addFilter(_CredentialRedactionFilter())
        file_handler.setFormatter(logging.Formatter(log_format))
        logger.addHandler(file_handler)

    has_stream = any(
        getattr(handler, "_colab_cli_stderr_handler", False)
        for handler in logger.handlers
    )
    if log_to_stderr and not has_stream:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler._colab_cli_handler = True
        stream_handler._colab_cli_stderr_handler = True
        stream_handler.addFilter(_CredentialRedactionFilter())
        stream_handler.setFormatter(logging.Formatter(log_format))
        logger.addHandler(stream_handler)
