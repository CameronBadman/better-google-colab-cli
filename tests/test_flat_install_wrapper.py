import base64
import json
from types import SimpleNamespace

from typer.testing import CliRunner

from better_colab import (
    ExecutionState,
    ExecutionWaitResult,
    OutputEvent,
    OutputPage,
)
from better_colab.cli import app


runner = CliRunner()
EXECUTION_ID = "00000000-0000-4000-8000-000000000c01"


def _wait(
    state: ExecutionState = ExecutionState.FINISHED,
    *,
    text: str = "Installation Complete (via uv)!\n",
) -> ExecutionWaitResult:
    return ExecutionWaitResult(
        execution_id=EXECUTION_ID,
        session="training",
        state=state,
        source_sha256="a" * 64,
        output_complete=True,
        dispatch_confirmed=True,
        reply_received=True,
        idle_received=True,
        error_name="CalledProcessError" if state is ExecutionState.ERROR else None,
        error_value="pip failed" if state is ExecutionState.ERROR else None,
        created_at="2026-07-18T00:00:00Z",
        updated_at="2026-07-18T00:00:01Z",
        output=OutputPage(
            execution_id=EXECUTION_ID,
            events=[
                OutputEvent(
                    cursor="cursor",
                    event_type="stream",
                    stream="stdout",
                    text=text,
                )
            ],
            output_complete=True,
        ),
    )


def _client_context(mocker, mock_common_state):
    # Keep the initial TDD failure on the legacy path fully mocked.
    mock_common_state.resolve_session.return_value = "training"
    mock_common_state.store.get.return_value = SimpleNamespace(name="training")
    mocker.patch(
        "colab_cli.commands.automation.run_automation",
        return_value=[],
    )
    context = mocker.MagicMock()
    client = context.__enter__.return_value
    mocker.patch(
        "colab_cli.commands.automation._client_from_cli_state",
        return_value=context,
        create=True,
    )
    client.start_execution.return_value = _wait()
    return client


def test_better_flat_install_executes_packages_durably(
    mocker,
    mock_common_state,
):
    client = _client_context(mocker, mock_common_state)

    result = runner.invoke(
        app,
        ["install", "-s", "training", "pandas", "odd'package"],
    )

    assert result.exit_code == 0
    assert result.stdout == "Installation Complete (via uv)!\n"
    call = client.start_execution.call_args.kwargs
    assert call["session"] == "training"
    assert call["provenance"] == {"kind": "install"}
    assert repr(["pandas", "odd'package"]) in call["source"]
    compile(call["source"], "<durable-install>", "exec")
    assert call["execution_timeout"] is None


def test_better_flat_install_embeds_requirements_without_contents_api(
    mocker,
    mock_common_state,
    tmp_path,
):
    requirement = tmp_path / "requirements.txt"
    raw = b"requests==2.32.0\n--extra-index-url https://example.invalid/simple\n"
    requirement.write_bytes(raw)
    client = _client_context(mocker, mock_common_state)
    contents = mocker.patch("colab_cli.commands.automation.ContentsClient")

    result = runner.invoke(
        app,
        [
            "install",
            "-s",
            "training",
            "-r",
            str(requirement),
            "--format=json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["result"] == {
        "session": "training",
        "packages": [],
        "requirement": str(requirement),
        "installed": True,
    }
    assert "Installation Complete" not in result.stdout
    source = client.start_execution.call_args.kwargs["source"]
    assert base64.b64encode(raw).decode("ascii") in source
    compile(source, "<durable-install>", "exec")
    assert str(requirement.resolve()) not in source
    assert client.start_execution.call_args.kwargs["provenance"] == {
        "kind": "install",
        "path": str(requirement.resolve()),
    }
    contents.assert_not_called()


def test_better_flat_install_json_maps_execution_error(mocker, mock_common_state):
    client = _client_context(mocker, mock_common_state)
    client.start_execution.return_value = _wait(
        ExecutionState.ERROR,
        text="resolver output\n",
    )

    result = runner.invoke(
        app,
        ["install", "-s", "training", "missing-package", "--format=json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INSTALL_FAILED"
    assert "resolver output" not in result.stdout
