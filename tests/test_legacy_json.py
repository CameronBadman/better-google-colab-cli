import json
from unittest.mock import MagicMock

from typer.testing import CliRunner

from better_colab.cli import app as better_app
from better_colab.errors import ExitCode
from colab_cli.cli import app as compatibility_app
from colab_cli.client import PostAssignmentResponse


runner = CliRunner()


def _payload(result) -> dict:
    assert len(result.stdout.splitlines()) == 1, result.stdout
    return json.loads(result.stdout)


def test_compat_sessions_supports_compact_json(mock_common_state):
    assignment = MagicMock()
    assignment.endpoint = "endpoint-1"
    assignment.accelerator.value = "T4"
    assignment.variant.name = "GPU"
    local = MagicMock(name="local")
    local.name = "training"
    local.endpoint = "endpoint-1"
    mock_common_state.sync_sessions.return_value = (
        {"training": local},
        [assignment],
    )

    result = runner.invoke(compatibility_app, ["sessions", "--format", "json"])
    payload = _payload(result)
    assert result.exit_code == ExitCode.OK
    assert payload == {
        "schema_version": 1,
        "ok": True,
        "result": {
            "sessions": [
                {
                    "name": "training",
                    "endpoint": "endpoint-1",
                    "hardware": "T4",
                    "variant": "GPU",
                }
            ]
        },
    }


def test_compat_status_json_uses_not_found_contract(mock_common_state):
    mock_common_state.sync_sessions.return_value = ({}, [])
    mock_common_state.store.get.return_value = None

    result = runner.invoke(
        compatibility_app,
        ["status", "--session", "missing", "--format=json"],
    )

    payload = _payload(result)
    assert result.exit_code == ExitCode.NOT_FOUND
    assert payload["error"]["code"] == "SESSION_NOT_FOUND"
    assert payload["error"]["retryable"] is False


def test_compat_new_json_never_prints_progress_or_tokens(
    mocker,
    mock_common_state,
):
    response = MagicMock()
    response.__class__ = PostAssignmentResponse
    response.runtime_proxy_info.token = "secret-token"
    response.runtime_proxy_info.url = "https://runtime"
    response.endpoint = "endpoint-1"
    mock_common_state.client.assign.return_value = response
    mocker.patch(
        "colab_cli.commands.session.spawn_keep_alive",
        return_value=4321,
    )

    result = runner.invoke(
        compatibility_app,
        ["new", "--session", "training", "--gpu", "T4", "--format", "json"],
    )

    payload = _payload(result)
    assert result.exit_code == ExitCode.OK
    assert payload["result"] == {
        "name": "training",
        "endpoint": "endpoint-1",
        "hardware": "T4",
        "variant": "GPU",
        "status": "ready",
    }
    assert "secret-token" not in result.stdout
    assert "Creating session" not in result.stdout


def test_flat_ls_json_returns_sorted_structured_items(
    mocker, mock_common_state
):
    mock_common_state.store.get.return_value = MagicMock()
    contents = mocker.patch("colab_cli.commands.files.ContentsClient").return_value
    contents.list_dir.return_value = {
        "type": "directory",
        "content": [
            {"name": "z.py", "type": "file", "size": 12},
            {"name": "data", "type": "directory"},
        ],
    }

    result = runner.invoke(
        better_app,
        ["ls", "--session", "training", "content", "--format=json"],
    )

    assert result.exit_code == ExitCode.OK
    assert _payload(result)["result"] == {
        "session": "training",
        "path": "content",
        "type": "directory",
        "items": [
            {"name": "data", "type": "directory"},
            {"name": "z.py", "type": "file", "size": 12},
        ],
    }


def test_flat_upload_json_maps_missing_local_file_to_not_found(mock_common_state):
    mock_common_state.store.get.return_value = MagicMock()

    result = runner.invoke(
        better_app,
        [
            "upload",
            "--session",
            "training",
            "/definitely/missing.py",
            "content/missing.py",
            "--format=json",
        ],
    )

    assert result.exit_code == ExitCode.NOT_FOUND
    assert _payload(result)["error"]["code"] == "LOCAL_FILE_NOT_FOUND"


def test_flat_install_json_suppresses_kernel_terminal_output(
    mocker, mock_common_state
):
    mock_common_state.resolve_session.return_value = "training"
    run_automation = mocker.patch(
        "colab_cli.commands.automation.run_automation",
        return_value=[{"text": "noisy installer output"}],
    )

    result = runner.invoke(
        better_app,
        ["install", "--session", "training", "numpy", "--format", "json"],
    )

    assert result.exit_code == ExitCode.OK
    assert _payload(result)["result"] == {
        "session": "training",
        "packages": ["numpy"],
        "installed": True,
    }
    assert "noisy installer output" not in result.stdout
    assert run_automation.call_args.kwargs["emit_output"] is False
