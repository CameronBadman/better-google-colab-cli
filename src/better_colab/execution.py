"""Per-kernel FIFO workers and proof-based durable execution lifecycle."""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from better_colab.errors import ExitCode, api_error
from better_colab.kernel_transport import (
    ExecutionProof,
    KernelEvent,
    KernelTransportAdapter,
    PreparedExecution,
    TransportDisconnected,
)
from better_colab.models import ExecutionState
from better_colab.storage import (
    DurableStore,
    ProfileSpec,
    StatePaths,
    StoredSession,
)


class ExecutionTransport(Protocol):
    kernel_id: str
    jupyter_session_id: str

    def prepare_execution(self, code: str) -> PreparedExecution: ...

    def send(self, prepared: PreparedExecution) -> None: ...

    def next_event(self, *, timeout: float | None) -> KernelEvent: ...

    def interrupt(self) -> None: ...

    def close(self) -> None: ...


TransportFactory = Callable[[StoredSession], ExecutionTransport]
NotifyCallback = Callable[[ProfileSpec, str], None]


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
        completed: Callable[[str], None],
    ):
        self.paths = paths
        self.profile = profile
        self.session_name = session_name
        self.transport_factory = transport_factory
        self.notify = notify
        self.completed = completed
        self.items: queue.Queue[str | None] = queue.Queue()
        self.stop_event = threading.Event()
        self.transport: ExecutionTransport | None = None
        self.thread = threading.Thread(
            target=self._run,
            name=f"better-colab-{profile.profile_id[:8]}-{session_name}",
            daemon=True,
        )
        self.thread.start()

    def submit(self, execution_id: str) -> None:
        self.items.put(execution_id)

    def _run(self) -> None:
        while True:
            execution_id = self.items.get()
            try:
                if execution_id is None:
                    return
                try:
                    self._run_one(execution_id)
                except Exception as error:
                    logging.exception(
                        "Durable execution worker failed for %s",
                        execution_id,
                    )
                    self._internal_failure(execution_id, error)
            finally:
                self.items.task_done()
                if execution_id is not None:
                    self.completed(execution_id)

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
            self.transport = self.transport_factory(session)
            store.update_session_connection(
                session.name,
                kernel_id=self.transport.kernel_id,
                jupyter_session_id=self.transport.jupyter_session_id,
            )
        return self.transport

    def _drop_transport(self) -> None:
        if self.transport is not None:
            with contextlib.suppress(Exception):
                self.transport.close()
            self.transport = None

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
                    return
                except Exception:
                    self._connection_lost(store, execution_id, proof)
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
        self._drop_transport()
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
        self._drop_transport()
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
        self._drop_transport()
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
    ):
        self.paths = paths or StatePaths.discover()
        self.transport_factory = transport_factory
        self.notify = notify or (lambda _profile, _execution_id: None)
        self._workers: dict[tuple[str, str], _KernelWorker] = {}
        self._condition = threading.Condition()
        self._pending = 0
        self._scheduled: set[str] = set()
        self._closed = False

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
        key = (profile.profile_id, session_name)
        with self._condition:
            if self._closed:
                raise RuntimeError("execution coordinator is closed")
            if execution_id in self._scheduled:
                return
            worker = self._workers.get(key)
            if worker is None:
                worker = _KernelWorker(
                    paths=self.paths,
                    profile=profile,
                    session_name=session_name,
                    transport_factory=self.transport_factory,
                    notify=self.notify,
                    completed=self._completed,
                )
                self._workers[key] = worker
            self._pending += 1
            self._scheduled.add(execution_id)
            worker.submit(execution_id)

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
