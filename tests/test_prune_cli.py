import json
from datetime import datetime, timezone

from typer.testing import CliRunner

from better_colab.cli import app
from better_colab.errors import ExitCode


runner = CliRunner()


def test_execution_prune_defaults_to_dry_run_json(monkeypatch, mocker, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    mocker.patch("colab_cli.cli.setup_logging")

    result = runner.invoke(
        app,
        [
            "execution",
            "prune",
            "--before",
            datetime(2099, 1, 1, tzinfo=timezone.utc).isoformat(),
            "--format=json",
        ],
    )

    assert result.exit_code == ExitCode.OK
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "ok": True,
        "result": {
            "dry_run": True,
            "matched": 0,
            "deleted": 0,
            "execution_ids": [],
            "artifact_bytes": 0,
        },
    }


def test_execution_prune_rejects_conflicting_confirmation_flags(
    monkeypatch, mocker, tmp_path
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    mocker.patch("colab_cli.cli.setup_logging")

    result = runner.invoke(
        app,
        [
            "execution",
            "prune",
            "--before",
            "2099-01-01T00:00:00Z",
            "--dry-run",
            "--confirm",
            "--format=json",
        ],
    )

    assert result.exit_code == ExitCode.USAGE
    assert json.loads(result.stdout)["error"]["code"] == "CONFLICTING_FLAGS"
