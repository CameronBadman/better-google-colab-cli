import base64
import hashlib

import nbformat
import pytest
from typer.testing import CliRunner

from better_colab import BetterColabClient, BetterColabError, ExecutionState
from better_colab.cli import app
from better_colab.models import NotebookWriteResult
from better_colab.storage import DurableStore, ProfileSpec, StatePaths


EXECUTION_ID = "00000000-0000-4000-8000-000000000801"
runner = CliRunner()


@pytest.fixture
def context(tmp_path):
    path = tmp_path / "writeback.ipynb"
    source = "print('new output')"
    notebook = nbformat.v4.new_notebook(
        cells=[
            nbformat.v4.new_code_cell(
                "untouched = True",
                id="untouched",
                outputs=[
                    nbformat.v4.new_output(
                        "stream",
                        name="stdout",
                        text="keep\n",
                    )
                ],
            ),
            nbformat.v4.new_code_cell(source, id="target"),
        ]
    )
    nbformat.write(notebook, path)
    paths = StatePaths(
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "run",
    )
    profile = ProfileSpec.from_values(
        config_path=tmp_path / "sessions.json",
        auth_provider="oauth2",
        oauth_config_path=tmp_path / "oauth.json",
    )
    store = DurableStore(paths=paths, profile=profile)
    store.upsert_session(
        name="training",
        endpoint="endpoint",
        backend_url="https://runtime.example",
        runtime_token="secret",
        hardware="CPU",
    )
    notebook_id = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    execution = store.create_execution(
        execution_id=EXECUTION_ID,
        session_name="training",
        source=source.encode(),
        provenance={
            "kind": "notebook_cell",
            "path": str(path.resolve()),
            "notebook_id": notebook_id,
            "cell_id": "target",
            "cell_index": 1,
        },
        request={"session": "training", "source_sha256": "source"},
    )
    store.begin_dispatch(
        execution.execution_id,
        kernel_message_id="message",
        session_endpoint="endpoint",
        kernel_id="kernel",
        jupyter_session_id="jupyter",
    )
    store.confirm_dispatch(execution.execution_id)
    yield path, source, paths, profile, store
    store.close()


def _finish(store, *, state=ExecutionState.FINISHED):
    store.record_execution_evidence(
        EXECUTION_ID,
        reply_received=True,
        idle_received=True,
        reply_status="ok" if state is ExecutionState.FINISHED else "error",
        error_name="ValueError" if state is ExecutionState.ERROR else None,
        error_value="bad" if state is ExecutionState.ERROR else None,
        traceback=["trace"] if state is ExecutionState.ERROR else None,
    )
    store.finalize_output(EXECUTION_ID)
    store.transition_execution(EXECUTION_ID, state)


def _client(paths, profile):
    return BetterColabClient(
        paths=paths,
        config_path=profile.config_path,
        auth_provider=profile.auth_provider,
        oauth_config_path=profile.oauth_config_path,
    )


def test_writeback_requires_explicit_complete_terminal_execution(context):
    path, source, paths, profile, store = context
    png = b"\x89PNG\r\n\x1a\nwriteback"
    store.append_output_event(
        EXECUTION_ID,
        {
            "event_type": "stream",
            "stream": "stdout",
            "text": "x" * 700,
        },
    )
    store.append_output_event(
        EXECUTION_ID,
        {
            "event_type": "display_data",
            "data": {
                "text/plain": "plain",
                "image/png": base64.b64encode(png).decode(),
            },
            "metadata": {"isolated": True},
            "display_id": "display-1",
        },
    )
    _finish(store)

    with _client(paths, profile) as client:
        result = client.write_notebook_output(EXECUTION_ID)
    notebook = nbformat.read(path, as_version=4)

    assert result.outputs_written == 3
    assert result.cell_id == "target"
    assert notebook.cells[0].outputs[0].text == "keep\n"
    assert notebook.cells[1].source == source
    assert notebook.cells[1].outputs[0].output_type == "stream"
    assert notebook.cells[1].outputs[0].text == "x" * 700
    assert notebook.cells[1].outputs[1].data == {"text/plain": "plain"}
    assert notebook.cells[1].outputs[2].data["image/png"] == base64.b64encode(
        png
    ).decode()


def test_error_writeback_preserves_error_metadata_and_preceding_output(context):
    path, _source, paths, profile, store = context
    store.append_output_event(
        EXECUTION_ID,
        {"event_type": "stream", "stream": "stdout", "text": "before\n"},
    )
    store.append_output_event(
        EXECUTION_ID,
        {
            "event_type": "error",
            "error_name": "ValueError",
            "error_value": "bad",
            "traceback": ["trace one", "trace two"],
        },
    )
    _finish(store, state=ExecutionState.ERROR)

    with _client(paths, profile) as client:
        client.write_notebook_output(EXECUTION_ID)
    outputs = nbformat.read(path, as_version=4).cells[1].outputs

    assert outputs[0].text == "before\n"
    assert outputs[1].output_type == "error"
    assert outputs[1].ename == "ValueError"
    assert outputs[1].evalue == "bad"
    assert outputs[1].traceback == ["trace one", "trace two"]


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("running", "WRITEBACK_EXECUTION_NOT_TERMINAL"),
        ("incomplete", "WRITEBACK_OUTPUT_INCOMPLETE"),
        ("source", "SOURCE_HASH_MISMATCH"),
        ("identity", "NOTEBOOK_IDENTITY_MISMATCH"),
    ],
)
def test_writeback_guards_notebook_and_execution(context, mutation, code):
    path, _source, paths, profile, store = context
    if mutation != "running":
        _finish(store)
    if mutation == "incomplete":
        store.mark_output_incomplete(EXECUTION_ID)
    elif mutation == "source":
        notebook = nbformat.read(path, as_version=4)
        notebook.cells[1].source = "changed"
        nbformat.write(notebook, path)
    elif mutation == "identity":
        moved = path.with_name("moved.ipynb")
        path.rename(moved)
        path = moved
        store.connection.execute(
            "UPDATE executions SET source_path = ? WHERE execution_id = ?",
            (str(path.resolve()), EXECUTION_ID),
        )

    with _client(paths, profile) as client:
        with pytest.raises(BetterColabError) as error:
            client.write_notebook_output(EXECUTION_ID)

    assert error.value.error.code == code


def test_notebook_write_output_cli_is_explicit_and_json(mocker):
    mocker.patch(
        "better_colab.durable_commands.BetterColabClient.write_notebook_output",
        return_value=NotebookWriteResult(
            execution_id=EXECUTION_ID,
            notebook_id="a" * 64,
            path="/tmp/notebook.ipynb",
            cell_id="target",
            notebook_sha256="b" * 64,
            outputs_written=1,
        ),
    )

    result = runner.invoke(
        app,
        [
            "notebook",
            "write-output",
            EXECUTION_ID,
            "--format=json",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout.count("\n") == 1
    assert __import__("json").loads(result.stdout)["result"]["outputs_written"] == 1
