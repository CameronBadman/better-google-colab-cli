"""Single-instance per-user Better Colab controller process."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import filelock

from better_colab.controller_protocol import (
    FrameTooLargeError,
    ProtocolError,
    read_frame,
    write_frame,
)
from better_colab.errors import BetterColabError, ExitCode, api_error
from better_colab.protocol import INTERNAL_PROTOCOL_VERSION
from better_colab.storage import DurableStore, ProfileSpec, StatePaths


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class _Topic:
    condition: asyncio.Condition
    revision: int = 0
    payload: Any = None


class ControllerServer:
    def __init__(self, *, paths: StatePaths):
        self.paths = paths
        self.started_at = _timestamp()
        self._server: asyncio.AbstractServer | None = None
        self._stop_event = asyncio.Event()
        self._shutdown_requested = False
        self._writers: set[asyncio.StreamWriter] = set()
        self._topics: dict[str, _Topic] = {}
        self._lifetime_lock = filelock.FileLock(str(paths.lifetime_lock))
        self._metadata_store: DurableStore | None = None

    def _default_profile(self) -> ProfileSpec:
        return ProfileSpec.from_values(
            config_path=None,
            auth_provider="oauth2",
            oauth_config_path=None,
        )

    async def start(self) -> None:
        self.paths.ensure()
        try:
            self._lifetime_lock.acquire(timeout=0)
        except filelock.Timeout as error:
            raise RuntimeError("another controller holds the lifetime lock") from error
        self.paths.lifetime_lock.chmod(0o600)
        if self.paths.socket.exists():
            self._lifetime_lock.release()
            raise RuntimeError(
                "controller socket already exists; startup election must "
                "remove stale sockets"
            )
        self._metadata_store = DurableStore(
            paths=self.paths,
            profile=self._default_profile(),
        )
        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            path=str(self.paths.socket),
        )
        self.paths.socket.chmod(0o600)
        self._write_pid()

    def _write_pid(self) -> None:
        descriptor = os.open(
            self.paths.pid_file,
            os.O_CREAT | os.O_TRUNC | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(descriptor, f"{os.getpid()}\n".encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.paths.pid_file.chmod(0o600)

    async def run(self) -> None:
        await self.start()
        try:
            await self._stop_event.wait()
        finally:
            await self.close()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for writer in list(self._writers):
            writer.close()
        for writer in list(self._writers):
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        self._writers.clear()
        if self._metadata_store is not None:
            self._metadata_store.close()
            self._metadata_store = None
        self.paths.socket.unlink(missing_ok=True)
        self.paths.pid_file.unlink(missing_ok=True)
        if self._lifetime_lock.is_locked:
            self._lifetime_lock.release()

    def request_signal_stop(self) -> None:
        asyncio.create_task(self._force_and_stop("controller_signal"))

    async def _force_and_stop(self, reason: str) -> None:
        self._force_active_uncertain(reason=reason)
        self._stop_event.set()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._writers.add(writer)
        try:
            while True:
                try:
                    request = await read_frame(reader)
                except EOFError:
                    break
                except (ProtocolError, FrameTooLargeError) as error:
                    await write_frame(
                        writer,
                        self._error_response(
                            request_id=None,
                            error=api_error(
                                "PROTOCOL_ERROR",
                                str(error),
                                exit_code=ExitCode.USAGE,
                                retryable=False,
                                suggested_action="restart_controller_client",
                            ),
                        ),
                    )
                    break

                request_id = request.get("request_id")
                try:
                    response = await self._dispatch(request)
                except BetterColabError as error:
                    frame = self._error_response(request_id, error)
                except Exception:
                    frame = self._error_response(
                        request_id,
                        api_error(
                            "CONTROLLER_INTERNAL_ERROR",
                            "Controller request failed",
                            exit_code=ExitCode.UNAVAILABLE,
                            retryable=True,
                            suggested_action="inspect_controller_log",
                        ),
                    )
                else:
                    frame = {
                        "protocol_version": INTERNAL_PROTOCOL_VERSION,
                        "request_id": request_id,
                        "ok": True,
                        "result": response,
                    }
                await write_frame(writer, frame)
                if self._shutdown_requested:
                    self._stop_event.set()
                    break
        finally:
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _error_response(
        self,
        request_id: str | None,
        error: BetterColabError,
    ) -> dict[str, Any]:
        return {
            "protocol_version": INTERNAL_PROTOCOL_VERSION,
            "request_id": request_id,
            "ok": False,
            "error": error.error.to_wire(),
            "exit_code": int(error.exit_code),
        }

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise api_error(
                "REQUEST_ID_REQUIRED",
                "Internal requests require a non-empty request_id",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="fix_controller_client",
            )
        version = request.get("protocol_version")
        if version != INTERNAL_PROTOCOL_VERSION:
            raise api_error(
                "PROTOCOL_VERSION_MISMATCH",
                f"Controller protocol is {INTERNAL_PROTOCOL_VERSION}; "
                f"client requested {version}",
                exit_code=ExitCode.CONFLICT,
                retryable=False,
                suggested_action="use_matching_cli_and_controller_versions",
            )
        method = request.get("method")
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise api_error(
                "INVALID_PARAMS",
                "params must be an object",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="fix_controller_client",
            )

        if method in {"hello", "controller.status"}:
            return self._status()
        if method == "profile.ensure":
            profile = self._profile_from_params(params)
            with DurableStore(paths=self.paths, profile=profile) as store:
                return {
                    "profile_id": profile.profile_id,
                    "diagnostics": [item.to_wire() for item in store.diagnostics],
                }
        if method == "profile.sessions":
            profile = self._profile_from_params(params)
            with DurableStore(paths=self.paths, profile=profile) as store:
                return {
                    "sessions": [
                        {
                            "name": session.name,
                            "endpoint": session.endpoint,
                            "hardware": (
                                "CPU"
                                if session.hardware == "NONE"
                                else session.hardware
                            ),
                            "variant": session.variant,
                        }
                        for session in store.list_sessions()
                    ]
                }
        if method == "condition.wait":
            return await self._wait_condition(params)
        if method == "condition.notify":
            return await self._notify_condition(params)
        if method == "controller.stop":
            force = bool(params.get("force", False))
            active = self._active_execution_count()
            if active and not force:
                raise api_error(
                    "CONTROLLER_BUSY",
                    f"Controller has {active} active execution(s)",
                    exit_code=ExitCode.CONFLICT,
                    retryable=False,
                    suggested_action="wait_or_force_controller_stop",
                    details={"active_executions": active},
                )
            affected = (
                self._force_active_uncertain(reason="forced_controller_stop")
                if force
                else []
            )
            self._shutdown_requested = True
            return {"stopping": True, "forced": force, "affected": affected}

        raise api_error(
            "METHOD_NOT_FOUND",
            f"Unknown controller method: {method}",
            exit_code=ExitCode.USAGE,
            retryable=False,
            suggested_action="refresh_capabilities",
        )

    def _status(self) -> dict[str, Any]:
        return {
            "controller_alive": True,
            "pid": os.getpid(),
            "protocol_version": INTERNAL_PROTOCOL_VERSION,
            "started_at": self.started_at,
            "active_executions": self._active_execution_count(),
        }

    def _profile_from_params(self, params: dict[str, Any]) -> ProfileSpec:
        profile = params.get("profile")
        if not isinstance(profile, dict):
            raise api_error(
                "PROFILE_REQUIRED",
                "profile parameters are required",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="fix_controller_client",
            )
        return ProfileSpec.from_values(
            config_path=profile.get("config_path"),
            auth_provider=str(profile.get("auth_provider") or "oauth2"),
            oauth_config_path=profile.get("oauth_config_path"),
        )

    def _topic(self, name: str) -> _Topic:
        topic = self._topics.get(name)
        if topic is None:
            topic = _Topic(condition=asyncio.Condition())
            self._topics[name] = topic
        return topic

    async def _wait_condition(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("topic") or "")
        if not name:
            raise api_error(
                "TOPIC_REQUIRED",
                "condition topic is required",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="fix_controller_client",
            )
        after_revision = int(params.get("after_revision", 0))
        timeout = params.get("timeout")
        timeout_value = None if timeout is None else max(0.0, float(timeout))
        topic = self._topic(name)
        timed_out = False
        async with topic.condition:
            if topic.revision <= after_revision:
                try:
                    await asyncio.wait_for(
                        topic.condition.wait_for(
                            lambda: topic.revision > after_revision
                        ),
                        timeout=timeout_value,
                    )
                except TimeoutError:
                    timed_out = True
            result: dict[str, Any] = {
                "revision": topic.revision,
                "wait_timed_out": timed_out,
            }
            if topic.payload is not None and topic.revision > after_revision:
                result["payload"] = topic.payload
            return result

    async def _notify_condition(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("topic") or "")
        if not name:
            raise api_error(
                "TOPIC_REQUIRED",
                "condition topic is required",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="fix_controller_client",
            )
        topic = self._topic(name)
        async with topic.condition:
            topic.revision += 1
            topic.payload = params.get("payload")
            topic.condition.notify_all()
        return {"revision": topic.revision}

    def _profile_specs(self) -> list[ProfileSpec]:
        assert self._metadata_store is not None
        rows = self._metadata_store.connection.execute(
            """
            SELECT config_path, auth_provider, oauth_config_path
            FROM profiles ORDER BY profile_id
            """
        )
        return [
            ProfileSpec.from_values(
                config_path=row["config_path"],
                auth_provider=row["auth_provider"],
                oauth_config_path=row["oauth_config_path"],
            )
            for row in rows
        ]

    def _active_execution_count(self) -> int:
        assert self._metadata_store is not None
        return self._metadata_store.connection.execute(
            """
            SELECT COUNT(*) FROM executions
            WHERE state IN ('dispatching', 'running', 'disconnected')
            """
        ).fetchone()[0]

    def _force_active_uncertain(self, *, reason: str) -> list[str]:
        affected: list[str] = []
        for profile in self._profile_specs():
            with DurableStore(paths=self.paths, profile=profile) as store:
                affected.extend(store.force_uncertain_active(reason=reason))
        return sorted(affected)


async def _serve(paths: StatePaths) -> None:
    server = ControllerServer(paths=paths)
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, server.request_signal_stop)
    await server.run()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="better-colab-controller")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--state-dir", required=True)
    serve.add_argument("--runtime-dir", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "serve":
        paths = StatePaths(
            state_dir=Path(args.state_dir),
            runtime_dir=Path(args.runtime_dir),
        )
        try:
            asyncio.run(_serve(paths))
        except Exception as error:
            sys.stderr.write(f"controller failed: {error}\n")
            return 1
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
