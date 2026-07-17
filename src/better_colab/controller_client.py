"""Synchronous Unix-socket client and single-instance startup election."""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import filelock

from better_colab.controller_protocol import (
    ProtocolError,
    encode_frame,
    recv_frame,
)
from better_colab.errors import BetterColabError, ExitCode, api_error
from better_colab.models import ErrorDetail
from better_colab.protocol import INTERNAL_PROTOCOL_VERSION
from better_colab.storage import ProfileSpec, StatePaths


class ControllerClient:
    """Blocking local RPC client with safe controller autostart."""

    _processes: dict[Path, subprocess.Popen] = {}
    _processes_lock = threading.Lock()

    def __init__(
        self,
        *,
        paths: StatePaths | None = None,
        startup_timeout: float = 5.0,
        connect_timeout: float = 1.0,
    ):
        self.paths = paths or StatePaths.discover()
        self.startup_timeout = startup_timeout
        self.connect_timeout = connect_timeout
        self._last_pid: int | None = None

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        frame = {
            "protocol_version": INTERNAL_PROTOCOL_VERSION,
            "request_id": request_id,
            "method": method,
            "params": params or {},
        }
        socket_timeout = self.connect_timeout if timeout is None else timeout
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(socket_timeout)
                connection.connect(str(self.paths.socket))
                connection.sendall(encode_frame(frame))
                response = recv_frame(connection)
        except (OSError, ProtocolError) as error:
            raise api_error(
                "CONTROLLER_NOT_RUNNING",
                "Better Colab controller is not reachable",
                exit_code=ExitCode.UNAVAILABLE,
                retryable=True,
                suggested_action="start_controller",
                details={"error": str(error)},
            ) from error

        if response.get("protocol_version") != INTERNAL_PROTOCOL_VERSION:
            raise api_error(
                "PROTOCOL_VERSION_MISMATCH",
                "Controller response uses an incompatible protocol",
                exit_code=ExitCode.CONFLICT,
                retryable=False,
                suggested_action="restart_controller_with_matching_cli",
            )
        if response.get("request_id") != request_id:
            raise api_error(
                "CONTROLLER_RESPONSE_MISMATCH",
                "Controller response request_id did not match",
                exit_code=ExitCode.CONFLICT,
                retryable=False,
                suggested_action="restart_controller",
            )
        if not response.get("ok"):
            raw_error = response.get("error") or {}
            raise BetterColabError(
                ErrorDetail(
                    code=str(raw_error.get("code") or "CONTROLLER_ERROR"),
                    message=str(
                        raw_error.get("message") or "Controller request failed"
                    ),
                    retryable=bool(raw_error.get("retryable", False)),
                    suggested_action=str(
                        raw_error.get("suggested_action")
                        or "inspect_controller_status"
                    ),
                    details=raw_error.get("details"),
                ),
                ExitCode(response.get("exit_code", ExitCode.UNAVAILABLE)),
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise api_error(
                "CONTROLLER_RESPONSE_INVALID",
                "Controller result must be an object",
                exit_code=ExitCode.CONFLICT,
                retryable=False,
                suggested_action="restart_controller",
            )
        if "pid" in result:
            self._last_pid = int(result["pid"])
        return result

    def status(self) -> dict[str, Any]:
        return self.request("controller.status")

    def ensure_running(self) -> dict[str, Any]:
        try:
            return self.status()
        except BetterColabError as error:
            if error.error.code != "CONTROLLER_NOT_RUNNING":
                raise

        self.paths.ensure()
        election = filelock.FileLock(
            str(self.paths.startup_lock),
            mode=0o600,
        )
        try:
            election.acquire(timeout=self.startup_timeout)
        except filelock.Timeout as error:
            raise api_error(
                "CONTROLLER_START_TIMEOUT",
                "Timed out waiting for controller startup election",
                exit_code=ExitCode.UNAVAILABLE,
                retryable=True,
                suggested_action="retry_controller_start",
            ) from error
        try:
            self.paths.startup_lock.chmod(0o600)
            try:
                return self.status()
            except BetterColabError as error:
                if error.error.code != "CONTROLLER_NOT_RUNNING":
                    raise

            deadline = time.monotonic() + self.startup_timeout
            if self._lifetime_lock_held():
                return self._wait_for_start(deadline)

            # The election winner is the only process allowed to unlink these
            # stale endpoints, and only after proving no lifetime owner exists.
            self.paths.socket.unlink(missing_ok=True)
            self.paths.pid_file.unlink(missing_ok=True)
            self._spawn_controller()
            return self._wait_for_start(deadline)
        finally:
            election.release()

    def _lifetime_lock_held(self) -> bool:
        probe = filelock.FileLock(
            str(self.paths.lifetime_lock),
            mode=0o600,
        )
        try:
            probe.acquire(timeout=0)
        except filelock.Timeout:
            return True
        else:
            probe.release()
            return False

    def _spawn_controller(self) -> None:
        descriptor = os.open(
            self.paths.log_file,
            os.O_CREAT | os.O_APPEND | os.O_WRONLY,
            0o600,
        )
        log = os.fdopen(descriptor, "ab", buffering=0)
        command = [
            sys.executable,
            "-m",
            "better_colab.controller",
            "serve",
            "--state-dir",
            str(self.paths.state_dir),
            "--runtime-dir",
            str(self.paths.runtime_dir),
        ]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=(sys.platform != "win32"),
                close_fds=True,
            )
        finally:
            log.close()
        self.paths.log_file.chmod(0o600)
        with self._processes_lock:
            self._processes[self.paths.socket] = process

    def _wait_for_start(self, deadline: float) -> dict[str, Any]:
        last_error: BetterColabError | None = None
        while time.monotonic() < deadline:
            try:
                return self.status()
            except BetterColabError as error:
                if error.error.code != "CONTROLLER_NOT_RUNNING":
                    raise
                last_error = error
            time.sleep(0.01)
        raise api_error(
            "CONTROLLER_START_TIMEOUT",
            "Controller did not become ready before the startup deadline",
            exit_code=ExitCode.UNAVAILABLE,
            retryable=True,
            suggested_action="inspect_controller_log",
            details=(
                {"last_error": last_error.error.message}
                if last_error is not None
                else None
            ),
        )

    @staticmethod
    def _profile_params(profile: ProfileSpec) -> dict[str, Any]:
        return {
            "profile": {
                "config_path": str(profile.config_path),
                "auth_provider": profile.auth_provider,
                "oauth_config_path": str(profile.oauth_config_path),
            }
        }

    def ensure_profile(self, profile: ProfileSpec) -> dict[str, Any]:
        self.ensure_running()
        return self.request("profile.ensure", self._profile_params(profile))

    def list_profile_sessions(
        self,
        profile: ProfileSpec,
    ) -> list[dict[str, Any]]:
        self.ensure_running()
        result = self.request("profile.sessions", self._profile_params(profile))
        return result["sessions"]

    def wait_condition(
        self,
        *,
        topic: str,
        after_revision: int,
        timeout: float | None,
    ) -> dict[str, Any]:
        self.ensure_running()
        socket_timeout = (
            max(self.connect_timeout, timeout + 1)
            if timeout is not None
            else max(self.connect_timeout, 3600)
        )
        return self.request(
            "condition.wait",
            {
                "topic": topic,
                "after_revision": after_revision,
                "timeout": timeout,
            },
            timeout=socket_timeout,
        )

    def notify_condition(
        self,
        *,
        topic: str,
        payload: Any,
    ) -> dict[str, Any]:
        self.ensure_running()
        return self.request(
            "condition.notify",
            {"topic": topic, "payload": payload},
        )

    def stop(self, *, force: bool = False) -> dict[str, Any]:
        status = self.status()
        self._last_pid = status["pid"]
        return self.request("controller.stop", {"force": force})

    def wait_until_stopped(self, *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.status()
            except BetterColabError as error:
                if error.error.code == "CONTROLLER_NOT_RUNNING":
                    self._reap_spawned_process()
                    return
                raise
            time.sleep(0.01)
        raise api_error(
            "CONTROLLER_STOP_TIMEOUT",
            "Controller did not stop before the deadline",
            exit_code=ExitCode.UNAVAILABLE,
            retryable=True,
            suggested_action="inspect_controller_log",
        )

    def _reap_spawned_process(self) -> None:
        with self._processes_lock:
            process = self._processes.pop(self.paths.socket, None)
        if process is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)
