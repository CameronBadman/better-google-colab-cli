import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from better_colab import (
    BetterColabClient,
    BetterColabError,
    SessionListResult,
    SessionStopResult,
    SessionSummary,
)
from better_colab.cli import app
from better_colab.storage import DurableStore, StatePaths


runner = CliRunner()


@pytest.fixture
def client(tmp_path):
    return BetterColabClient(
        config_path=tmp_path / "sessions.json",
        auth_provider="oauth2",
        oauth_config_path=tmp_path / "oauth.json",
        paths=StatePaths(
            state_dir=tmp_path / "state",
            runtime_dir=tmp_path / "run",
        ),
    )


def _assignment():
    return SimpleNamespace(
        endpoint="endpoint",
        runtime_proxy_info=SimpleNamespace(
            url="https://runtime.example",
            token="secret",
        ),
    )


def test_typed_session_ensure_allocates_only_when_missing(mocker, client):
    control = mocker.patch.object(client, "_control_client").return_value
    control.assign.return_value = _assignment()
    spawn = mocker.patch.object(client, "_spawn_keep_alive", return_value=321)

    created = client.ensure_session("training", gpu="T4")
    existing = client.ensure_session("training", gpu="T4")

    assert created == SessionSummary(
        name="training",
        endpoint="endpoint",
        hardware="T4",
        variant="GPU",
        status="ready",
    )
    assert existing == created
    control.assign.assert_called_once()
    control.keep_alive_assignment.assert_called_once_with("endpoint")
    spawn.assert_called_once_with("training", "endpoint")
    with DurableStore(paths=client.paths, profile=client.profile) as store:
        stored = store.get_session("training")
        assert stored is not None
        assert stored.keep_alive_pid == 321
        assert stored.runtime_token == "secret"


def test_typed_session_ensure_rejects_conflicting_hardware(client):
    with pytest.raises(BetterColabError) as conflict:
        client.ensure_session("training", gpu="T4", tpu="v5e1")

    assert conflict.value.error.code == "CONFLICTING_HARDWARE"
    assert conflict.value.exit_code == 2


def test_typed_session_list_and_stop_use_durable_profile(mocker, client):
    with DurableStore(paths=client.paths, profile=client.profile) as store:
        store.upsert_session(
            name="training",
            endpoint="endpoint",
            backend_url="https://runtime.example",
            runtime_token="secret",
            hardware="NONE",
        )
        store.update_session_keep_alive_pid("training", 987)
    terminate = mocker.patch.object(client, "_terminate_keep_alive")
    control = mocker.patch.object(client, "_control_client").return_value

    listed = client.list_sessions()
    stopped = client.stop_session("training")

    assert listed == SessionListResult(
        sessions=[
            SessionSummary(
                name="training",
                endpoint="endpoint",
                hardware="CPU",
                variant="DEFAULT",
            )
        ]
    )
    assert stopped == SessionStopResult(name="training", stopped=True)
    terminate.assert_called_once_with(987)
    control.unassign.assert_called_once_with("endpoint")
    with DurableStore(paths=client.paths, profile=client.profile) as store:
        assert store.get_session("training") is None


def test_session_commands_share_typed_json_models(mocker):
    ensure = mocker.patch(
        "better_colab.durable_commands.BetterColabClient.ensure_session",
        return_value=SessionSummary(
            name="training",
            endpoint="endpoint",
            hardware="CPU",
            variant="DEFAULT",
            status="ready",
        ),
    )
    mocker.patch(
        "better_colab.durable_commands.BetterColabClient.list_sessions",
        return_value=SessionListResult(sessions=[]),
    )
    mocker.patch(
        "better_colab.durable_commands.BetterColabClient.stop_session",
        return_value=SessionStopResult(name="training", stopped=True),
    )

    created = runner.invoke(
        app,
        ["session", "ensure", "training", "--format=json"],
    )
    listed = runner.invoke(app, ["session", "list", "--format=json"])
    stopped = runner.invoke(
        app,
        ["session", "stop", "training", "--format=json"],
    )

    assert created.exit_code == 0
    assert listed.exit_code == 0
    assert stopped.exit_code == 0
    assert json.loads(created.stdout)["result"]["status"] == "ready"
    assert json.loads(listed.stdout)["result"] == {"sessions": []}
    assert json.loads(stopped.stdout)["result"]["stopped"] is True
    ensure.assert_called_once_with("training", gpu=None, tpu=None)
