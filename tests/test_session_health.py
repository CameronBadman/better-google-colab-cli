import queue
import uuid

import pytest

from better_colab.execution import ExecutionCoordinator
from better_colab.kernel_transport import KernelEvent, PreparedExecution, ReadinessProof
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


def test_readiness_proof_requires_matching_nonce_reply_and_idle():
    nonce = "nonce-123"
    proof = ReadinessProof("probe-message", nonce)
    proof.observe(
        _event(
            "iopub",
            "status",
            "probe-message",
            {"execution_state": "idle"},
        )
    )
    assert proof.ready is False
    proof.observe(
        _event(
            "shell",
            "execute_reply",
            "probe-message",
            {
                "status": "ok",
                "user_expressions": {
                    "better_colab_nonce": {
                        "status": "ok",
                        "data": {"text/plain": repr(nonce)},
                    }
                },
            },
        )
    )
    assert proof.ready is True
    assert proof.error is None

    wrong = ReadinessProof("wrong-message", nonce)
    wrong.observe(
        _event(
            "shell",
            "execute_reply",
            "wrong-message",
            {
                "status": "ok",
                "user_expressions": {
                    "better_colab_nonce": {
                        "status": "ok",
                        "data": {"text/plain": repr("different")},
                    }
                },
            },
        )
    )
    assert wrong.ready is False
    assert wrong.error == "NONCE_MISMATCH"


class _ProbeTransport:
    kernel_id = "kernel-live"
    jupyter_session_id = "jupyter-live"

    def __init__(self):
        self.events = queue.Queue()
        self.prepared_probe = None
        self.closed = False

    def prepare_readiness_probe(self, nonce):
        self.prepared_probe = PreparedExecution(
            message_id="readiness-message",
            message={
                "header": {
                    "msg_id": "readiness-message",
                    "msg_type": "execute_request",
                },
                "content": {
                    "code": "None",
                    "store_history": False,
                    "user_expressions": {"better_colab_nonce": repr(nonce)},
                },
            },
        )
        return self.prepared_probe

    def send(self, prepared):
        rendered = prepared.message["content"]["user_expressions"][
            "better_colab_nonce"
        ]
        self.events.put(
            _event(
                "shell",
                "execute_reply",
                prepared.message_id,
                {
                    "status": "ok",
                    "user_expressions": {
                        "better_colab_nonce": {
                            "status": "ok",
                            "data": {"text/plain": rendered},
                        }
                    },
                },
            )
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

    def close(self):
        self.closed = True


def test_nonce_probe_is_serialized_cached_and_invalidated_on_close(
    paths,
    profile,
    store,
):
    transport = _ProbeTransport()
    coordinator = ExecutionCoordinator(
        paths=paths,
        transport_factory=lambda _session: transport,
    )

    result = coordinator.probe_session(profile, "training", timeout=1)

    assert result.controller_alive is True
    assert result.backend_alive is True
    assert result.kernel_connected is True
    assert result.kernel_execution_ready is True
    assert result.kernel_probe_at
    assert result.kernel_probe_latency_ms >= 0
    assert result.kernel_probe_error is None
    assert transport.prepared_probe.message["content"]["store_history"] is False
    connection = store.get_kernel_connection("training")
    assert connection.readiness_checked_at == result.kernel_probe_at
    assert connection.readiness_error is None

    coordinator.close()

    invalidated = store.get_kernel_connection("training")
    assert transport.closed is True
    assert invalidated.disconnected_at is not None
    assert invalidated.readiness_nonce is None
    assert invalidated.readiness_checked_at is None
