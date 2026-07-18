import json

from typer.testing import CliRunner

from better_colab import (
    SessionHealthResult,
    SessionListResult,
    SessionStopResult,
    SessionSummary,
)
from better_colab.cli import app


runner = CliRunner()
SUMMARY = SessionSummary(
    name="training",
    endpoint="endpoint",
    hardware="CPU",
    variant="DEFAULT",
    status="ready",
)


def test_better_flat_new_and_sessions_use_typed_session_api(mocker):
    ensure = mocker.patch(
        "better_colab.durable_commands.BetterColabClient.ensure_session",
        return_value=SUMMARY,
    )
    listed = mocker.patch(
        "better_colab.durable_commands.BetterColabClient.list_sessions",
        return_value=SessionListResult(sessions=[SUMMARY]),
    )

    created = runner.invoke(
        app,
        ["new", "-s", "training", "--format=json"],
    )
    sessions = runner.invoke(app, ["sessions", "--format=json"])

    assert created.exit_code == 0
    assert sessions.exit_code == 0
    assert json.loads(created.stdout)["result"] == SUMMARY.model_dump(
        exclude_none=True
    )
    assert json.loads(sessions.stdout)["result"]["sessions"][0]["name"] == (
        "training"
    )
    ensure.assert_called_once_with("training", gpu=None, tpu=None)
    listed.assert_called_once_with()


def test_better_flat_status_and_stop_use_typed_session_api(mocker):
    health = SessionHealthResult(
        name="training",
        endpoint="endpoint",
        hardware="CPU",
        variant="DEFAULT",
        controller_alive=True,
        backend_alive=True,
        kernel_connected=True,
        kernel_execution_ready=True,
        kernel_probe_at="2026-07-18T00:00:00Z",
        kernel_probe_latency_ms=12.5,
        kernel_probe_error=None,
    )
    status = mocker.patch(
        "better_colab.durable_commands.BetterColabClient.session_status",
        return_value=health,
    )
    stop = mocker.patch(
        "better_colab.durable_commands.BetterColabClient.stop_session",
        return_value=SessionStopResult(name="training", stopped=True),
    )

    observed = runner.invoke(
        app,
        ["status", "-s", "training", "--format=json"],
    )
    stopped = runner.invoke(
        app,
        ["stop", "-s", "training", "--format=json"],
    )

    assert observed.exit_code == 0
    assert stopped.exit_code == 0
    assert json.loads(observed.stdout)["result"]["kernel_execution_ready"] is True
    assert json.loads(stopped.stdout)["result"] == {
        "name": "training",
        "stopped": True,
    }
    status.assert_called_once_with("training")
    stop.assert_called_once_with("training")
