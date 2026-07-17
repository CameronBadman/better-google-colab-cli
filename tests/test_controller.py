import os
import socket
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import filelock
import pytest

from better_colab import BetterColabError, ExecutionState
from better_colab.controller import ControllerServer
from better_colab.controller_client import ControllerClient
from better_colab.controller_protocol import encode_frame, recv_frame
from better_colab.storage import DurableStore, ProfileSpec, StatePaths


@pytest.fixture
def controller_paths(tmp_path):
    # Unix-domain socket paths are short and platform-limited. Keep the socket
    # directly below pytest's temporary root rather than another deep tree.
    return StatePaths(
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "run",
    )


@pytest.fixture
def controller_client(controller_paths):
    client = ControllerClient(paths=controller_paths, startup_timeout=5)
    yield client
    try:
        client.stop(force=True)
    except BetterColabError:
        pass


def _profile(tmp_path, provider: str) -> ProfileSpec:
    return ProfileSpec.from_values(
        config_path=tmp_path / f"{provider}-sessions.json",
        auth_provider=provider,
        oauth_config_path=tmp_path / "oauth.json",
    )


def test_controller_autostart_handshake_and_private_files(
    controller_client, controller_paths
):
    status = controller_client.ensure_running()

    assert status["controller_alive"] is True
    assert status["pid"] > 0
    assert status["protocol_version"] == 1
    assert stat.S_IMODE(controller_paths.runtime_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(controller_paths.socket.stat().st_mode) == 0o600
    assert stat.S_IMODE(controller_paths.pid_file.stat().st_mode) == 0o600


def test_concurrent_clients_elect_exactly_one_controller(
    controller_paths, monkeypatch
):
    spawn_count = 0
    count_lock = threading.Lock()
    original_spawn = ControllerClient._spawn_controller

    def counted_spawn(self):
        nonlocal spawn_count
        with count_lock:
            spawn_count += 1
        return original_spawn(self)

    monkeypatch.setattr(ControllerClient, "_spawn_controller", counted_spawn)
    clients = [
        ControllerClient(paths=controller_paths, startup_timeout=5)
        for _ in range(12)
    ]

    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        statuses = list(pool.map(lambda client: client.ensure_running(), clients))

    try:
        assert len({status["pid"] for status in statuses}) == 1
        assert spawn_count == 1
    finally:
        clients[0].stop(force=True)


def test_stale_socket_is_removed_only_when_lifetime_lock_is_free(
    controller_paths,
):
    controller_paths.ensure()
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(controller_paths.socket))
    stale.close()
    assert controller_paths.socket.exists()

    client = ControllerClient(paths=controller_paths, startup_timeout=5)
    status = client.ensure_running()
    try:
        assert status["controller_alive"] is True
        assert stat.S_ISSOCK(controller_paths.socket.stat().st_mode)
    finally:
        client.stop(force=True)


def test_client_does_not_remove_socket_while_lifetime_lock_is_held(
    controller_paths,
):
    controller_paths.ensure()
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(controller_paths.socket))
    stale.close()
    lifetime = filelock.FileLock(str(controller_paths.lifetime_lock))
    lifetime.acquire()
    try:
        client = ControllerClient(paths=controller_paths, startup_timeout=0.15)
        with pytest.raises(BetterColabError) as unavailable:
            client.ensure_running()
        assert unavailable.value.error.code == "CONTROLLER_START_TIMEOUT"
        assert controller_paths.socket.exists()
    finally:
        lifetime.release()
        controller_paths.socket.unlink(missing_ok=True)


def test_protocol_version_mismatch_is_rejected_without_replacement(
    controller_client, controller_paths
):
    status = controller_client.ensure_running()
    original_pid = status["pid"]
    request = {
        "protocol_version": 999,
        "request_id": "bad-version",
        "method": "hello",
        "params": {},
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(str(controller_paths.socket))
        connection.sendall(encode_frame(request))
        response = recv_frame(connection)

    assert response["ok"] is False
    assert response["error"]["code"] == "PROTOCOL_VERSION_MISMATCH"
    assert controller_client.status()["pid"] == original_pid


def test_condition_wait_is_unblocked_by_server_notification(controller_client):
    controller_client.ensure_running()
    result = {}

    def wait():
        result.update(
            controller_client.wait_condition(
                topic="execution:test",
                after_revision=0,
                timeout=2,
            )
        )

    thread = threading.Thread(target=wait)
    thread.start()
    time.sleep(0.05)
    notified = controller_client.notify_condition(
        topic="execution:test",
        payload={"state": "finished"},
    )
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert notified["revision"] == 1
    assert result == {
        "revision": 1,
        "payload": {"state": "finished"},
        "wait_timed_out": False,
    }


def test_profiles_are_isolated_behind_one_controller(
    controller_client, controller_paths, tmp_path
):
    oauth = _profile(tmp_path, "oauth2")
    adc = _profile(tmp_path, "adc")
    with DurableStore(paths=controller_paths, profile=oauth) as oauth_store:
        oauth_store.upsert_session(
            name="shared",
            endpoint="oauth-endpoint",
            backend_url="https://oauth.example",
            runtime_token="oauth-secret",
            hardware="CPU",
        )
    with DurableStore(paths=controller_paths, profile=adc) as adc_store:
        adc_store.upsert_session(
            name="shared",
            endpoint="adc-endpoint",
            backend_url="https://adc.example",
            runtime_token="adc-secret",
            hardware="T4",
        )

    controller_client.ensure_running()
    oauth_sessions = controller_client.list_profile_sessions(oauth)
    adc_sessions = controller_client.list_profile_sessions(adc)

    assert oauth_sessions == [
        {
            "name": "shared",
            "endpoint": "oauth-endpoint",
            "hardware": "CPU",
            "variant": "DEFAULT",
        }
    ]
    assert adc_sessions == [
        {
            "name": "shared",
            "endpoint": "adc-endpoint",
            "hardware": "T4",
            "variant": "DEFAULT",
        }
    ]
    assert "secret" not in repr(oauth_sessions + adc_sessions)


def test_controller_status_survives_external_wal_writers(
    controller_client, controller_paths, tmp_path
):
    """Foreground typed operations must not stale the controller's DB view."""
    controller_client.ensure_running()
    profile = _profile(tmp_path, "oauth2")

    for index in range(100):
        with DurableStore(paths=controller_paths, profile=profile) as store:
            store.upsert_session(
                name="shared",
                endpoint=f"endpoint-{index}",
                backend_url="https://runtime.example",
                runtime_token="secret",
                hardware="CPU",
            )
        status = controller_client.status()
        assert status["controller_alive"] is True


def test_controller_scopes_metadata_connections_to_each_operation(
    controller_paths,
):
    async def scenario():
        server = ControllerServer(paths=controller_paths)
        await server.start()
        try:
            assert getattr(server, "_metadata_store", None) is None
            assert server._status()["controller_alive"] is True
        finally:
            await server.close()

    import asyncio

    asyncio.run(scenario())


def test_normal_stop_refuses_active_work_and_force_journals_unknown(
    controller_client, controller_paths, tmp_path
):
    profile = _profile(tmp_path, "oauth2")
    store = DurableStore(paths=controller_paths, profile=profile)
    store.upsert_session(
        name="training",
        endpoint="endpoint",
        backend_url="https://runtime.example",
        runtime_token="secret",
        hardware="CPU",
    )
    execution = store.create_execution(
        execution_id="00000000-0000-4000-8000-000000000100",
        session_name="training",
        source=b"while True: pass",
        provenance={"kind": "stdin"},
        request={"session": "training", "source": "loop"},
    )
    store.transition_execution(execution.execution_id, ExecutionState.DISPATCHING)
    store.confirm_dispatch(execution.execution_id)
    controller_client.ensure_running()

    with pytest.raises(BetterColabError) as busy:
        controller_client.stop(force=False)
    assert busy.value.error.code == "CONTROLLER_BUSY"
    assert controller_client.status()["controller_alive"] is True

    stopped = controller_client.stop(force=True)

    assert stopped["stopping"] is True
    controller_client.wait_until_stopped(timeout=3)
    recovered = store.get_execution(execution.execution_id)
    transitions = store.list_transitions(execution.execution_id)
    store.close()
    assert recovered.state is ExecutionState.UNKNOWN
    assert recovered.output_complete is False
    assert [item.to_state for item in transitions[-2:]] == [
        ExecutionState.DISCONNECTED,
        ExecutionState.UNKNOWN,
    ]


def test_wait_timeout_is_a_single_condition_response(controller_client):
    controller_client.ensure_running()

    result = controller_client.wait_condition(
        topic="execution:never",
        after_revision=0,
        timeout=0.05,
    )

    assert result == {
        "revision": 0,
        "wait_timed_out": True,
    }


def test_controller_stays_alive_until_explicit_stop(controller_client):
    first = controller_client.ensure_running()
    time.sleep(0.05)
    second = controller_client.status()

    assert second["pid"] == first["pid"]
    assert os.kill(first["pid"], 0) is None
