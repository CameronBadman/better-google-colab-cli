import queue
import threading
import time
from pathlib import Path

import pytest

from better_colab import BetterColabError, ExecutionState
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


def _event(channel, message_type, parent, content):
    return KernelEvent(
        channel=channel,
        message={
            "header": {"msg_type": message_type, "msg_id": f"{message_type}-id"},
            "parent_header": {"msg_id": parent},
            "content": content,
        },
    )


class _ScriptedTransport:
    def __init__(self, *, store, profile, events, block=False):
        self.store = store
        self.profile = profile
        self.events = queue.Queue()
        for event in events:
            self.events.put(event)
        self.block = block
        self.message_id = "kernel-message-1"
        self.send_calls = 0
        self.interrupt_calls = 0
        self.closed = False
        self.kernel_id = "kernel-actual"
        self.jupyter_session_id = "jupyter-actual"
        self.sent = threading.Event()

    def prepare_execution(self, code):
        return PreparedExecution(
            message_id=self.message_id,
            message={
                "header": {
                    "msg_id": self.message_id,
                    "msg_type": "execute_request",
                },
                "msg_id": self.message_id,
                "msg_type": "execute_request",
                "parent_header": {},
                "metadata": {},
                "content": {"code": code},
            },
        )

    def send(self, prepared):
        # A separate connection sees the durable crash boundary before send.
        with DurableStore(paths=self.store.paths, profile=self.profile) as observer:
            record = observer.get_execution(self.execution_id)
        assert record.state is ExecutionState.DISPATCHING
        assert record.kernel_message_id == prepared.message_id
        assert record.kernel_id_snapshot == self.kernel_id
        assert record.jupyter_session_id_snapshot == self.jupyter_session_id
        self.send_calls += 1
        self.sent.set()

    def next_event(self, timeout):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            if self.block:
                raise TimeoutError
            raise RuntimeError("script exhausted")

    def interrupt(self):
        self.interrupt_calls += 1

    def close(self):
        self.closed = True


def _create(store, execution_id, *, timeout=None, source=b"print('hello')"):
    return store.create_execution(
        execution_id=execution_id,
        session_name="training",
        source=source,
        provenance={"kind": "stdin"},
        request={
            "session": "training",
            "source_sha256": store.sha256(source),
            "execution_timeout": timeout,
        },
        idempotency_key=None,
        execution_timeout_seconds=timeout,
    )


def _store(paths, profile):
    store = DurableStore(paths=paths, profile=profile)
    store.upsert_session(
        name="training",
        endpoint="endpoint",
        backend_url="https://runtime.example",
        runtime_token="secret",
        hardware="CPU",
    )
    return store


def _terminal_events(message_id, *, status="ok"):
    return [
        _event(
            "shell",
            "execute_reply",
            message_id,
            (
                {"status": "ok", "execution_count": 1}
                if status == "ok"
                else {
                    "status": "error",
                    "ename": "ValueError",
                    "evalue": "bad",
                    "traceback": ["trace"],
                }
            ),
        ),
        _event(
            "iopub",
            "status",
            message_id,
            {"execution_state": "idle"},
        ),
    ]


def test_runner_commits_dispatch_before_send_and_completes_from_proof(
    paths, profile
):
    store = _store(paths, profile)
    execution = _create(
        store,
        "00000000-0000-4000-8000-000000000201",
    )
    transport = _ScriptedTransport(
        store=store,
        profile=profile,
        events=_terminal_events("kernel-message-1"),
    )
    transport.execution_id = execution.execution_id
    coordinator = ExecutionCoordinator(
        paths=paths,
        transport_factory=lambda _session: transport,
    )

    coordinator.submit(profile, execution.execution_id)
    coordinator.wait_idle(timeout=2)

    record = store.get_execution(execution.execution_id)
    transitions = store.list_transitions(execution.execution_id)
    coordinator.close()
    store.close()
    assert transport.send_calls == 1
    assert record.state is ExecutionState.FINISHED
    assert record.dispatch_confirmed is True
    assert record.reply_received is True
    assert record.idle_received is True
    assert record.source_spool_path is None
    assert [item.to_state for item in transitions[-3:]] == [
        ExecutionState.DISPATCHING,
        ExecutionState.RUNNING,
        ExecutionState.FINISHED,
    ]


def test_same_kernel_is_fifo_but_different_sessions_can_run_concurrently(
    paths, profile
):
    store = _store(paths, profile)
    store.upsert_session(
        name="other",
        endpoint="other-endpoint",
        backend_url="https://other.example",
        runtime_token="other-secret",
        hardware="CPU",
    )
    first = _create(
        store,
        "00000000-0000-4000-8000-000000000202",
    )
    second = _create(
        store,
        "00000000-0000-4000-8000-000000000203",
    )
    third = store.create_execution(
        execution_id="00000000-0000-4000-8000-000000000204",
        session_name="other",
        source=b"pass",
        provenance={"kind": "stdin"},
        request={"session": "other", "source_sha256": store.sha256(b"pass")},
    )
    gate = threading.Event()
    order = []

    class _Gated(_ScriptedTransport):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.counter = 0

        def prepare_execution(self, code):
            self.counter += 1
            self.message_id = f"kernel-message-{self.counter}"
            return super().prepare_execution(code)

        def send(self, prepared):
            row = self.store.connection.execute(
                """
                SELECT execution_id FROM executions
                WHERE kernel_message_id = ? AND session_name = ?
                """,
                (prepared.message_id, self.session_name),
            ).fetchone()
            execution_id = row["execution_id"]
            order.append(("send", execution_id))
            self.send_calls += 1
            self.sent.set()
            if execution_id == first.execution_id:
                gate.wait(1)
            for event in _terminal_events(prepared.message_id):
                self.events.put(event)

    transports = {}

    def factory(session):
        transport = _Gated(
            store=store,
            profile=profile,
            events=[],
            block=True,
        )
        transport.session_name = session.name
        transports.setdefault(session.name, []).append(transport)
        return transport

    coordinator = ExecutionCoordinator(paths=paths, transport_factory=factory)
    coordinator.submit(profile, first.execution_id)
    coordinator.submit(profile, second.execution_id)
    coordinator.submit(profile, third.execution_id)

    deadline = time.monotonic() + 1
    while (
        ("training" not in transports or "other" not in transports)
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    assert transports["training"][0].sent.wait(1)
    assert transports["other"][0].sent.wait(1)
    assert store.get_execution(second.execution_id).state is ExecutionState.QUEUED
    gate.set()
    coordinator.wait_idle(timeout=2)

    coordinator.close()
    store.close()
    assert order.index(("send", first.execution_id)) < order.index(
        ("send", second.execution_id)
    )
    assert ("send", third.execution_id) in order
    assert len(transports["training"]) == 1


def test_disconnect_before_confirmation_is_unknown_and_never_replayed(
    paths, profile
):
    from better_colab.kernel_transport import TransportDisconnected

    store = _store(paths, profile)
    execution = _create(
        store,
        "00000000-0000-4000-8000-000000000205",
    )

    class _Disconnect(_ScriptedTransport):
        def next_event(self, timeout):
            raise TransportDisconnected("lost")

    transport = _Disconnect(store=store, profile=profile, events=[])
    transport.execution_id = execution.execution_id
    coordinator = ExecutionCoordinator(
        paths=paths,
        transport_factory=lambda _session: transport,
    )

    coordinator.submit(profile, execution.execution_id)
    coordinator.wait_idle(timeout=2)

    record = store.get_execution(execution.execution_id)
    coordinator.close()
    store.close()
    assert transport.send_calls == 1
    assert record.state is ExecutionState.UNKNOWN
    assert record.dispatch_confirmed is False


def test_confirmed_disconnect_is_disconnected_and_output_incomplete(
    paths, profile
):
    from better_colab.kernel_transport import TransportDisconnected

    store = _store(paths, profile)
    execution = _create(
        store,
        "00000000-0000-4000-8000-000000000206",
    )
    first = _event(
        "iopub",
        "stream",
        "kernel-message-1",
        {"name": "stdout", "text": "before\n"},
    )

    class _Disconnect(_ScriptedTransport):
        def next_event(self, timeout):
            if not self.events.empty():
                return self.events.get()
            raise TransportDisconnected("lost")

    transport = _Disconnect(store=store, profile=profile, events=[first])
    transport.execution_id = execution.execution_id
    factory_calls = 0

    def factory(_session):
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            return transport
        raise ConnectionError("reconnect unavailable")

    coordinator = ExecutionCoordinator(
        paths=paths,
        transport_factory=factory,
    )

    coordinator.submit(profile, execution.execution_id)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        record = store.get_execution(execution.execution_id)
        if record.state is ExecutionState.DISCONNECTED:
            break
        time.sleep(0.01)

    record = store.get_execution(execution.execution_id)
    events = store.list_output_events(execution.execution_id)
    coordinator.close()
    store.close()
    assert record.state is ExecutionState.DISCONNECTED
    assert record.dispatch_confirmed is True
    assert record.output_complete is False
    assert events[0]["text"] == "before\n"


def test_exception_preserves_preceding_stdout_and_metadata(paths, profile):
    store = _store(paths, profile)
    execution = _create(
        store,
        "00000000-0000-4000-8000-000000000207",
    )
    events = [
        _event(
            "iopub",
            "stream",
            "kernel-message-1",
            {"name": "stdout", "text": "before\n"},
        ),
        *_terminal_events("kernel-message-1", status="error"),
    ]
    transport = _ScriptedTransport(store=store, profile=profile, events=events)
    transport.execution_id = execution.execution_id
    coordinator = ExecutionCoordinator(
        paths=paths,
        transport_factory=lambda _session: transport,
    )

    coordinator.submit(profile, execution.execution_id)
    coordinator.wait_idle(timeout=2)

    record = store.get_execution(execution.execution_id)
    output = store.list_output_events(execution.execution_id)
    coordinator.close()
    store.close()
    assert record.state is ExecutionState.ERROR
    assert record.error_name == "ValueError"
    assert record.error_value == "bad"
    assert record.traceback_json == '["trace"]'
    assert output[0]["text"] == "before\n"


def test_internal_observation_failure_becomes_unknown_not_stuck_running(
    paths, profile, monkeypatch
):
    store = _store(paths, profile)
    execution = _create(
        store,
        "00000000-0000-4000-8000-000000000212",
    )
    events = [
        _event(
            "iopub",
            "stream",
            "kernel-message-1",
            {"name": "stdout", "text": "cannot-persist\n"},
        ),
        *_terminal_events("kernel-message-1"),
    ]
    transport = _ScriptedTransport(store=store, profile=profile, events=events)
    transport.execution_id = execution.execution_id

    def fail_output(*_args, **_kwargs):
        raise OSError("simulated spool failure")

    monkeypatch.setattr(DurableStore, "append_output_event", fail_output)
    coordinator = ExecutionCoordinator(
        paths=paths,
        transport_factory=lambda _session: transport,
    )

    coordinator.submit(profile, execution.execution_id)
    coordinator.wait_idle(timeout=2)

    record = store.get_execution(execution.execution_id)
    coordinator.close()
    store.close()
    assert record.state is ExecutionState.UNKNOWN
    assert record.output_complete is False


def test_queued_cancel_never_dispatches(paths, profile):
    store = _store(paths, profile)
    execution = _create(
        store,
        "00000000-0000-4000-8000-000000000208",
    )
    transport_created = False

    def factory(_session):
        nonlocal transport_created
        transport_created = True
        raise AssertionError("queued cancellation must not connect")

    coordinator = ExecutionCoordinator(paths=paths, transport_factory=factory)
    cancelled = coordinator.cancel(profile, execution.execution_id)

    coordinator.close()
    store.close()
    assert cancelled.state is ExecutionState.INTERRUPTED
    assert transport_created is False


def test_running_cancel_interrupts_and_waits_for_matching_proof(paths, profile):
    store = _store(paths, profile)
    execution = _create(
        store,
        "00000000-0000-4000-8000-000000000209",
        source=b"while True: pass",
    )
    transport = _ScriptedTransport(
        store=store,
        profile=profile,
        events=[],
        block=True,
    )
    transport.execution_id = execution.execution_id
    coordinator = ExecutionCoordinator(
        paths=paths,
        transport_factory=lambda _session: transport,
    )
    coordinator.submit(profile, execution.execution_id)
    assert transport.sent.wait(1)
    transport.events.put(
        _event(
            "iopub",
            "status",
            "kernel-message-1",
            {"execution_state": "busy"},
        )
    )
    deadline = time.monotonic() + 1
    while (
        store.get_execution(execution.execution_id).state
        is not ExecutionState.RUNNING
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)

    pending = coordinator.cancel(profile, execution.execution_id)

    assert pending.state is ExecutionState.RUNNING
    assert pending.cancel_requested is True
    deadline = time.monotonic() + 1
    while transport.interrupt_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert transport.interrupt_calls == 1
    assert store.get_execution(execution.execution_id).state is ExecutionState.RUNNING

    transport.events.put(
        _event(
            "shell",
            "execute_reply",
            "kernel-message-1",
            {
                "status": "error",
                "ename": "KeyboardInterrupt",
                "evalue": "",
                "traceback": [],
            },
        )
    )
    transport.events.put(
        _event(
            "iopub",
            "status",
            "kernel-message-1",
            {"execution_state": "idle"},
        )
    )
    coordinator.wait_idle(timeout=2)

    record = store.get_execution(execution.execution_id)
    coordinator.close()
    store.close()
    assert record.state is ExecutionState.INTERRUPTED


def test_execution_deadline_starts_only_after_matching_inbound_message(
    paths, profile
):
    store = _store(paths, profile)
    execution = _create(
        store,
        "00000000-0000-4000-8000-000000000210",
        timeout=0.05,
        source=b"while True: pass",
    )
    transport = _ScriptedTransport(
        store=store,
        profile=profile,
        events=[],
        block=True,
    )
    transport.execution_id = execution.execution_id
    coordinator = ExecutionCoordinator(
        paths=paths,
        transport_factory=lambda _session: transport,
    )
    coordinator.submit(profile, execution.execution_id)
    assert transport.sent.wait(1)
    time.sleep(0.08)

    assert transport.interrupt_calls == 0
    assert store.get_execution(execution.execution_id).execution_deadline is None

    transport.events.put(
        _event(
            "iopub",
            "status",
            "kernel-message-1",
            {"execution_state": "busy"},
        )
    )
    deadline = time.monotonic() + 1
    while transport.interrupt_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert transport.interrupt_calls == 1
    assert store.get_execution(execution.execution_id).execution_deadline

    transport.events.put(
        _event(
            "shell",
            "execute_reply",
            "kernel-message-1",
            {
                "status": "error",
                "ename": "KeyboardInterrupt",
                "evalue": "",
                "traceback": [],
            },
        )
    )
    transport.events.put(
        _event(
            "iopub",
            "status",
            "kernel-message-1",
            {"execution_state": "idle"},
        )
    )
    coordinator.wait_idle(timeout=2)

    record = store.get_execution(execution.execution_id)
    coordinator.close()
    store.close()
    assert record.state is ExecutionState.TIMED_OUT


def test_missing_session_is_rejected_before_source_is_queued(paths, profile):
    with DurableStore(paths=paths, profile=profile) as store:
        with pytest.raises(BetterColabError) as missing:
            _create(
                store,
                "00000000-0000-4000-8000-000000000211",
            )

        assert missing.value.error.code == "SESSION_NOT_FOUND"
        assert list(Path(paths.sources_dir).iterdir()) == []
