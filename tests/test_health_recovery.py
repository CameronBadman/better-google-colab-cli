import asyncio
import os
import queue
import signal
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from better_colab import ExecutionState
from better_colab.controller import ControllerServer
from better_colab.controller_client import ControllerClient
from better_colab.execution import ExecutionCoordinator
from better_colab.kernel_transport import (
    KernelEvent,
    KernelIdleProof,
    PreparedExecution,
)
from better_colab.models import CompletionSource
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
        kernel_id="kernel-live",
        jupyter_session_id="jupyter-live",
    )
    yield value
    value.close()


def _event(channel, message_type, parent, content):
    return KernelEvent(
        channel=channel,
        message={
            "header": {"msg_type": message_type, "msg_id": str(uuid.uuid4())},
            "parent_header": {"msg_id": parent},
            "content": content,
        },
    )


def _create_running(store, execution_id, *, message_id="original-message"):
    record = store.create_execution(
        execution_id=execution_id,
        session_name="training",
        source=b"import time; time.sleep(10)",
        provenance={"kind": "stdin"},
        request={"session": "training", "source_sha256": "source"},
    )
    store.begin_dispatch(
        record.execution_id,
        kernel_message_id=message_id,
        session_endpoint="endpoint",
        kernel_id="kernel-live",
        jupyter_session_id="jupyter-live",
    )
    return store.confirm_dispatch(record.execution_id)


def test_kernel_idle_proof_requires_matching_reply_and_idle():
    proof = KernelIdleProof("kernel-info")
    proof.observe(
        _event(
            "iopub",
            "status",
            "kernel-info",
            {"execution_state": "idle"},
        )
    )
    assert proof.idle is False
    proof.observe(
        _event(
            "shell",
            "kernel_info_reply",
            "kernel-info",
            {"status": "ok", "protocol_version": "5.3"},
        )
    )
    assert proof.idle is True


class _RecoveryTransport:
    kernel_id = "kernel-live"
    jupyter_session_id = "jupyter-live"

    def __init__(self, events):
        self.events = queue.Queue()
        for event in events:
            self.events.put(event)
        self.sent_message_types = []
        self.closed = False

    def prepare_execution(self, _code):
        raise AssertionError("confirmed execution must never be replayed")

    def prepare_kernel_info(self):
        return PreparedExecution(
            message_id="kernel-info",
            message={
                "header": {
                    "msg_id": "kernel-info",
                    "msg_type": "kernel_info_request",
                },
                "content": {},
            },
        )

    def send(self, prepared):
        self.sent_message_types.append(prepared.message["header"]["msg_type"])

    def next_event(self, timeout):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError

    def interrupt(self):
        pass

    def close(self):
        self.closed = True


def test_restart_reconciles_ambiguous_and_already_durable_proof(store):
    dispatching = store.create_execution(
        execution_id="00000000-0000-4000-8000-000000000701",
        session_name="training",
        source=b"pass",
        provenance={"kind": "stdin"},
        request={"session": "training", "source_sha256": "dispatching"},
    )
    store.begin_dispatch(
        dispatching.execution_id,
        kernel_message_id="ambiguous-message",
        session_endpoint="endpoint",
        kernel_id="kernel-live",
        jupyter_session_id="jupyter-live",
    )
    durable = _create_running(
        store,
        "00000000-0000-4000-8000-000000000702",
        message_id="durable-message",
    )
    store.record_execution_evidence(
        durable.execution_id,
        reply_received=True,
        idle_received=True,
        reply_status="ok",
    )

    recovery_ids = store.reconcile_after_restart()

    ambiguous = store.get_execution(dispatching.execution_id)
    completed = store.get_execution(durable.execution_id)
    assert recovery_ids == []
    assert ambiguous.state is ExecutionState.UNKNOWN
    assert ambiguous.output_complete is False
    assert completed.state is ExecutionState.FINISHED
    assert completed.output_complete is True
    assert completed.completion_source is CompletionSource.DURABLE_EVIDENCE


def test_confirmed_restart_reconnects_without_replay_and_accepts_late_proof(
    paths,
    profile,
    store,
):
    running = _create_running(
        store,
        "00000000-0000-4000-8000-000000000703",
    )
    recovery_ids = store.reconcile_after_restart()
    transport = _RecoveryTransport(
        [
            _event(
                "iopub",
                "stream",
                "original-message",
                {"name": "stdout", "text": "after-reconnect\n"},
            ),
            _event(
                "shell",
                "execute_reply",
                "original-message",
                {"status": "ok", "execution_count": 1},
            ),
            _event(
                "iopub",
                "status",
                "original-message",
                {"execution_state": "idle"},
            ),
        ]
    )
    coordinator = ExecutionCoordinator(
        paths=paths,
        transport_factory=lambda _session: transport,
    )

    assert recovery_ids == [running.execution_id]
    coordinator.recover(profile, running.execution_id)
    coordinator.wait_idle(timeout=2)

    recovered = store.get_execution(running.execution_id)
    output = store.list_output_events(running.execution_id)
    coordinator.close()
    assert recovered.state is ExecutionState.FINISHED
    assert recovered.output_complete is False
    assert recovered.reconnect_count == 1
    assert recovered.completion_source is CompletionSource.RECOVERY
    assert output[0]["text"] == "after-reconnect\n"
    assert transport.sent_message_types == ["kernel_info_request"]


def test_recovery_idle_boundary_without_terminal_proof_becomes_unknown(
    paths,
    profile,
    store,
):
    running = _create_running(
        store,
        "00000000-0000-4000-8000-000000000704",
    )
    store.reconcile_after_restart()
    transport = _RecoveryTransport(
        [
            _event(
                "shell",
                "kernel_info_reply",
                "kernel-info",
                {"status": "ok"},
            ),
            _event(
                "iopub",
                "status",
                "kernel-info",
                {"execution_state": "idle"},
            ),
        ]
    )
    coordinator = ExecutionCoordinator(
        paths=paths,
        transport_factory=lambda _session: transport,
    )

    coordinator.recover(profile, running.execution_id)
    coordinator.wait_idle(timeout=2)

    recovered = store.get_execution(running.execution_id)
    coordinator.close()
    assert recovered.state is ExecutionState.UNKNOWN
    assert recovered.output_complete is False
    assert store.list_transitions(running.execution_id)[-1].reason == (
        "kernel_idle_without_terminal_proof"
    )
    assert transport.sent_message_types == ["kernel_info_request"]


def test_controller_start_reconciles_recovery_before_queued_work(
    paths,
    profile,
    store,
):
    running = _create_running(
        store,
        "00000000-0000-4000-8000-000000000705",
    )
    queued = store.create_execution(
        execution_id="00000000-0000-4000-8000-000000000706",
        session_name="training",
        source=b"pass",
        provenance={"kind": "stdin"},
        request={"session": "training", "source_sha256": "queued"},
    )
    transport = _RecoveryTransport(
        [
            _event(
                "shell",
                "kernel_info_reply",
                "kernel-info",
                {"status": "ok"},
            ),
            _event(
                "iopub",
                "status",
                "kernel-info",
                {"execution_state": "idle"},
            ),
        ]
    )
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

    assert store.get_execution(running.execution_id).state is ExecutionState.UNKNOWN
    # The fake refuses execution sends. Reaching queued work therefore proves
    # startup scheduled recovery first, then handled its worker failure.
    assert store.get_execution(queued.execution_id).state is ExecutionState.INTERRUPTED


def test_hard_controller_death_elects_one_replacement(paths):
    client = ControllerClient(paths=paths, startup_timeout=5)
    original = client.ensure_running()
    os.kill(original["pid"], signal.SIGKILL)
    deadline = time.monotonic() + 3
    while client._lifetime_lock_held() and time.monotonic() < deadline:
        time.sleep(0.01)
    clients = [ControllerClient(paths=paths, startup_timeout=5) for _ in range(8)]

    try:
        with ThreadPoolExecutor(max_workers=len(clients)) as pool:
            statuses = list(pool.map(lambda item: item.ensure_running(), clients))
        replacement_pids = {status["pid"] for status in statuses}
        assert len(replacement_pids) == 1
        assert original["pid"] not in replacement_pids
    finally:
        try:
            clients[0].stop(force=True)
        except Exception:
            pass
