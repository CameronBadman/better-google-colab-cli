import contextlib
from unittest.mock import MagicMock

from typer.testing import CliRunner

from colab_cli.cli import app


runner = CliRunner()


def _session():
    session = MagicMock()
    session.name = "training"
    session.url = "https://runtime.example"
    session.token = "secret"
    session.endpoint = "endpoint"
    session.kernel_id = None
    session.session_id = None
    session.running = None
    return session


def test_tty_repl_and_console_hold_exclusive_session_lease(
    mocker,
    mock_common_state,
):
    session = _session()
    mock_common_state.resolve_session.return_value = "training"
    mock_common_state.store.get.return_value = session
    lease = mocker.patch(
        "colab_cli.commands.execution.compatibility_session_lease",
        side_effect=lambda *_args, **_kwargs: contextlib.nullcontext(),
    )
    mocker.patch(
        "colab_cli.commands.execution.is_stdin_tty",
        return_value=True,
    )
    mocker.patch("colab_cli.repl.ColabREPL")
    mocker.patch("colab_cli.commands.execution.connect_console")

    repl = runner.invoke(app, ["repl", "-s", "training"])
    console = runner.invoke(app, ["console", "-s", "training"])

    assert repl.exit_code == 0
    assert console.exit_code == 0
    assert lease.call_args_list == [
        mocker.call("training"),
        mocker.call("training"),
    ]


def test_vm_auth_and_drive_mount_hold_exclusive_session_lease(
    mocker,
    mock_common_state,
):
    mock_common_state.resolve_session.return_value = "training"
    lease = mocker.patch(
        "colab_cli.commands.automation.compatibility_session_lease",
        side_effect=lambda *_args, **_kwargs: contextlib.nullcontext(),
    )
    automation = mocker.patch("colab_cli.commands.automation.run_automation")

    auth = runner.invoke(app, ["auth", "-s", "training"])
    drive = runner.invoke(
        app,
        ["drivemount", "-s", "training", "/content/drive"],
    )

    assert auth.exit_code == 0
    assert drive.exit_code == 0
    assert lease.call_args_list == [
        mocker.call("training"),
        mocker.call("training"),
    ]
    assert automation.call_count == 2
