import asyncio
import queue
import time

import pytest

from better_colab import ExecutionState
from better_colab.controller import ControllerServer
from better_colab.execution import ExecutionCoordinator
from better_colab.kernel_transport import KernelEvent, PreparedExecution
from better_colab.storage import DurableStore, ProfileSpec, StatePaths


@pytest.fixture
def paths(tmp_path):
    return StatePaths(
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "run",
    )


@pytest.fixture
def profile(tmp_path):
    return ProfileSpec.from_values(
        config_path=tmp_path / "sessions.json",
        auth_provider="oauth2",
        oauth_config_path=tmp_path / "oauth.json",
    )


@pytest.fixture
def store(paths, profile):
    value = DurableStore(paths=paths, profile=profile)
    value.upsert_session(
        name="training",
        endpoint="endpoint",
        backend_url="https://runtime.example",
        runtime_token="secret",
        hardware="CPU",
    )
    yield value
    value.close()


def _event(channel, message_type, parent, content):
    return KernelEvent(
        channel=channel,
        message={
            "header": {"msg_type": message_type, "msg_id": f"{message_type}-id"},
            "parent_header": {"msg_id": parent},
            "content": content,
        },
    )


class _BatchTransport:
    kernel_id = "kernel-live"
    jupyter_session_id = "jupyter-live"

    def __init__(self, *, block=False):
        self.events = queue.Queue()
        self.sent_sources = []
        self.message_count = 0
        self.block = block
        self.current_message_id = None
        self.interrupt_calls = 0

    def prepare_execution(self, code):
        self.message_count += 1
        message_id = f"message-{self.message_count}"
        return PreparedExecution(
            message_id=message_id,
            message={
                "header": {
                    "msg_id": message_id,
                    "msg_type": "execute_request",
                },
                "content": {"code": code},
            },
        )

    def send(self, prepared):
        self.current_message_id = prepared.message_id
        source = prepared.message["content"]["code"]
        self.sent_sources.append(source)
        self.events.put(
            _event(
                "iopub",
                "stream",
                prepared.message_id,
                {"name": "stdout", "text": f"{source}\n"},
            )
        )
        if self.block:
            return
        if "raise" in source:
            reply = {
                "status": "error",
                "ename": "ValueError",
                "evalue": "bad",
                "traceback": ["trace"],
            }
        else:
            reply = {"status": "ok", "execution_count": self.message_count}
        self.events.put(
            _event("shell", "execute_reply", prepared.message_id, reply)
        )
        self.events.put(
            _event(
                "iopub",
                "status",
                prepared.message_id,
                {"execution_state": "idle"},
            )
        )

    def next_event(self, timeout):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError

    def interrupt(self):
        self.interrupt_calls += 1
        self.block = False
        self.events.put(
            _event(
                "shell",
                "execute_reply",
                self.current_message_id,
                {
                    "status": "error",
                    "ename": "KeyboardInterrupt",
                    "evalue": "",
                    "traceback": ["KeyboardInterrupt"],
                },
            )
        )
        self.events.put(
            _event(
                "iopub",
                "status",
                self.current_message_id,
                {"execution_state": "idle"},
            )
        )

    def close(self):
        pass


def _child(store, execution_id, source):
    return store.create_execution(
        execution_id=execution_id,
        session_name="training",
        source=source.encode(),
        provenance={
            "kind": "notebook_cell",
            "path": "/tmp/notebook.ipynb",
            "notebook_id": "a" * 64,
            "cell_id": execution_id[-4:],
            "cell_index": int(execution_id[-1]),
        },
        request={"session": "training", "source_sha256": store.sha256(source.encode())},
    )


def _batch(store, *, continue_on_error):
    first = _child(
        store,
        "00000000-0000-4000-8000-000000000811",
        "raise ValueError('bad')",
    )
    second = _child(
        store,
        "00000000-0000-4000-8000-000000000812",
        "after = 2",
    )
    batch = store.create_batch(
        batch_id="10000000-0000-4000-8000-000000000801",
        session_name="training",
        execution_ids=[first.execution_id, second.execution_id],
        continue_on_error=continue_on_error,
    )
    return batch, first, second


def test_batch_stops_after_first_error_and_marks_undispatched_child(
    paths,
    profile,
    store,
):
    batch, first, second = _batch(store, continue_on_error=False)
    transport = _BatchTransport()
    coordinator = ExecutionCoordinator(
        paths=paths,
        transport_factory=lambda _session: transport,
    )

    coordinator.submit_batch(profile, batch.batch_id)
    coordinator.wait_idle(timeout=2)

    assert store.get_execution(first.execution_id).state is ExecutionState.ERROR
    assert (
        store.get_execution(second.execution_id).state
        is ExecutionState.INTERRUPTED
    )
    assert store.list_transitions(second.execution_id)[-1].reason == "batch_stopped"
    assert store.get_batch(batch.batch_id).state == "error"
    assert transport.sent_sources == ["raise ValueError('bad')"]
    coordinator.close()


def test_batch_continue_on_error_dispatches_every_child(
    paths,
    profile,
    store,
):
    batch, first, second = _batch(store, continue_on_error=True)
    transport = _BatchTransport()
    coordinator = ExecutionCoordinator(
        paths=paths,
        transport_factory=lambda _session: transport,
    )

    coordinator.submit_batch(profile, batch.batch_id)
    coordinator.wait_idle(timeout=2)

    assert store.get_execution(first.execution_id).state is ExecutionState.ERROR
    assert store.get_execution(second.execution_id).state is ExecutionState.FINISHED
    assert store.get_batch(batch.batch_id).state == "error"
    assert transport.sent_sources == ["raise ValueError('bad')", "after = 2"]
    coordinator.close()


def test_batch_cancel_interrupts_running_and_never_dispatches_queued_child(
    paths,
    profile,
    store,
):
    batch, first, second = _batch(store, continue_on_error=True)
    transport = _BatchTransport(block=True)
    coordinator = ExecutionCoordinator(
        paths=paths,
        transport_factory=lambda _session: transport,
    )

    coordinator.submit_batch(profile, batch.batch_id)
    deadline = time.monotonic() + 2
    while (
        store.get_execution(first.execution_id).state is not ExecutionState.RUNNING
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    coordinator.cancel_batch(profile, batch.batch_id)
    coordinator.wait_idle(timeout=2)

    assert (
        store.get_execution(first.execution_id).state
        is ExecutionState.INTERRUPTED
    )
    assert (
        store.get_execution(second.execution_id).state
        is ExecutionState.INTERRUPTED
    )
    assert store.get_batch(batch.batch_id).state == "interrupted"
    assert transport.interrupt_calls == 1
    assert transport.sent_sources == ["raise ValueError('bad')"]
    coordinator.close()


def test_controller_restart_resumes_batch_policy_instead_of_children(
    paths,
    profile,
    store,
):
    batch, first, second = _batch(store, continue_on_error=False)
    transport = _BatchTransport()
    server = ControllerServer(
        paths=paths,
        transport_factory=lambda _session: transport,
    )

    async def scenario():
        await server.start()
        try:
            server._coordinator.wait_idle(timeout=2)
        finally:
            await server.close()

    asyncio.run(scenario())

    assert store.get_execution(first.execution_id).state is ExecutionState.ERROR
    assert (
        store.get_execution(second.execution_id).state
        is ExecutionState.INTERRUPTED
    )
    assert store.list_transitions(second.execution_id)[-1].reason == "batch_stopped"
    assert store.get_batch(batch.batch_id).state == "error"
    assert transport.sent_sources == ["raise ValueError('bad')"]
