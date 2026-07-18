import nbformat
from typer.testing import CliRunner

from better_colab import (
    BatchState,
    BatchWaitResult,
    ExecutionResult,
    ExecutionState,
    ExecutionWaitResult,
    NotebookWriteResult,
    OutputEvent,
    OutputPage,
)
from better_colab.cli import app


runner = CliRunner()
EXECUTION_ID = "00000000-0000-4000-8000-000000000a01"
BATCH_ID = "10000000-0000-4000-8000-000000000a01"


def _execution(state=ExecutionState.FINISHED):
    return ExecutionResult(
        execution_id=EXECUTION_ID,
        session="training",
        state=state,
        source_sha256="a" * 64,
        output_complete=True,
        dispatch_confirmed=True,
        reply_received=True,
        idle_received=True,
        error_name="ValueError" if state is ExecutionState.ERROR else None,
        error_value="bad" if state is ExecutionState.ERROR else None,
        created_at="2026-07-18T00:00:00Z",
        updated_at="2026-07-18T00:00:01Z",
    )


def _wait(state=ExecutionState.FINISHED, text="hello\n"):
    return ExecutionWaitResult(
        **_execution(state).model_dump(),
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


def _client_context(mocker):
    context = mocker.MagicMock()
    client = context.__enter__.return_value
    mocker.patch(
        "colab_cli.commands.execution._client_from_cli_state",
        return_value=context,
    )
    return client


def test_better_flat_exec_routes_through_durable_controller(mocker):
    client = _client_context(mocker)
    client.start_execution.return_value = _wait()

    result = runner.invoke(
        app,
        ["exec", "-s", "training"],
        input="print('hello')\n",
    )

    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    client.start_execution.assert_called_once_with(
        session="training",
        source="print('hello')\n",
        provenance={"kind": "stdin"},
        execution_timeout=30.0,
    )


def test_better_flat_exec_returns_nonzero_for_user_code_error(mocker):
    client = _client_context(mocker)
    client.start_execution.return_value = _wait(
        ExecutionState.ERROR,
        text="before\n",
    )

    result = runner.invoke(
        app,
        ["exec", "-s", "training"],
        input="print('before'); raise ValueError('bad')\n",
    )

    assert result.exit_code == 1
    assert result.stdout == "before\n"
    assert "ValueError: bad" in result.stderr


def test_better_piped_repl_uses_same_durable_execution_path(mocker):
    client = _client_context(mocker)
    client.start_execution.return_value = _wait(text="piped\n")

    result = runner.invoke(
        app,
        ["repl", "-s", "training"],
        input="print('piped')\n",
    )

    assert result.exit_code == 0
    assert result.stdout == "piped\n"
    client.start_execution.assert_called_once()
    assert client.start_execution.call_args.kwargs["provenance"] == {
        "kind": "stdin",
        "compatibility_command": "repl",
    }


def test_notebook_exec_writes_only_explicit_output_copy(mocker, tmp_path):
    notebook = nbformat.v4.new_notebook(
        cells=[nbformat.v4.new_code_cell("print('cell')", id="cell")]
    )
    path = tmp_path / "job.ipynb"
    nbformat.write(notebook, path)
    output_path = tmp_path / "job_output.ipynb"
    client = _client_context(mocker)
    child = _execution()
    client.start_batch.return_value = BatchWaitResult(
        batch_id=BATCH_ID,
        session="training",
        state=BatchState.FINISHED,
        continue_on_error=True,
        executions=[child],
        created_at="2026-07-18T00:00:00Z",
        updated_at="2026-07-18T00:00:01Z",
    )
    client.execution_output.return_value = OutputPage(
        execution_id=EXECUTION_ID,
        events=[],
        output_complete=True,
    )
    client.write_notebook_output.return_value = NotebookWriteResult(
        execution_id=EXECUTION_ID,
        notebook_id="b" * 64,
        path=str(output_path),
        cell_id="cell",
        notebook_sha256="c" * 64,
        outputs_written=1,
    )

    without_write = runner.invoke(
        app,
        ["exec", "-s", "training", "-f", str(path)],
    )
    assert without_write.exit_code == 0
    assert not output_path.exists()
    assert client.start_batch.call_args.kwargs["notebook"] == str(path)
    client.write_notebook_output.assert_not_called()

    with_write = runner.invoke(
        app,
        [
            "exec",
            "-s",
            "training",
            "-f",
            str(path),
            "--write-output",
        ],
    )
    assert with_write.exit_code == 0
    assert output_path.exists()
    assert client.start_batch.call_args.kwargs["notebook"] == str(output_path)
    client.write_notebook_output.assert_called_once_with(EXECUTION_ID)
