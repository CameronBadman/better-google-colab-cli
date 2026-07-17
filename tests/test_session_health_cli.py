import json

from typer.testing import CliRunner

from better_colab.cli import app
from better_colab.models import SessionHealthResult


runner = CliRunner()


def test_session_probe_cli_keeps_all_health_fields(mocker):
    mocker.patch(
        "better_colab.durable_commands.BetterColabClient.session_probe",
        return_value=SessionHealthResult(
            name="training",
            endpoint="endpoint",
            hardware="CPU",
            variant="DEFAULT",
            controller_alive=True,
            backend_alive=True,
            kernel_connected=True,
            kernel_execution_ready=True,
            kernel_probe_at="2026-07-17T00:00:00Z",
            kernel_probe_latency_ms=12.5,
            kernel_probe_error=None,
        ),
    )

    result = runner.invoke(
        app,
        ["session", "probe", "training", "--format=json"],
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["ok"] is True
    assert {
        "controller_alive",
        "backend_alive",
        "kernel_connected",
        "kernel_execution_ready",
        "kernel_probe_at",
        "kernel_probe_latency_ms",
        "kernel_probe_error",
    } <= payload["result"].keys()
