import json

from typer.testing import CliRunner

from better_colab.cli import app
from better_colab.errors import ExitCode


runner = CliRunner()


def _payload(result):
    assert len(result.stdout.splitlines()) == 1, result.stdout
    return json.loads(result.stdout)


def test_controller_cli_lifecycle_is_explicit_and_json(
    monkeypatch, mocker, tmp_path
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    setup_logging = mocker.patch("colab_cli.cli.setup_logging")

    absent = runner.invoke(app, ["controller", "status", "--format=json"])
    started = runner.invoke(app, ["controller", "start", "--format=json"])
    observed = runner.invoke(app, ["controller", "status", "--format=json"])
    stopped = runner.invoke(app, ["controller", "stop", "--format=json"])
    final = runner.invoke(app, ["controller", "status", "--format=json"])

    assert absent.exit_code == ExitCode.OK
    assert _payload(absent)["result"] == {"controller_alive": False}
    started_result = _payload(started)["result"]
    assert started_result["controller_alive"] is True
    assert started_result["pid"] > 0
    assert _payload(observed)["result"]["pid"] == started_result["pid"]
    assert _payload(stopped)["result"] == {
        "stopping": True,
        "controller_alive": False,
    }
    assert _payload(final)["result"] == {"controller_alive": False}
    setup_logging.assert_not_called()


def test_controller_stop_when_absent_is_idempotent_json(
    monkeypatch, mocker, tmp_path
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    mocker.patch("colab_cli.cli.setup_logging")

    result = runner.invoke(app, ["controller", "stop", "--format=json"])

    assert result.exit_code == ExitCode.OK
    assert _payload(result)["result"] == {
        "stopping": False,
        "controller_alive": False,
    }
