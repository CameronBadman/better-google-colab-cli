"""Pinned low-level transport boundary for durable Jupyter execution.

All access to ``jupyter-kernel-client`` private attributes lives here.  The
controller has one consumer of the shell and IOPub queues per connected kernel.
"""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass
from typing import Any, Literal, TYPE_CHECKING

from colab_cli.runtime import ColabRuntime

if TYPE_CHECKING:
    from better_colab.storage import StoredSession


class TransportDisconnected(RuntimeError):
    """The websocket stopped before the requested proof was complete."""


@dataclass(frozen=True)
class PreparedExecution:
    """One complete execute request that may be sent exactly once."""

    message_id: str
    message: dict[str, Any]


@dataclass(frozen=True)
class KernelEvent:
    channel: Literal["shell", "iopub"]
    message: dict[str, Any]


@dataclass(frozen=True)
class ProofObservation:
    matched: bool
    confirm_dispatch: bool = False
    valid_reply: bool | None = None
    reply_received: bool = False
    idle_received: bool = False
    terminal_state: str | None = None
    reply_status: str | None = None
    error_name: str | None = None
    error_value: str | None = None
    traceback: list[str] | None = None


class ExecutionProof:
    """Accumulate only matching execute-reply and idle evidence."""

    _INTERRUPT_ERRORS = frozenset(
        {
            "KeyboardInterrupt",
            "InterruptedError",
            "CancelledError",
        }
    )

    def __init__(self, message_id: str):
        self.message_id = message_id
        self.dispatch_confirmed = False
        self.reply_received = False
        self.idle_received = False
        self.reply_status: str | None = None
        self.error_name: str | None = None
        self.error_value: str | None = None
        self.traceback: list[str] | None = None
        self._requested_terminal: str | None = None

    def request_interrupt(self, terminal_state: str) -> None:
        if terminal_state not in {"interrupted", "timed_out"}:
            raise ValueError("interrupt terminal must be interrupted or timed_out")
        if self._requested_terminal != "timed_out":
            self._requested_terminal = terminal_state

    def observe(self, event: KernelEvent) -> ProofObservation:
        message = event.message
        parent = message.get("parent_header")
        if not isinstance(parent, dict) or parent.get("msg_id") != self.message_id:
            return ProofObservation(matched=False)

        first_match = not self.dispatch_confirmed
        self.dispatch_confirmed = True
        message_type = self._message_type(message)
        content = message.get("content")
        valid_reply: bool | None = None
        reply_delta = False
        idle_delta = False

        if event.channel == "shell" and message_type == "execute_reply":
            valid_reply = self._observe_reply(content)
            reply_delta = bool(valid_reply)
        elif (
            event.channel == "iopub"
            and message_type == "status"
            and isinstance(content, dict)
            and content.get("execution_state") == "idle"
        ):
            if not self.idle_received:
                self.idle_received = True
                idle_delta = True
        elif (
            event.channel == "iopub"
            and message_type == "error"
            and isinstance(content, dict)
        ):
            self._capture_error(content)

        return ProofObservation(
            matched=True,
            confirm_dispatch=first_match,
            valid_reply=valid_reply,
            reply_received=reply_delta,
            idle_received=idle_delta,
            terminal_state=self._terminal_state(),
            reply_status=self.reply_status,
            error_name=self.error_name,
            error_value=self.error_value,
            traceback=self.traceback,
        )

    @staticmethod
    def _message_type(message: dict[str, Any]) -> str | None:
        header = message.get("header")
        if isinstance(header, dict) and isinstance(header.get("msg_type"), str):
            return header["msg_type"]
        value = message.get("msg_type")
        return value if isinstance(value, str) else None

    def _observe_reply(self, content: Any) -> bool:
        if not isinstance(content, dict):
            return False
        status = content.get("status")
        if status not in {"ok", "error", "aborted"}:
            return False
        if self.reply_received:
            return True
        self.reply_received = True
        self.reply_status = status
        if status in {"error", "aborted"}:
            self._capture_error(content)
        return True

    def _capture_error(self, content: dict[str, Any]) -> None:
        error_name = content.get("ename")
        error_value = content.get("evalue")
        traceback = content.get("traceback")
        if isinstance(error_name, str):
            self.error_name = error_name
        if isinstance(error_value, str):
            self.error_value = error_value
        if isinstance(traceback, list) and all(
            isinstance(line, str) for line in traceback
        ):
            self.traceback = traceback

    def _terminal_state(self) -> str | None:
        if not (self.reply_received and self.idle_received):
            return None
        if self.reply_status == "ok":
            return "finished"
        if self._requested_terminal is not None and (
            self.reply_status == "aborted"
            or self.error_name in self._INTERRUPT_ERRORS
        ):
            return self._requested_terminal
        return "error"


class ReadinessProof:
    """Validate one no-history nonce execution through raw kernel messages."""

    def __init__(self, message_id: str, nonce: str):
        self.message_id = message_id
        self.nonce = nonce
        self.reply_valid = False
        self.idle_received = False
        self.error: str | None = None

    @property
    def ready(self) -> bool:
        return (
            self.reply_valid
            and self.idle_received
            and self.error is None
        )

    def observe(self, event: KernelEvent) -> bool:
        parent = event.message.get("parent_header")
        if not isinstance(parent, dict) or parent.get("msg_id") != self.message_id:
            return False
        message_type = ExecutionProof._message_type(event.message)
        content = event.message.get("content")
        if (
            event.channel == "iopub"
            and message_type == "status"
            and isinstance(content, dict)
            and content.get("execution_state") == "idle"
        ):
            self.idle_received = True
            return True
        if event.channel != "shell" or message_type != "execute_reply":
            return True
        if not isinstance(content, dict):
            self.error = "MALFORMED_REPLY"
            return True
        if content.get("status") != "ok":
            self.error = "PROBE_EXECUTION_ERROR"
            return True
        expressions = content.get("user_expressions")
        expression = (
            expressions.get("better_colab_nonce")
            if isinstance(expressions, dict)
            else None
        )
        if not isinstance(expression, dict) or expression.get("status") != "ok":
            self.error = "NONCE_MISSING"
            return True
        data = expression.get("data")
        rendered = data.get("text/plain") if isinstance(data, dict) else None
        if rendered != repr(self.nonce):
            self.error = "NONCE_MISMATCH"
            return True
        self.reply_valid = True
        return True


class KernelTransportAdapter:
    """Single-reader adapter around the pinned blocking websocket client."""

    def __init__(
        self,
        kernel_client: Any,
        *,
        kernel_id: str,
        jupyter_session_id: str,
        runtime: ColabRuntime | None = None,
    ):
        self._kernel_client = kernel_client
        self._client = kernel_client._manager.client
        self._runtime = runtime
        self.kernel_id = kernel_id
        self.jupyter_session_id = jupyter_session_id
        self._prefer_shell = False

    @classmethod
    def connect(cls, session: StoredSession) -> KernelTransportAdapter:
        runtime = ColabRuntime(
            session.backend_url,
            session.runtime_token,
            session_name=session.name,
            kernel_id=session.kernel_id,
            session_id=session.jupyter_session_id,
        )
        kernel_client = runtime.kernel_client
        kernel_id = runtime.kernel_id or kernel_client.id
        private_client = kernel_client._manager.client
        jupyter_session_id = runtime.session_id or private_client.session.session
        if not kernel_id or not jupyter_session_id:
            runtime.stop()
            raise RuntimeError("kernel connection did not expose stable identities")
        return cls(
            kernel_client,
            kernel_id=str(kernel_id),
            jupyter_session_id=str(jupyter_session_id),
            runtime=runtime,
        )

    @classmethod
    def from_connected_client(
        cls,
        kernel_client: Any,
        *,
        kernel_id: str,
        jupyter_session_id: str,
    ) -> KernelTransportAdapter:
        """Conformance-test hook for an already connected client."""
        return cls(
            kernel_client,
            kernel_id=kernel_id,
            jupyter_session_id=jupyter_session_id,
        )

    def prepare_execution(
        self,
        code: str,
        *,
        silent: bool = False,
        store_history: bool = True,
    ) -> PreparedExecution:
        if not isinstance(code, str):
            raise TypeError("execution source must be text")
        content = {
            "code": code,
            "silent": silent,
            "store_history": False if silent else store_history,
            "user_expressions": {},
            "allow_stdin": False,
            "stop_on_error": True,
        }
        message = self._client.session.msg("execute_request", content)
        return PreparedExecution(
            message_id=str(message["header"]["msg_id"]),
            message=message,
        )

    def prepare_readiness_probe(self, nonce: str) -> PreparedExecution:
        if not isinstance(nonce, str) or not nonce:
            raise ValueError("readiness nonce must be non-empty text")
        content = {
            "code": "None",
            "silent": False,
            "store_history": False,
            "user_expressions": {"better_colab_nonce": repr(nonce)},
            "allow_stdin": False,
            "stop_on_error": True,
        }
        message = self._client.session.msg("execute_request", content)
        return PreparedExecution(
            message_id=str(message["header"]["msg_id"]),
            message=message,
        )

    def send(self, prepared: PreparedExecution) -> None:
        if prepared.message_id != prepared.message.get("header", {}).get("msg_id"):
            raise ValueError("prepared message ID does not match its header")
        self._client.shell_channel.send(prepared.message)

    def next_event(self, *, timeout: float | None) -> KernelEvent:
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while True:
            event = self._take_ready_message()
            if event is not None:
                return event
            if not self._client.connection_ready.is_set():
                raise TransportDisconnected("kernel websocket connection was lost")
            remaining = (
                None
                if deadline is None
                else max(0.0, deadline - time.monotonic())
            )
            if remaining == 0:
                raise TimeoutError
            self._client._message_received.wait(timeout=remaining)
            if not (
                self._client.shell_channel.msg_ready()
                or self._client.iopub_channel.msg_ready()
            ):
                self._client._message_received.clear()

    def _take_ready_message(self) -> KernelEvent | None:
        channel_order = (
            ("shell", self._client.shell_channel),
            ("iopub", self._client.iopub_channel),
        )
        if not self._prefer_shell:
            channel_order = tuple(reversed(channel_order))
        for name, channel in channel_order:
            if not channel.msg_ready():
                continue
            try:
                message = channel.get_msg(timeout=0)
            except queue.Empty:
                continue
            self._prefer_shell = name != "shell"
            if isinstance(message, dict):
                return KernelEvent(channel=name, message=message)
        return None

    def interrupt(self) -> None:
        self._kernel_client.interrupt()

    def close(self) -> None:
        if self._runtime is not None:
            self._runtime.stop()
            return
        client = self._client
        stop_channels = getattr(client, "stop_channels", None)
        if callable(stop_channels):
            stop_channels()
