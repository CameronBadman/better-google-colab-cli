"""Per-kernel FIFO workers and proof-based durable execution lifecycle."""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from better_colab.errors import ExitCode, api_error
from better_colab.kernel_transport import (
    ExecutionProof,
    KernelEvent,
    KernelIdleProof,
    KernelTransportAdapter,
    PreparedExecution,
    ReadinessProof,
    TransportDisconnected,
)
from better_colab.models import CompletionSource, ExecutionState, SessionHealthResult
from better_colab.storage import (
    DurableStore,
    ProfileSpec,
    StatePaths,
    StoredSession,
    parse_timestamp,
)


class ExecutionTransport(Protocol):
    kernel_id: str
    jupyter_session_id: str

    def prepare_execution(self, code: str) -> PreparedExecution: ...

    def prepare_readiness_probe(self, nonce: str) -> PreparedExecution: ...

    def prepare_kernel_info(self) -> PreparedExecution: ...

    def send(self, prepared: PreparedExecution) -> None: ...

    def next_event(self, *, timeout: float | None) -> KernelEvent: ...

    def interrupt(self) -> None: ...

    def close(self) -> None: ...


TransportFactory = Callable[[StoredSession], ExecutionTransport]
NotifyCallback = Callable[[ProfileSpec, str], None]


@dataclass
class _ProbeRequest:
    timeout: float
    deadline: float
    completed: threading.Event
    result: SessionHealthResult | None = None
    error: BaseException | None = None


@dataclass(frozen=True)
class _WorkItem:
    kind: str
    execution_id: str | None = None
    batch_id: str | None = None
    probe: _ProbeRequest | None = None


def _default_transport_factory(session: StoredSession) -> ExecutionTransport:
    return KernelTransportAdapter.connect(session)


def _normalize_output(event: KernelEvent) -> dict[str, Any] | None:
    if event.channel != "iopub":
        return None
    message = event.message
    header = message.get("header")
    message_type = (
        header.get("msg_type")
        if isinstance(header, dict)
        else message.get("msg_type")
    )
    content = message.get("content")
    if not isinstance(message_type, str) or not isinstance(content, dict):
        return None
    if message_type == "stream":
        return {
            "event_type": "stream",
            "stream": str(content.get("name") or "stdout"),
            "text": str(content.get("text") or ""),
        }
    if message_type in {"display_data", "execute_result"}:
        data = content.get("data")
        return {
            "event_type": message_type,
            "data": data if isinstance(data, dict) else {},
            "metadata": (
                content.get("metadata")
                if isinstance(content.get("metadata"), dict)
                else {}
            ),
            "execution_count": content.get("execution_count"),
            "display_id": (
                content.get("transient", {}).get("display_id")
                if isinstance(content.get("transient"), dict)
                else None
            ),
        }
    if message_type == "error":
        traceback = content.get("traceback")
        return {
            "event_type": "error",
            "error_name": str(content.get("ename") or "Error"),
            "error_value": str(content.get("evalue") or ""),
            "traceback": (
                traceback
                if isinstance(traceback, list)
                and all(isinstance(line, str) for line in traceback)
                else []
            ),
        }
    if message_type == "clear_output":
        return {
            "event_type": "clear_output",
            "wait": bool(content.get("wait", False)),
        }
    if message_type == "update_display_data":
        return {
            "event_type": "update_display_data",
            "data": (
                content.get("data")
                if isinstance(content.get("data"), dict)
                else {}
            ),
            "metadata": (
                content.get("metadata")
                if isinstance(content.get("metadata"), dict)
                else {}
            ),
            "display_id": (
                content.get("transient", {}).get("display_id")
                if isinstance(content.get("transient"), dict)
                else None
            ),
        }
    return None


class _KernelWorker:
    def __init__(
        self,
        *,
        paths: StatePaths,
        profile: ProfileSpec,
        session_name: str,
        transport_factory: TransportFactory,
        notify: NotifyCallback,
        notify_batch: NotifyCallback,
        completed: Callable[[str], None],
    ):
        self.paths = paths
        self.profile = profile
        self.session_name = session_name
        self.transport_factory = transport_factory
        self.notify = notify
        self.notify_batch = notify_batch
        self.completed = completed
        self.items: queue.Queue[_WorkItem | None] = queue.Queue()
        self.stop_event = threading.Event()
        self.transport: ExecutionTransport | None = None
        self.connection_id: str | None = None
        self.thread = threading.Thread(
            target=self._run,
            name=f"better-colab-{profile.profile_id[:8]}-{session_name}",
            daemon=True,
        )
        self.thread.start()

    def submit(self, execution_id: str) -> None:
        self.items.put(_WorkItem(kind="execute", execution_id=execution_id))

    def recover(self, execution_id: str) -> None:
        self.items.put(_WorkItem(kind="recover", execution_id=execution_id))

    def submit_batch(self, batch_id: str) -> None:
        self.items.put(_WorkItem(kind="batch", batch_id=batch_id))

    def probe(self, *, timeout: float) -> SessionHealthResult:
        request = _ProbeRequest(
            timeout=timeout,
            deadline=time.monotonic() + timeout,
            completed=threading.Event(),
        )
        self.items.put(_WorkItem(kind="probe", probe=request))
        if not request.completed.wait(timeout=max(0.1, timeout + 0.5)):
            raise api_error(
                "KERNEL_PROBE_TIMEOUT",
                "Kernel readiness probe did not complete before its deadline",
                exit_code=ExitCode.UNAVAILABLE,
                retryable=True,
                suggested_action="retry_session_probe",
            )
        if request.error is not None:
            raise request.error
        assert request.result is not None
        return request.result

    def _run(self) -> None:
        while True:
            item = self.items.get()
            execution_id = item.execution_id if item is not None else None
            batch_id = item.batch_id if item is not None else None
            work_key = (
                execution_id
                if execution_id is not None
                else (f"batch:{batch_id}" if batch_id is not None else None)
            )
            try:
                if item is None:
                    return
                try:
                    if item.kind == "execute":
                        assert execution_id is not None
                        self._run_one(execution_id)
                    elif item.kind == "recover":
                        assert execution_id is not None
                        self._recover_one(execution_id)
                    elif item.kind == "batch":
                        assert batch_id is not None
                        self._run_batch(batch_id)
                    elif item.kind == "probe":
                        assert item.probe is not None
                        self._run_probe(item.probe)
                    else:
                        raise RuntimeError(f"unknown kernel work kind: {item.kind}")
                except Exception as error:
                    if execution_id is not None:
                        logging.exception(
                            "Durable execution worker failed for %s",
                            execution_id,
                        )
                        self._internal_failure(execution_id, error)
                    elif batch_id is not None:
                        logging.exception(
                            "Durable execution batch worker failed for %s",
                            batch_id,
                        )
                        self._batch_internal_failure(batch_id)
                    elif item.probe is not None:
                        item.probe.error = error
                        item.probe.completed.set()
            finally:
                self.items.task_done()
                if work_key is not None:
                    self.completed(work_key)

    def _batch_internal_failure(self, batch_id: str) -> None:
        with DurableStore(paths=self.paths, profile=self.profile) as store:
            batch = store.get_batch(batch_id)
            if batch is None or batch.state in {
                "finished",
                "error",
                "interrupted",
            }:
                return
            for execution_id in store.list_batch_members(batch_id):
                record = store.get_execution(execution_id)
                if record is not None and record.state is ExecutionState.QUEUED:
                    store.stop_queued_execution(
                        execution_id,
                        reason="batch_worker_failed",
                    )
                    self.notify(self.profile, execution_id)
            store.set_batch_state(batch_id, "error")
        self.notify_batch(self.profile, batch_id)

    def _internal_failure(
        self,
        execution_id: str,
        error: Exception,
    ) -> None:
        with DurableStore(paths=self.paths, profile=self.profile) as store:
            current = store.get_execution(execution_id)
            if current is None or current.state in {
                ExecutionState.FINISHED,
                ExecutionState.ERROR,
                ExecutionState.INTERRUPTED,
                ExecutionState.TIMED_OUT,
                ExecutionState.UNKNOWN,
            }:
                return
            evidence = {"error_type": type(error).__name__}
            if current.state is ExecutionState.QUEUED:
                store.finalize_output(execution_id)
                store.transition_execution(
                    execution_id,
                    ExecutionState.INTERRUPTED,
                    reason="worker_failed_before_dispatch",
                    evidence=evidence,
                    completion_source="live",
                )
            elif current.state is ExecutionState.DISPATCHING:
                store.finalize_output(execution_id)
                store.transition_execution(
                    execution_id,
                    ExecutionState.UNKNOWN,
                    reason="worker_failed_after_dispatch_commit",
                    evidence=evidence,
                    completion_source="live",
                )
            else:
                store.mark_output_incomplete(execution_id)
                if current.state is ExecutionState.RUNNING:
                    store.transition_execution(
                        execution_id,
                        ExecutionState.DISCONNECTED,
                        reason="worker_observation_failed",
                        evidence=evidence,
                    )
                    current = store.get_execution(execution_id)
                if current.state is ExecutionState.DISCONNECTED:
                    store.finalize_output(execution_id)
                    store.transition_execution(
                        execution_id,
                        ExecutionState.UNKNOWN,
                        reason="worker_observation_failed",
                        evidence=evidence,
                        completion_source="live",
                    )
        self._drop_transport()
        self.notify(self.profile, execution_id)

    def _ensure_transport(
        self,
        store: DurableStore,
        session: StoredSession,
    ) -> ExecutionTransport:
        if self.transport is None:
            transport = self.transport_factory(session)
            connection_id = str(uuid.uuid4())
            self.transport = transport
            self.connection_id = connection_id
            store.update_session_connection(
                session.name,
                kernel_id=transport.kernel_id,
                jupyter_session_id=transport.jupyter_session_id,
            )
            store.record_kernel_connection(
                session.name,
                kernel_id=transport.kernel_id,
                jupyter_session_id=transport.jupyter_session_id,
                connection_id=connection_id,
            )
        return self.transport

    def _drop_transport(self, store: DurableStore | None = None) -> None:
        transport = self.transport
        connection_id = self.connection_id
        self.transport = None
        self.connection_id = None
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()
        if connection_id is not None:
            if store is not None:
                store.disconnect_kernel_connection(
                    self.session_name,
                    connection_id=connection_id,
                )
            else:
                with contextlib.suppress(Exception):
                    with DurableStore(
                        paths=self.paths,
                        profile=self.profile,
                    ) as opened:
                        opened.disconnect_kernel_connection(
                            self.session_name,
                            connection_id=connection_id,
                        )

    @staticmethod
    def _health_timestamp() -> str:
        return (
            datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def _health_result(
        self,
        store: DurableStore,
        session: StoredSession,
        *,
        probe_error: str | None = None,
        probe_at: str | None = None,
        latency_ms: float | None = None,
    ) -> SessionHealthResult:
        connection = store.get_kernel_connection(session.name)
        connected = bool(
            connection is not None
            and connection.disconnected_at is None
            and connection.connection_id == self.connection_id
            and self.transport is not None
            and connection.kernel_id == self.transport.kernel_id
            and connection.jupyter_session_id
            == self.transport.jupyter_session_id
        )
        cached_ready = bool(
            connected
            and connection.readiness_checked_at is not None
            and connection.readiness_error is None
        )
        return SessionHealthResult(
            name=session.name,
            endpoint=session.endpoint,
            hardware=(
                "CPU" if session.hardware == "NONE" else session.hardware
            ),
            variant=session.variant,
            controller_alive=True,
            backend_alive=connected,
            kernel_connected=connected,
            kernel_execution_ready=cached_ready,
            kernel_probe_at=(
                probe_at
                if probe_at is not None
                else (
                    connection.readiness_checked_at
                    if connected and connection is not None
                    else None
                )
            ),
            kernel_probe_latency_ms=(
                latency_ms
                if latency_ms is not None
                else (
                    connection.readiness_latency_ms
                    if connected and connection is not None
                    else None
                )
            ),
            kernel_probe_error=(
                probe_error
                if probe_error is not None
                else (
                    connection.readiness_error
                    if connected and connection is not None
                    else None
                )
            ),
        )

    def status(self) -> SessionHealthResult:
        with DurableStore(paths=self.paths, profile=self.profile) as store:
            session = store.get_session(self.session_name)
            if session is None:
                raise api_error(
                    "SESSION_NOT_FOUND",
                    f"Session not found: {self.session_name}",
                    exit_code=ExitCode.NOT_FOUND,
                    retryable=False,
                    suggested_action="ensure_session",
                )
            return self._health_result(store, session)

    def _run_probe(self, request: _ProbeRequest) -> None:
        started = time.monotonic()
        with DurableStore(paths=self.paths, profile=self.profile) as store:
            session = store.get_session(self.session_name)
            if session is None:
                request.error = api_error(
                    "SESSION_NOT_FOUND",
                    f"Session not found: {self.session_name}",
                    exit_code=ExitCode.NOT_FOUND,
                    retryable=False,
                    suggested_action="ensure_session",
                )
                request.completed.set()
                return
            if time.monotonic() >= request.deadline:
                request.result = self._health_result(
                    store,
                    session,
                    probe_error="PROBE_QUEUE_TIMEOUT",
                    probe_at=self._health_timestamp(),
                    latency_ms=0.0,
                )
                request.completed.set()
                return
            try:
                transport = self._ensure_transport(store, session)
            except Exception as error:
                request.result = self._health_result(
                    store,
                    session,
                    probe_error=f"CONNECT_FAILED:{type(error).__name__}",
                    probe_at=self._health_timestamp(),
                    latency_ms=(time.monotonic() - started) * 1000,
                )
                request.completed.set()
                return

            nonce = str(uuid.uuid4())
            proof: ReadinessProof | None = None
            error_code: str | None = None
            try:
                prepared = transport.prepare_readiness_probe(nonce)
                proof = ReadinessProof(prepared.message_id, nonce)
                transport.send(prepared)
                while (
                    not self.stop_event.is_set()
                    and time.monotonic() < request.deadline
                ):
                    remaining = request.deadline - time.monotonic()
                    try:
                        event = transport.next_event(
                            timeout=min(0.05, max(0.0, remaining))
                        )
                    except TimeoutError:
                        continue
                    proof.observe(event)
                    if proof.ready or proof.error is not None:
                        break
                if proof.ready:
                    error_code = None
                elif proof.error is not None:
                    error_code = proof.error
                else:
                    error_code = "PROBE_TIMEOUT"
            except TransportDisconnected:
                error_code = "TRANSPORT_DISCONNECTED"
                self._drop_transport(store)
            except Exception as error:
                error_code = f"PROBE_FAILED:{type(error).__name__}"

            latency_ms = (time.monotonic() - started) * 1000
            checked_at = self._health_timestamp()
            if self.connection_id is not None:
                connection = store.record_kernel_readiness(
                    session.name,
                    connection_id=self.connection_id,
                    nonce=nonce,
                    latency_ms=latency_ms,
                    error=error_code,
                )
                checked_at = connection.readiness_checked_at
            request.result = self._health_result(
                store,
                session,
                probe_error=error_code,
                probe_at=checked_at,
                latency_ms=latency_ms,
            )
            request.completed.set()

    def _run_one(self, execution_id: str) -> None:
        with DurableStore(paths=self.paths, profile=self.profile) as store:
            record = store.get_execution(execution_id)
            if record is None or record.state is not ExecutionState.QUEUED:
                return
            session = store.get_session(record.session_name)
            if session is None:
                # This can happen only if a session is removed after queueing.
                store.finalize_output(execution_id)
                store.transition_execution(
                    execution_id,
                    ExecutionState.INTERRUPTED,
                    reason="session_removed_before_dispatch",
                    completion_source="live",
                )
                self.notify(self.profile, execution_id)
                return
            source = store.read_execution_source(execution_id)
            try:
                code = source.decode("utf-8")
            except UnicodeDecodeError:
                store.finalize_output(execution_id)
                store.transition_execution(
                    execution_id,
                    ExecutionState.INTERRUPTED,
                    reason="source_not_utf8",
                    completion_source="live",
                )
                self.notify(self.profile, execution_id)
                return

            try:
                transport = self._ensure_transport(store, session)
                prepared = transport.prepare_execution(code)
                store.begin_dispatch(
                    execution_id,
                    kernel_message_id=prepared.message_id,
                    session_endpoint=session.endpoint,
                    kernel_id=transport.kernel_id,
                    jupyter_session_id=transport.jupyter_session_id,
                )
                self.notify(self.profile, execution_id)
                transport.send(prepared)
            except Exception as error:
                self._dispatch_failed(store, execution_id, error)
                return

            proof = ExecutionProof(prepared.message_id)
            interrupt_sent = False
            deadline_monotonic: float | None = None
            while not self.stop_event.is_set():
                current = store.get_execution(execution_id)
                if current is None or current.state in {
                    ExecutionState.FINISHED,
                    ExecutionState.ERROR,
                    ExecutionState.INTERRUPTED,
                    ExecutionState.TIMED_OUT,
                    ExecutionState.UNKNOWN,
                }:
                    return

                requested = current.interrupt_requested_state
                if requested is not None and not interrupt_sent:
                    proof.request_interrupt(requested)
                    interrupt_sent = True
                    try:
                        transport.interrupt()
                    except Exception:
                        self._ambiguous_interrupt(store, execution_id)
                        return

                if (
                    deadline_monotonic is not None
                    and not interrupt_sent
                    and time.monotonic() >= deadline_monotonic
                ):
                    current = store.request_execution_timeout(execution_id)
                    proof.request_interrupt(ExecutionState.TIMED_OUT.value)
                    interrupt_sent = True
                    try:
                        transport.interrupt()
                    except Exception:
                        self._ambiguous_interrupt(store, execution_id)
                        return
                    self.notify(self.profile, execution_id)

                try:
                    event = transport.next_event(timeout=0.05)
                except TimeoutError:
                    continue
                except TransportDisconnected:
                    self._connection_lost(store, execution_id, proof)
                    if (
                        store.get_execution(execution_id).state
                        is ExecutionState.DISCONNECTED
                    ):
                        self._recover_one(execution_id)
                    return
                except Exception:
                    self._connection_lost(store, execution_id, proof)
                    if (
                        store.get_execution(execution_id).state
                        is ExecutionState.DISCONNECTED
                    ):
                        self._recover_one(execution_id)
                    return

                observation = proof.observe(event)
                if not observation.matched:
                    continue
                if observation.confirm_dispatch:
                    running = store.confirm_dispatch(
                        execution_id,
                        evidence={
                            "channel": event.channel,
                            "message_type": self._message_type(event),
                        },
                    )
                    if running.execution_timeout_seconds is not None:
                        deadline_monotonic = (
                            time.monotonic()
                            + running.execution_timeout_seconds
                        )
                    self.notify(self.profile, execution_id)

                output = _normalize_output(event)
                if output is not None:
                    store.append_output_event(execution_id, output)

                if (
                    observation.reply_received
                    or observation.idle_received
                    or observation.error_name is not None
                    or observation.traceback is not None
                ):
                    store.record_execution_evidence(
                        execution_id,
                        reply_received=observation.reply_received,
                        idle_received=observation.idle_received,
                        reply_status=(
                            observation.reply_status
                            if observation.reply_received
                            else None
                        ),
                        error_name=observation.error_name,
                        error_value=observation.error_value,
                        traceback=observation.traceback,
                    )
                    self.notify(self.profile, execution_id)

                if observation.terminal_state is not None:
                    store.finalize_output(execution_id)
                    store.transition_execution(
                        execution_id,
                        ExecutionState(observation.terminal_state),
                        reason="matching_execute_reply_and_idle",
                        evidence={
                            "kernel_message_id": prepared.message_id,
                            "reply_status": observation.reply_status,
                        },
                        completion_source="live",
                    )
                    self.notify(self.profile, execution_id)
                    return

    @staticmethod
    def _restored_proof(record) -> ExecutionProof:
        proof = ExecutionProof(record.kernel_message_id)
        traceback: list[str] | None = None
        if record.traceback_json is not None:
            with contextlib.suppress(json.JSONDecodeError):
                candidate = json.loads(record.traceback_json)
                if isinstance(candidate, list) and all(
                    isinstance(line, str) for line in candidate
                ):
                    traceback = candidate
        proof.restore(
            dispatch_confirmed=record.dispatch_confirmed,
            reply_received=record.reply_received,
            idle_received=record.idle_received,
            reply_status=record.reply_status,
            error_name=record.error_name,
            error_value=record.error_value,
            traceback=traceback,
            requested_terminal=record.interrupt_requested_state,
        )
        return proof

    def _recovery_unknown(
        self,
        store: DurableStore,
        execution_id: str,
        *,
        reason: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        current = store.get_execution(execution_id)
        if current is None or current.state is not ExecutionState.DISCONNECTED:
            return
        store.mark_output_incomplete(execution_id)
        store.finalize_output(execution_id)
        store.transition_execution(
            execution_id,
            ExecutionState.UNKNOWN,
            reason=reason,
            evidence=evidence,
            completion_source=CompletionSource.RECOVERY,
        )
        self.notify(self.profile, execution_id)

    def _recover_one(self, execution_id: str) -> None:
        """Reconnect to the same kernel without ever replaying the request."""
        with DurableStore(paths=self.paths, profile=self.profile) as store:
            while not self.stop_event.is_set():
                record = store.get_execution(execution_id)
                if record is None or record.state is not ExecutionState.DISCONNECTED:
                    return
                session = store.get_session(record.session_name)
                if session is None:
                    self._recovery_unknown(
                        store,
                        execution_id,
                        reason="session_missing_during_recovery",
                    )
                    return
                if (
                    record.session_endpoint is None
                    or session.endpoint != record.session_endpoint
                ):
                    self._recovery_unknown(
                        store,
                        execution_id,
                        reason="session_identity_changed_during_recovery",
                    )
                    return

                try:
                    transport = self._ensure_transport(store, session)
                except Exception:
                    if self.stop_event.wait(0.1):
                        return
                    continue
                if (
                    record.kernel_id_snapshot is None
                    or record.jupyter_session_id_snapshot is None
                    or transport.kernel_id != record.kernel_id_snapshot
                    or transport.jupyter_session_id
                    != record.jupyter_session_id_snapshot
                ):
                    self._recovery_unknown(
                        store,
                        execution_id,
                        reason="kernel_identity_changed_during_recovery",
                        evidence={
                            "expected_kernel_id": record.kernel_id_snapshot,
                            "observed_kernel_id": transport.kernel_id,
                            "expected_jupyter_session_id": (
                                record.jupyter_session_id_snapshot
                            ),
                            "observed_jupyter_session_id": (
                                transport.jupyter_session_id
                            ),
                        },
                    )
                    self._drop_transport(store)
                    return

                try:
                    prepared = transport.prepare_kernel_info()
                    idle_proof = KernelIdleProof(prepared.message_id)
                    proof = self._restored_proof(record)
                    store.increment_reconnect_count(execution_id)
                    transport.send(prepared)
                except Exception:
                    self._drop_transport(store)
                    if self.stop_event.wait(0.1):
                        return
                    continue

                interrupt_sent = False
                while not self.stop_event.is_set():
                    current = store.get_execution(execution_id)
                    if (
                        current is None
                        or current.state is not ExecutionState.DISCONNECTED
                    ):
                        return

                    requested = current.interrupt_requested_state
                    if (
                        requested is None
                        and current.execution_deadline is not None
                        and datetime.now(timezone.utc)
                        >= parse_timestamp(current.execution_deadline)
                    ):
                        current = store.request_execution_timeout(execution_id)
                        requested = current.interrupt_requested_state
                        self.notify(self.profile, execution_id)
                    if requested is not None and not interrupt_sent:
                        proof.request_interrupt(requested)
                        interrupt_sent = True
                        try:
                            transport.interrupt()
                        except Exception:
                            self._ambiguous_interrupt(store, execution_id)
                            return

                    try:
                        event = transport.next_event(timeout=0.05)
                    except TimeoutError:
                        continue
                    except Exception:
                        self._drop_transport(store)
                        self.notify(self.profile, execution_id)
                        break

                    observation = proof.observe(event)
                    if observation.matched:
                        output = _normalize_output(event)
                        if output is not None:
                            store.append_output_event(execution_id, output)
                        if (
                            observation.reply_received
                            or observation.idle_received
                            or observation.error_name is not None
                            or observation.traceback is not None
                        ):
                            store.record_execution_evidence(
                                execution_id,
                                reply_received=observation.reply_received,
                                idle_received=observation.idle_received,
                                reply_status=(
                                    observation.reply_status
                                    if observation.reply_received
                                    else None
                                ),
                                error_name=observation.error_name,
                                error_value=observation.error_value,
                                traceback=observation.traceback,
                            )
                            self.notify(self.profile, execution_id)
                        if observation.terminal_state is not None:
                            store.finalize_output(execution_id)
                            store.transition_execution(
                                execution_id,
                                ExecutionState(observation.terminal_state),
                                reason=(
                                    "matching_execute_reply_and_idle_after_reconnect"
                                ),
                                evidence={
                                    "kernel_message_id": record.kernel_message_id,
                                    "reply_status": observation.reply_status,
                                },
                                completion_source=CompletionSource.RECOVERY,
                            )
                            self.notify(self.profile, execution_id)
                            return

                    idle_proof.observe(event)
                    if idle_proof.idle:
                        self._recovery_unknown(
                            store,
                            execution_id,
                            reason="kernel_idle_without_terminal_proof",
                            evidence={
                                "kernel_info_message_id": prepared.message_id,
                                "reply_received": proof.reply_received,
                                "idle_received": proof.idle_received,
                            },
                        )
                        return

    def _run_batch(self, batch_id: str) -> None:
        with DurableStore(paths=self.paths, profile=self.profile) as store:
            batch = store.get_batch(batch_id)
            if batch is None or batch.state in {
                "finished",
                "error",
                "interrupted",
            }:
                return
            if batch.state == "queued":
                batch = store.set_batch_state(batch_id, "running")
                self.notify_batch(self.profile, batch_id)
            members = store.list_batch_members(batch_id)
            saw_failure = False
            for position, execution_id in enumerate(members):
                batch = store.get_batch(batch_id)
                if batch is None:
                    return
                record = store.get_execution(execution_id)
                if record is None:
                    saw_failure = True
                    continue
                if record.state is ExecutionState.DISCONNECTED:
                    self._recover_one(execution_id)
                elif record.state is ExecutionState.QUEUED:
                    self._run_one(execution_id)
                record = store.get_execution(execution_id)
                if record is None:
                    saw_failure = True
                    continue
                failed = record.state in {
                    ExecutionState.ERROR,
                    ExecutionState.INTERRUPTED,
                    ExecutionState.TIMED_OUT,
                    ExecutionState.UNKNOWN,
                }
                saw_failure = saw_failure or failed
                batch = store.get_batch(batch_id)
                cancelling = batch is not None and batch.state == "cancelling"
                if failed and not batch.continue_on_error and not cancelling:
                    for remaining_id in members[position + 1 :]:
                        remaining = store.stop_queued_execution(
                            remaining_id,
                            reason="batch_stopped",
                        )
                        if remaining.state is ExecutionState.INTERRUPTED:
                            self.notify(self.profile, remaining_id)
                    store.set_batch_state(batch_id, "error")
                    self.notify_batch(self.profile, batch_id)
                    return
                if cancelling:
                    # Cancellation marks all queued members terminal
                    # immediately and running work observes interrupt intent.
                    continue

            batch = store.get_batch(batch_id)
            if batch is None:
                return
            if batch.state == "cancelling":
                final_state = "interrupted"
            elif saw_failure:
                final_state = "error"
            else:
                final_state = "finished"
            store.set_batch_state(batch_id, final_state)
        self.notify_batch(self.profile, batch_id)

    @staticmethod
    def _message_type(event: KernelEvent) -> str | None:
        header = event.message.get("header")
        if isinstance(header, dict):
            value = header.get("msg_type")
            return value if isinstance(value, str) else None
        return None

    def _dispatch_failed(
        self,
        store: DurableStore,
        execution_id: str,
        error: Exception,
    ) -> None:
        current = store.get_execution(execution_id)
        if current is None:
            return
        if current.state is ExecutionState.QUEUED:
            store.finalize_output(execution_id)
            store.transition_execution(
                execution_id,
                ExecutionState.INTERRUPTED,
                reason="kernel_connection_failed",
                evidence={"error_type": type(error).__name__},
                completion_source="live",
            )
        elif current.state is ExecutionState.DISPATCHING:
            store.finalize_output(execution_id)
            store.transition_execution(
                execution_id,
                ExecutionState.UNKNOWN,
                reason="send_failed_after_dispatch_commit",
                evidence={"error_type": type(error).__name__},
                completion_source="live",
            )
        self._drop_transport(store)
        self.notify(self.profile, execution_id)

    def _connection_lost(
        self,
        store: DurableStore,
        execution_id: str,
        proof: ExecutionProof,
    ) -> None:
        current = store.get_execution(execution_id)
        if current is None:
            return
        if not proof.dispatch_confirmed:
            if current.state is ExecutionState.DISPATCHING:
                store.finalize_output(execution_id)
                store.transition_execution(
                    execution_id,
                    ExecutionState.UNKNOWN,
                    reason="disconnect_before_confirmation",
                    completion_source="live",
                )
        else:
            store.mark_output_incomplete(execution_id)
            current = store.get_execution(execution_id)
            if current.state is ExecutionState.RUNNING:
                store.transition_execution(
                    execution_id,
                    ExecutionState.DISCONNECTED,
                    reason="transport_lost_after_confirmation",
                )
        self._drop_transport(store)
        self.notify(self.profile, execution_id)

    def _ambiguous_interrupt(
        self,
        store: DurableStore,
        execution_id: str,
    ) -> None:
        store.mark_output_incomplete(execution_id)
        current = store.get_execution(execution_id)
        if current.state is ExecutionState.RUNNING:
            store.transition_execution(
                execution_id,
                ExecutionState.DISCONNECTED,
                reason="interrupt_delivery_ambiguous",
            )
            current = store.get_execution(execution_id)
        if current.state is ExecutionState.DISCONNECTED:
            store.finalize_output(execution_id)
            store.transition_execution(
                execution_id,
                ExecutionState.UNKNOWN,
                reason="interrupt_delivery_ambiguous",
                completion_source="live",
            )
        self._drop_transport(store)
        self.notify(self.profile, execution_id)

    def close(self) -> None:
        self.stop_event.set()
        self.items.put(None)
        self.thread.join(timeout=2)
        self._drop_transport()


class ExecutionCoordinator:
    """Own one persistent FIFO worker for each profile/session kernel."""

    def __init__(
        self,
        *,
        paths: StatePaths | None = None,
        transport_factory: TransportFactory = _default_transport_factory,
        notify: NotifyCallback | None = None,
        notify_batch: NotifyCallback | None = None,
    ):
        self.paths = paths or StatePaths.discover()
        self.transport_factory = transport_factory
        self.notify = notify or (lambda _profile, _execution_id: None)
        self.notify_batch = notify_batch or (
            lambda _profile, _batch_id: None
        )
        self._workers: dict[tuple[str, str], _KernelWorker] = {}
        self._condition = threading.Condition()
        self._pending = 0
        self._scheduled: set[str] = set()
        self._closed = False

    def _worker(
        self,
        profile: ProfileSpec,
        session_name: str,
        *,
        create: bool,
    ) -> _KernelWorker | None:
        key = (profile.profile_id, session_name)
        with self._condition:
            if self._closed:
                raise RuntimeError("execution coordinator is closed")
            worker = self._workers.get(key)
            if worker is None and create:
                worker = _KernelWorker(
                    paths=self.paths,
                    profile=profile,
                    session_name=session_name,
                    transport_factory=self.transport_factory,
                    notify=self.notify,
                    notify_batch=self.notify_batch,
                    completed=self._completed,
                )
                self._workers[key] = worker
            return worker

    def submit(self, profile: ProfileSpec, execution_id: str) -> None:
        with DurableStore(paths=self.paths, profile=profile) as store:
            record = store.get_execution(execution_id)
            if record is None:
                raise api_error(
                    "EXECUTION_NOT_FOUND",
                    f"Execution not found: {execution_id}",
                    exit_code=ExitCode.NOT_FOUND,
                    retryable=False,
                    suggested_action="list_executions",
                )
            if record.state is not ExecutionState.QUEUED:
                return
            session_name = record.session_name
        worker = self._worker(profile, session_name, create=True)
        assert worker is not None
        with self._condition:
            if execution_id in self._scheduled:
                return
            self._pending += 1
            self._scheduled.add(execution_id)
            worker.submit(execution_id)

    def recover(self, profile: ProfileSpec, execution_id: str) -> None:
        with DurableStore(paths=self.paths, profile=profile) as store:
            record = store.get_execution(execution_id)
            if record is None:
                raise api_error(
                    "EXECUTION_NOT_FOUND",
                    f"Execution not found: {execution_id}",
                    exit_code=ExitCode.NOT_FOUND,
                    retryable=False,
                    suggested_action="list_executions",
                )
            if record.state is not ExecutionState.DISCONNECTED:
                return
            session_name = record.session_name
        worker = self._worker(profile, session_name, create=True)
        assert worker is not None
        with self._condition:
            if execution_id in self._scheduled:
                return
            self._pending += 1
            self._scheduled.add(execution_id)
            worker.recover(execution_id)

    def submit_batch(self, profile: ProfileSpec, batch_id: str) -> None:
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
                return
            session_name = batch.session_name
        worker = self._worker(profile, session_name, create=True)
        assert worker is not None
        key = f"batch:{batch_id}"
        with self._condition:
            if key in self._scheduled:
                return
            self._pending += 1
            self._scheduled.add(key)
            worker.submit_batch(batch_id)

    def cancel_batch(self, profile: ProfileSpec, batch_id: str):
        with DurableStore(paths=self.paths, profile=profile) as store:
            batch = store.request_batch_cancel(batch_id)
            for execution_id in store.list_batch_members(batch_id):
                self.notify(profile, execution_id)
        self.notify_batch(profile, batch_id)
        return batch

    def session_status(
        self,
        profile: ProfileSpec,
        session_name: str,
    ) -> SessionHealthResult:
        with DurableStore(paths=self.paths, profile=profile) as store:
            session = store.get_session(session_name)
            if session is None:
                raise api_error(
                    "SESSION_NOT_FOUND",
                    f"Session not found: {session_name}",
                    exit_code=ExitCode.NOT_FOUND,
                    retryable=False,
                    suggested_action="ensure_session",
                )
            connection = store.get_kernel_connection(session_name)
        worker = self._worker(profile, session_name, create=False)
        if worker is not None:
            return worker.status()
        connected = bool(
            connection is not None and connection.disconnected_at is None
        )
        return SessionHealthResult(
            name=session.name,
            endpoint=session.endpoint,
            hardware=(
                "CPU" if session.hardware == "NONE" else session.hardware
            ),
            variant=session.variant,
            controller_alive=True,
            backend_alive=False,
            kernel_connected=False,
            kernel_execution_ready=False,
            kernel_probe_at=None,
            kernel_probe_latency_ms=None,
            kernel_probe_error=(
                "CONNECTION_NOT_OWNED"
                if connected
                else None
            ),
        )

    def probe_session(
        self,
        profile: ProfileSpec,
        session_name: str,
        *,
        timeout: float,
    ) -> SessionHealthResult:
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise api_error(
                "INVALID_PROBE_TIMEOUT",
                "Probe timeout must be a positive number",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="use_a_positive_timeout",
            )
        with DurableStore(paths=self.paths, profile=profile) as store:
            if store.get_session(session_name) is None:
                raise api_error(
                    "SESSION_NOT_FOUND",
                    f"Session not found: {session_name}",
                    exit_code=ExitCode.NOT_FOUND,
                    retryable=False,
                    suggested_action="ensure_session",
                )
        worker = self._worker(profile, session_name, create=True)
        assert worker is not None
        return worker.probe(timeout=float(timeout))

    def cancel(self, profile: ProfileSpec, execution_id: str):
        with DurableStore(paths=self.paths, profile=profile) as store:
            record = store.request_execution_cancel(execution_id)
        self.notify(profile, execution_id)
        return record

    def _completed(self, execution_id: str) -> None:
        with self._condition:
            self._pending -= 1
            self._scheduled.discard(execution_id)
            self._condition.notify_all()

    def wait_idle(self, *, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._pending:
                remaining = (
                    None
                    if deadline is None
                    else max(0.0, deadline - time.monotonic())
                )
                if remaining == 0:
                    raise TimeoutError("execution coordinator did not become idle")
                self._condition.wait(timeout=remaining)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.close()
