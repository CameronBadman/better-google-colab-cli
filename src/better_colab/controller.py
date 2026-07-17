"""Single-instance per-user Better Colab controller process."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import signal
import sys
import time
import uuid
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
from better_colab.execution import ExecutionCoordinator, TransportFactory
from better_colab.protocol import (
    DEFAULT_EXECUTION_LIMIT,
    DEFAULT_OUTPUT_PAGE_BYTES,
    INTERNAL_PROTOCOL_VERSION,
    MAX_COLLECTION_LIMIT,
    decode_cursor,
    encode_cursor,
)
from better_colab.storage import (
    TERMINAL_STATES,
    DurableStore,
    ExecutionRecord,
    ProfileSpec,
    StatePaths,
)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class _Topic:
    condition: asyncio.Condition
    revision: int = 0
    payload: Any = None


class ControllerServer:
    def __init__(
        self,
        *,
        paths: StatePaths,
        transport_factory: TransportFactory | None = None,
    ):
        self.paths = paths
        self.started_at = _timestamp()
        self._server: asyncio.AbstractServer | None = None
        self._stop_event = asyncio.Event()
        self._shutdown_requested = False
        self._writers: set[asyncio.StreamWriter] = set()
        self._topics: dict[str, _Topic] = {}
        self._lifetime_lock = filelock.FileLock(str(paths.lifetime_lock))
        self._transport_factory = transport_factory
        self._coordinator: ExecutionCoordinator | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _default_profile(self) -> ProfileSpec:
        return ProfileSpec.from_values(
            config_path=None,
            auth_provider="oauth2",
            oauth_config_path=None,
        )

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
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
        with DurableStore(
            paths=self.paths,
            profile=self._default_profile(),
        ):
            pass
        coordinator_kwargs: dict[str, Any] = {
            "paths": self.paths,
            "notify": self._notify_from_worker,
            "notify_batch": self._notify_batch_from_worker,
        }
        if self._transport_factory is not None:
            coordinator_kwargs["transport_factory"] = self._transport_factory
        self._coordinator = ExecutionCoordinator(**coordinator_kwargs)
        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            path=str(self.paths.socket),
        )
        self.paths.socket.chmod(0o600)
        self._write_pid()
        self._reconcile_and_resume()

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
        if self._coordinator is not None:
            self._coordinator.close()
            self._coordinator = None
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
                    logging.exception("Controller request failed")
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
        if method == "session.status":
            return self._session_status(params)
        if method == "session.probe":
            return await self._session_probe(params)
        if method == "execution.start":
            return self._start_execution(params)
        if method == "execution.batch.start":
            return self._start_batch(params)
        if method == "execution.batch.status":
            return self._batch_status(params)
        if method == "execution.batch.wait":
            return await self._wait_batch(params)
        if method == "execution.batch.cancel":
            return self._cancel_batch(params)
        if method == "execution.status":
            return self._execution_status(params)
        if method == "execution.wait":
            return await self._wait_execution(params)
        if method == "execution.output":
            return self._execution_output(params)
        if method == "execution.cancel":
            return self._cancel_execution(params)
        if method == "execution.list":
            return self._list_executions(params)
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

    @staticmethod
    def _execution_id(params: dict[str, Any]) -> str:
        execution_id = params.get("execution_id")
        try:
            parsed = uuid.UUID(str(execution_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise api_error(
                "INVALID_EXECUTION_ID",
                "execution_id must be a UUID",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="use_an_execution_uuid",
            ) from error
        return str(parsed)

    def _start_execution(self, params: dict[str, Any]) -> dict[str, Any]:
        profile = self._profile_from_params(params)
        execution_id = self._execution_id(params)
        session_name = params.get("session")
        source = params.get("source")
        provenance = params.get("provenance")
        if not isinstance(session_name, str) or not session_name:
            raise api_error(
                "SESSION_REQUIRED",
                "session is required",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="specify_session",
            )
        if not isinstance(source, str):
            raise api_error(
                "SOURCE_REQUIRED",
                "UTF-8 source text is required",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="provide_source",
            )
        if not isinstance(provenance, dict):
            raise api_error(
                "PROVENANCE_REQUIRED",
                "source provenance is required",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="provide_source_provenance",
            )
        timeout = params.get("execution_timeout")
        if timeout is not None:
            try:
                timeout = float(timeout)
            except (TypeError, ValueError) as error:
                raise api_error(
                    "INVALID_EXECUTION_TIMEOUT",
                    "execution timeout must be a finite positive number",
                    exit_code=ExitCode.USAGE,
                    retryable=False,
                    suggested_action="use_a_positive_timeout",
                ) from error
            if not math.isfinite(timeout) or timeout <= 0:
                raise api_error(
                    "INVALID_EXECUTION_TIMEOUT",
                    "execution timeout must be a finite positive number",
                    exit_code=ExitCode.USAGE,
                    retryable=False,
                    suggested_action="use_a_positive_timeout",
                )
        idempotency_key = params.get("idempotency_key")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not idempotency_key
        ):
            raise api_error(
                "INVALID_IDEMPOTENCY_KEY",
                "idempotency key must be a non-empty string",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="use_a_nonempty_idempotency_key",
            )
        source_bytes = source.encode("utf-8")
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        canonical_request = {
            "session": session_name,
            "source_sha256": source_hash,
            "provenance": provenance,
            "execution_timeout": timeout,
        }
        with DurableStore(paths=self.paths, profile=profile) as store:
            record = store.create_execution(
                execution_id=execution_id,
                session_name=session_name,
                source=source_bytes,
                provenance=provenance,
                request=canonical_request,
                idempotency_key=idempotency_key,
                execution_timeout_seconds=timeout,
            )
            result = self._execution_result(store, record, include=[])
        assert self._coordinator is not None
        self._coordinator.submit(profile, record.execution_id)
        return result

    @staticmethod
    def _batch_id(params: dict[str, Any]) -> str:
        batch_id = params.get("batch_id")
        try:
            parsed = uuid.UUID(str(batch_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise api_error(
                "INVALID_BATCH_ID",
                "batch_id must be a UUID",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="use_a_batch_uuid",
            ) from error
        return str(parsed)

    def _start_batch(self, params: dict[str, Any]) -> dict[str, Any]:
        profile = self._profile_from_params(params)
        batch_id = self._batch_id(params)
        session_name = params.get("session")
        members = params.get("members")
        if not isinstance(session_name, str) or not session_name:
            raise api_error(
                "SESSION_REQUIRED",
                "session is required",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="specify_session",
            )
        if (
            not isinstance(members, list)
            or not members
            or len(members) > MAX_COLLECTION_LIMIT
        ):
            raise api_error(
                "INVALID_BATCH_MEMBERS",
                f"members must contain 1 to {MAX_COLLECTION_LIMIT} cells",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="select_supported_batch_cells",
            )
        prepared_members: list[dict[str, Any]] = []
        for member in members:
            if not isinstance(member, dict):
                raise api_error(
                    "INVALID_BATCH_MEMBER",
                    "Each batch member must be an object",
                    exit_code=ExitCode.USAGE,
                    retryable=False,
                    suggested_action="fix_batch_request",
                )
            execution_id = self._execution_id(member)
            source = member.get("source")
            provenance = member.get("provenance")
            if not isinstance(source, str) or not isinstance(provenance, dict):
                raise api_error(
                    "INVALID_BATCH_MEMBER",
                    "Each member requires UTF-8 source and provenance",
                    exit_code=ExitCode.USAGE,
                    retryable=False,
                    suggested_action="fix_batch_request",
                )
            source_bytes = source.encode("utf-8")
            prepared_members.append(
                {
                    "execution_id": execution_id,
                    "source": source_bytes,
                    "provenance": provenance,
                    "request": {
                        "batch_id": batch_id,
                        "session": session_name,
                        "source_sha256": hashlib.sha256(
                            source_bytes
                        ).hexdigest(),
                        "provenance": provenance,
                    },
                }
            )
        with DurableStore(paths=self.paths, profile=profile) as store:
            batch = store.create_batch_executions(
                batch_id=batch_id,
                session_name=session_name,
                members=prepared_members,
                continue_on_error=bool(params.get("continue_on_error", False)),
            )
            result = self._batch_result(store, batch)
        assert self._coordinator is not None
        self._coordinator.submit_batch(profile, batch_id)
        return result

    def _batch_status(self, params: dict[str, Any]) -> dict[str, Any]:
        profile = self._profile_from_params(params)
        batch_id = self._batch_id(params)
        with DurableStore(paths=self.paths, profile=profile) as store:
            batch = store.get_batch(batch_id)
            if batch is None:
                raise api_error(
                    "BATCH_NOT_FOUND",
                    f"Batch not found: {batch_id}",
                    exit_code=ExitCode.NOT_FOUND,
                    retryable=False,
                    suggested_action="inspect_batch_id",
                )
            return self._batch_result(store, batch)

    async def _wait_batch(self, params: dict[str, Any]) -> dict[str, Any]:
        profile = self._profile_from_params(params)
        batch_id = self._batch_id(params)
        timeout = params.get("timeout")
        if timeout is not None:
            try:
                timeout = float(timeout)
            except (TypeError, ValueError) as error:
                raise api_error(
                    "INVALID_WAIT_TIMEOUT",
                    "wait timeout must be a finite non-negative number",
                    exit_code=ExitCode.USAGE,
                    retryable=False,
                    suggested_action="use_a_nonnegative_timeout",
                ) from error
            if not math.isfinite(timeout) or timeout < 0:
                raise api_error(
                    "INVALID_WAIT_TIMEOUT",
                    "wait timeout must be a finite non-negative number",
                    exit_code=ExitCode.USAGE,
                    retryable=False,
                    suggested_action="use_a_nonnegative_timeout",
                )
        deadline = None if timeout is None else time.monotonic() + timeout
        topic = self._topic(self._batch_topic(profile, batch_id))
        timed_out = False
        async with topic.condition:
            while True:
                with DurableStore(paths=self.paths, profile=profile) as store:
                    batch = store.get_batch(batch_id)
                    if batch is None:
                        raise api_error(
                            "BATCH_NOT_FOUND",
                            f"Batch not found: {batch_id}",
                            exit_code=ExitCode.NOT_FOUND,
                            retryable=False,
                            suggested_action="inspect_batch_id",
                        )
                    if batch.state in {"finished", "error", "interrupted"}:
                        result = self._batch_result(store, batch)
                        result["wait_timed_out"] = False
                        return result
                remaining = (
                    None
                    if deadline is None
                    else max(0.0, deadline - time.monotonic())
                )
                if remaining == 0:
                    timed_out = True
                    break
                try:
                    await asyncio.wait_for(
                        topic.condition.wait(),
                        timeout=remaining,
                    )
                except TimeoutError:
                    timed_out = True
                    break
        with DurableStore(paths=self.paths, profile=profile) as store:
            batch = store.get_batch(batch_id)
            if batch is None:
                raise api_error(
                    "BATCH_NOT_FOUND",
                    f"Batch not found: {batch_id}",
                    exit_code=ExitCode.NOT_FOUND,
                    retryable=False,
                    suggested_action="inspect_batch_id",
                )
            result = self._batch_result(store, batch)
            result["wait_timed_out"] = timed_out
            return result

    def _cancel_batch(self, params: dict[str, Any]) -> dict[str, Any]:
        profile = self._profile_from_params(params)
        batch_id = self._batch_id(params)
        assert self._coordinator is not None
        batch = self._coordinator.cancel_batch(profile, batch_id)
        with DurableStore(paths=self.paths, profile=profile) as store:
            return self._batch_result(store, batch)

    @staticmethod
    def _session_name(params: dict[str, Any]) -> str:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise api_error(
                "SESSION_REQUIRED",
                "A non-empty session name is required",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="specify_session",
            )
        return name

    def _session_status(self, params: dict[str, Any]) -> dict[str, Any]:
        profile = self._profile_from_params(params)
        assert self._coordinator is not None
        return self._coordinator.session_status(
            profile,
            self._session_name(params),
        ).to_wire()

    async def _session_probe(self, params: dict[str, Any]) -> dict[str, Any]:
        profile = self._profile_from_params(params)
        timeout = params.get("timeout", 10)
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as error:
            raise api_error(
                "INVALID_PROBE_TIMEOUT",
                "Probe timeout must be a positive number",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="use_a_positive_timeout",
            ) from error
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise api_error(
                "INVALID_PROBE_TIMEOUT",
                "Probe timeout must be a finite positive number",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="use_a_positive_timeout",
            )
        assert self._coordinator is not None
        result = await asyncio.to_thread(
            self._coordinator.probe_session,
            profile,
            self._session_name(params),
            timeout=timeout_value,
        )
        return result.to_wire()

    def _execution_status(self, params: dict[str, Any]) -> dict[str, Any]:
        profile = self._profile_from_params(params)
        execution_id = self._execution_id(params)
        include = params.get("include", [])
        if not isinstance(include, list) or any(
            value not in {"provenance", "transitions", "traceback"}
            for value in include
        ):
            raise api_error(
                "INVALID_INCLUDE",
                "include supports provenance, transitions, and traceback",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="use_a_named_expansion",
            )
        with DurableStore(paths=self.paths, profile=profile) as store:
            record = self._require_execution(store, execution_id)
            return self._execution_result(store, record, include=include)

    async def _wait_execution(self, params: dict[str, Any]) -> dict[str, Any]:
        profile = self._profile_from_params(params)
        execution_id = self._execution_id(params)
        timeout = params.get("timeout")
        if timeout is not None:
            try:
                timeout = float(timeout)
            except (TypeError, ValueError) as error:
                raise api_error(
                    "INVALID_WAIT_TIMEOUT",
                    "wait timeout must be a finite non-negative number",
                    exit_code=ExitCode.USAGE,
                    retryable=False,
                    suggested_action="use_a_nonnegative_timeout",
                ) from error
            if not math.isfinite(timeout) or timeout < 0:
                raise api_error(
                    "INVALID_WAIT_TIMEOUT",
                    "wait timeout must be a finite non-negative number",
                    exit_code=ExitCode.USAGE,
                    retryable=False,
                    suggested_action="use_a_nonnegative_timeout",
                )
        deadline = None if timeout is None else time.monotonic() + timeout
        topic = self._topic(self._execution_topic(profile, execution_id))
        timed_out = False
        async with topic.condition:
            while True:
                with DurableStore(paths=self.paths, profile=profile) as store:
                    record = self._require_execution(store, execution_id)
                    if record.state in TERMINAL_STATES:
                        result = self._execution_result(
                            store,
                            record,
                            include=[],
                        )
                        result["wait_timed_out"] = False
                        result["output"] = self._output_page(
                            store,
                            record,
                            cursor=params.get("cursor"),
                            max_bytes=params.get(
                                "max_bytes", DEFAULT_OUTPUT_PAGE_BYTES
                            ),
                        )
                        return result
                remaining = (
                    None
                    if deadline is None
                    else max(0.0, deadline - time.monotonic())
                )
                if remaining == 0:
                    timed_out = True
                    break
                try:
                    await asyncio.wait_for(
                        topic.condition.wait(),
                        timeout=remaining,
                    )
                except TimeoutError:
                    timed_out = True
                    break

        with DurableStore(paths=self.paths, profile=profile) as store:
            record = self._require_execution(store, execution_id)
            result = self._execution_result(store, record, include=[])
            result["wait_timed_out"] = timed_out
            result["output"] = self._output_page(
                store,
                record,
                cursor=params.get("cursor"),
                max_bytes=params.get("max_bytes", DEFAULT_OUTPUT_PAGE_BYTES),
            )
            return result

    def _execution_output(self, params: dict[str, Any]) -> dict[str, Any]:
        profile = self._profile_from_params(params)
        execution_id = self._execution_id(params)
        with DurableStore(paths=self.paths, profile=profile) as store:
            record = self._require_execution(store, execution_id)
            return self._output_page(
                store,
                record,
                cursor=params.get("cursor"),
                max_bytes=params.get("max_bytes", DEFAULT_OUTPUT_PAGE_BYTES),
            )

    def _cancel_execution(self, params: dict[str, Any]) -> dict[str, Any]:
        profile = self._profile_from_params(params)
        execution_id = self._execution_id(params)
        assert self._coordinator is not None
        record = self._coordinator.cancel(profile, execution_id)
        with DurableStore(paths=self.paths, profile=profile) as store:
            return self._execution_result(store, record, include=[])

    def _list_executions(self, params: dict[str, Any]) -> dict[str, Any]:
        profile = self._profile_from_params(params)
        session_name = params.get("session")
        if session_name is not None and not isinstance(session_name, str):
            raise api_error(
                "INVALID_SESSION",
                "session filter must be a string",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="use_a_session_name",
            )
        try:
            limit = int(params.get("limit", DEFAULT_EXECUTION_LIMIT))
        except (TypeError, ValueError) as error:
            raise api_error(
                "INVALID_LIMIT",
                "limit must be an integer",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="use_a_supported_limit",
            ) from error
        if limit < 1 or limit > MAX_COLLECTION_LIMIT:
            raise api_error(
                "INVALID_LIMIT",
                f"limit must be between 1 and {MAX_COLLECTION_LIMIT}",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="use_a_supported_limit",
            )
        try:
            offset = decode_cursor(params.get("cursor"))
        except ValueError as error:
            raise api_error(
                "INVALID_CURSOR",
                "invalid execution cursor",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="restart_pagination",
            ) from error
        with DurableStore(paths=self.paths, profile=profile) as store:
            records = store.list_executions(session_name=session_name)
            if offset > len(records):
                raise api_error(
                    "INVALID_CURSOR",
                    "execution cursor is beyond the collection",
                    exit_code=ExitCode.USAGE,
                    retryable=False,
                    suggested_action="restart_pagination",
                )
            selected = records[offset : offset + limit]
            next_offset = offset + len(selected)
            result = {
                "executions": [
                    self._execution_result(store, record, include=[])
                    for record in selected
                ]
            }
            if next_offset < len(records):
                result["next_cursor"] = encode_cursor(next_offset)
            return result

    @staticmethod
    def _require_execution(
        store: DurableStore,
        execution_id: str,
    ) -> ExecutionRecord:
        record = store.get_execution(execution_id)
        if record is None:
            raise api_error(
                "EXECUTION_NOT_FOUND",
                f"Execution not found: {execution_id}",
                exit_code=ExitCode.NOT_FOUND,
                retryable=False,
                suggested_action="list_executions",
            )
        return record

    def _execution_result(
        self,
        store: DurableStore,
        record: ExecutionRecord,
        *,
        include: list[str],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "execution_id": record.execution_id,
            "session": record.session_name,
            "state": record.state.value,
            "source_sha256": record.source_sha256,
            "output_complete": record.output_complete,
            "dispatch_confirmed": record.dispatch_confirmed,
            "reply_received": record.reply_received,
            "idle_received": record.idle_received,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }
        optional = {
            "started_at": record.started_at,
            "completed_at": record.completed_at,
            "execution_deadline": record.execution_deadline,
            "idempotency_key": record.idempotency_key,
            "completion_source": record.completion_source,
            "error_name": record.error_name,
            "error_value": record.error_value,
        }
        result.update(
            {key: value for key, value in optional.items() if value is not None}
        )
        if "provenance" in include:
            provenance = {
                "kind": record.source_kind,
                "sha256": record.source_sha256,
            }
            provenance_optional = {
                "path": record.source_path,
                "notebook_id": record.notebook_id,
                "cell_id": record.cell_id,
                "cell_index": record.cell_index,
            }
            provenance.update(
                {
                    key: value
                    for key, value in provenance_optional.items()
                    if value is not None
                }
            )
            result["provenance"] = provenance
        if "transitions" in include:
            result["transitions"] = [
                {
                    "from_state": (
                        transition.from_state.value
                        if transition.from_state is not None
                        else None
                    ),
                    "to_state": transition.to_state.value,
                    "reason": transition.reason,
                    "evidence": transition.evidence,
                    "created_at": transition.created_at,
                }
                for transition in store.list_transitions(record.execution_id)
            ]
        if "traceback" in include and record.traceback_json:
            result["traceback"] = json.loads(record.traceback_json)
        return result

    def _batch_result(self, store: DurableStore, batch) -> dict[str, Any]:
        result: dict[str, Any] = {
            "batch_id": batch.batch_id,
            "session": batch.session_name,
            "state": batch.state,
            "continue_on_error": batch.continue_on_error,
            "executions": [
                self._execution_result(
                    store,
                    self._require_execution(store, execution_id),
                    include=[],
                )
                for execution_id in store.list_batch_members(batch.batch_id)
            ],
            "created_at": batch.created_at,
            "updated_at": batch.updated_at,
        }
        if batch.completed_at is not None:
            result["completed_at"] = batch.completed_at
        return result

    def _output_page(
        self,
        store: DurableStore,
        record: ExecutionRecord,
        *,
        cursor: Any,
        max_bytes: Any,
    ) -> dict[str, Any]:
        try:
            budget = int(max_bytes)
        except (TypeError, ValueError) as error:
            raise api_error(
                "INVALID_MAX_BYTES",
                "max_bytes must be an integer",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="use_a_positive_byte_budget",
            ) from error
        return store.read_output_page(
            record.execution_id,
            cursor=cursor,
            max_bytes=budget,
        ).to_wire()

    @staticmethod
    def _execution_topic(profile: ProfileSpec, execution_id: str) -> str:
        return f"execution:{profile.profile_id}:{execution_id}"

    def _notify_from_worker(
        self,
        profile: ProfileSpec,
        execution_id: str,
    ) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                self._publish_execution(profile, execution_id)
            )
        )

    async def _publish_execution(
        self,
        profile: ProfileSpec,
        execution_id: str,
    ) -> None:
        topic = self._topic(self._execution_topic(profile, execution_id))
        with DurableStore(paths=self.paths, profile=profile) as store:
            record = store.get_execution(execution_id)
            payload = (
                self._execution_result(store, record, include=[])
                if record is not None
                else None
            )
        async with topic.condition:
            topic.revision += 1
            topic.payload = payload
            topic.condition.notify_all()

    @staticmethod
    def _batch_topic(profile: ProfileSpec, batch_id: str) -> str:
        return f"batch:{profile.profile_id}:{batch_id}"

    def _notify_batch_from_worker(
        self,
        profile: ProfileSpec,
        batch_id: str,
    ) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                self._publish_batch(profile, batch_id)
            )
        )

    async def _publish_batch(
        self,
        profile: ProfileSpec,
        batch_id: str,
    ) -> None:
        topic = self._topic(self._batch_topic(profile, batch_id))
        with DurableStore(paths=self.paths, profile=profile) as store:
            batch = store.get_batch(batch_id)
            payload = (
                self._batch_result(store, batch)
                if batch is not None
                else None
            )
        async with topic.condition:
            topic.revision += 1
            topic.payload = payload
            topic.condition.notify_all()

    def _reconcile_and_resume(self) -> None:
        assert self._coordinator is not None
        for profile in self._profile_specs():
            with DurableStore(paths=self.paths, profile=profile) as store:
                store.invalidate_kernel_connections()
                recovery_ids = store.reconcile_after_restart()
                queued = store.list_queued_executions()
                batches = store.list_active_batches()
                batch_members = {
                    execution_id
                    for batch in batches
                    for execution_id in store.list_batch_members(batch.batch_id)
                }
            for batch in batches:
                self._coordinator.submit_batch(profile, batch.batch_id)
            for execution_id in recovery_ids:
                if execution_id not in batch_members:
                    self._coordinator.recover(profile, execution_id)
            for record in queued:
                if record.execution_id not in batch_members:
                    self._coordinator.submit(profile, record.execution_id)

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
        return DurableStore.list_profiles(self.paths)

    def _active_execution_count(self) -> int:
        return DurableStore.active_execution_count(self.paths)

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
