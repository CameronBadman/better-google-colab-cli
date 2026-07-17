import importlib.metadata
import json
import queue
import threading

import pytest
from jupyter_kernel_client.wsclient import KernelWebSocketClient

from better_colab.kernel_transport import (
    ExecutionProof,
    KernelEvent,
    KernelTransportAdapter,
    TransportDisconnected,
)


class _Channel:
    def __init__(self):
        self.messages = queue.Queue()
        self.sent = []

    def send(self, message):
        self.sent.append(message)

    def get_msg(self, timeout=None):
        return self.messages.get(timeout=timeout)

    def msg_ready(self):
        return not self.messages.empty()


class _PrivateClient:
    def __init__(self):
        from jupyter_client.session import Session

        self.session = Session()
        self.shell_channel = _Channel()
        self.iopub_channel = _Channel()
        self.connection_ready = threading.Event()
        self.connection_ready.set()
        self._message_received = threading.Event()


class _Manager:
    def __init__(self, client):
        self.client = client


class _KernelClient:
    def __init__(self, client):
        self._manager = _Manager(client)
        self.interrupt_calls = 0

    def interrupt(self):
        self.interrupt_calls += 1


def _message(
    message_id,
    *,
    channel,
    message_type,
    content=None,
    parent=None,
):
    return KernelEvent(
        channel=channel,
        message={
            "header": {"msg_type": message_type, "msg_id": message_id},
            "parent_header": {"msg_id": parent} if parent else {},
            "content": content or {},
        },
    )


def test_pinned_kernel_client_private_conformance():
    distribution = importlib.metadata.distribution("jupyter-kernel-client")
    direct_url = json.loads(distribution.read_text("direct_url.json"))
    client = KernelWebSocketClient(endpoint="ws://invalid.example/channels")

    assert distribution.version == "0.8.0"
    assert (
        direct_url["vcs_info"]["commit_id"]
        == "f18e982c3265df5e923aa9def101ab3fd737e139"
    )
    assert hasattr(client, "_shell_msg_queue")
    assert hasattr(client, "_iopub_msg_queue")
    assert hasattr(client, "_message_received")
    assert hasattr(client, "connection_ready")
    assert callable(client.session.msg)


def test_adapter_prepares_then_sends_the_exact_same_request():
    private = _PrivateClient()
    wrapper = _KernelClient(private)
    adapter = KernelTransportAdapter.from_connected_client(
        wrapper,
        kernel_id="kernel-1",
        jupyter_session_id="jupyter-1",
    )

    prepared = adapter.prepare_execution("print('hello')")

    assert prepared.message_id == prepared.message["header"]["msg_id"]
    assert prepared.message["msg_type"] == "execute_request"
    assert prepared.message["content"] == {
        "code": "print('hello')",
        "silent": False,
        "store_history": True,
        "user_expressions": {},
        "allow_stdin": False,
        "stop_on_error": True,
    }

    adapter.send(prepared)

    assert private.shell_channel.sent == [prepared.message]
    assert private.shell_channel.sent[0] is prepared.message


def test_adapter_prepares_no_history_nonce_request():
    private = _PrivateClient()
    adapter = KernelTransportAdapter.from_connected_client(
        _KernelClient(private),
        kernel_id="kernel-1",
        jupyter_session_id="jupyter-1",
    )

    readiness = adapter.prepare_readiness_probe("nonce-123")

    assert readiness.message["msg_type"] == "execute_request"
    assert readiness.message["content"] == {
        "code": "None",
        "silent": False,
        "store_history": False,
        "user_expressions": {
            "better_colab_nonce": repr("nonce-123"),
        },
        "allow_stdin": False,
        "stop_on_error": True,
    }


def test_adapter_is_the_single_reader_for_shell_and_iopub():
    private = _PrivateClient()
    adapter = KernelTransportAdapter.from_connected_client(
        _KernelClient(private),
        kernel_id="kernel-1",
        jupyter_session_id="jupyter-1",
    )
    private.iopub_channel.messages.put(
        {
            "header": {"msg_type": "stream"},
            "parent_header": {"msg_id": "request-1"},
            "content": {"name": "stdout", "text": "hello\n"},
        }
    )
    private._message_received.set()

    event = adapter.next_event(timeout=0.1)

    assert event.channel == "iopub"
    assert event.message["content"]["text"] == "hello\n"

    private.connection_ready.clear()
    with pytest.raises(TransportDisconnected):
        adapter.next_event(timeout=0.01)


@pytest.mark.parametrize("order", [("reply", "idle"), ("idle", "reply")])
def test_proof_requires_matching_reply_and_idle_in_either_order(order):
    proof = ExecutionProof("request-1")
    events = {
        "reply": _message(
            "reply-1",
            channel="shell",
            message_type="execute_reply",
            parent="request-1",
            content={"status": "ok", "execution_count": 1},
        ),
        "idle": _message(
            "status-1",
            channel="iopub",
            message_type="status",
            parent="request-1",
            content={"execution_state": "idle"},
        ),
    }

    first = proof.observe(events[order[0]])
    second = proof.observe(events[order[1]])

    assert first.matched is True
    assert first.confirm_dispatch is True
    assert first.terminal_state is None
    assert second.matched is True
    assert second.terminal_state == "finished"
    assert proof.reply_received is True
    assert proof.idle_received is True


def test_output_idle_malformed_and_mismatched_messages_never_prove_success():
    proof = ExecutionProof("request-1")

    output = proof.observe(
        _message(
            "output-1",
            channel="iopub",
            message_type="stream",
            parent="request-1",
            content={"name": "stdout", "text": ""},
        )
    )
    idle = proof.observe(
        _message(
            "status-1",
            channel="iopub",
            message_type="status",
            parent="request-1",
            content={"execution_state": "idle"},
        )
    )
    malformed_reply = proof.observe(
        _message(
            "reply-1",
            channel="shell",
            message_type="execute_reply",
            parent="request-1",
            content={},
        )
    )
    mismatched_reply = proof.observe(
        _message(
            "reply-2",
            channel="shell",
            message_type="execute_reply",
            parent="another-request",
            content={"status": "ok"},
        )
    )

    assert output.confirm_dispatch is True
    assert output.terminal_state is None
    assert idle.terminal_state is None
    assert malformed_reply.terminal_state is None
    assert malformed_reply.valid_reply is False
    assert mismatched_reply.matched is False
    assert proof.reply_received is False


def test_error_proof_preserves_error_metadata_and_prior_output_is_irrelevant():
    proof = ExecutionProof("request-1")
    proof.observe(
        _message(
            "output-1",
            channel="iopub",
            message_type="stream",
            parent="request-1",
            content={"name": "stdout", "text": "before\n"},
        )
    )
    reply = proof.observe(
        _message(
            "reply-1",
            channel="shell",
            message_type="execute_reply",
            parent="request-1",
            content={
                "status": "error",
                "ename": "ValueError",
                "evalue": "bad",
                "traceback": ["trace line"],
            },
        )
    )
    terminal = proof.observe(
        _message(
            "status-1",
            channel="iopub",
            message_type="status",
            parent="request-1",
            content={"execution_state": "idle"},
        )
    )

    assert reply.error_name == "ValueError"
    assert reply.error_value == "bad"
    assert reply.traceback == ["trace line"]
    assert terminal.terminal_state == "error"


def test_verified_keyboard_interrupt_uses_requested_terminal_state():
    proof = ExecutionProof("request-1")
    proof.request_interrupt("timed_out")
    proof.observe(
        _message(
            "reply-1",
            channel="shell",
            message_type="execute_reply",
            parent="request-1",
            content={
                "status": "error",
                "ename": "KeyboardInterrupt",
                "evalue": "",
                "traceback": [],
            },
        )
    )
    terminal = proof.observe(
        _message(
            "status-1",
            channel="iopub",
            message_type="status",
            parent="request-1",
            content={"execution_state": "idle"},
        )
    )

    assert terminal.terminal_state == "timed_out"


def test_completion_race_wins_over_requested_cancel():
    proof = ExecutionProof("request-1")
    proof.request_interrupt("interrupted")
    proof.observe(
        _message(
            "reply-1",
            channel="shell",
            message_type="execute_reply",
            parent="request-1",
            content={"status": "ok"},
        )
    )
    terminal = proof.observe(
        _message(
            "status-1",
            channel="iopub",
            message_type="status",
            parent="request-1",
            content={"execution_state": "idle"},
        )
    )

    assert terminal.terminal_state == "finished"
