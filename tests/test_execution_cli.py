import hashlib
import json

import nbformat
from typer.testing import CliRunner

from better_colab.cli import app
from better_colab.errors import ExitCode
from better_colab.models import (
    ExecutionListResult,
    ExecutionResult,
    ExecutionWaitResult,
    OutputPage,
)


runner = CliRunner()
EXECUTION_ID = "00000000-0000-4000-8000-000000000401"


def _execution(state="queued", **changes):
    values = {
        "execution_id": EXECUTION_ID,
        "session": "training",
        "state": state,
        "source_sha256": "a" * 64,
        "output_complete": True,
        "dispatch_confirmed": state != "queued",
        "reply_received": state in {"finished", "error"},
        "idle_received": state in {"finished", "error"},
        "created_at": "2026-07-17T00:00:00Z",
        "updated_at": "2026-07-17T00:00:01Z",
    }
    values.update(changes)
    return ExecutionResult(**values)


def _wait(state="finished", *, timed_out=False):
    return ExecutionWaitResult(
        **_execution(state).model_dump(),
        wait_timed_out=timed_out,
        output=OutputPage(
            execution_id=EXECUTION_ID,
            events=[],
            has_more=False,
            output_complete=True,
        ),
    )


def _payload(result):
    assert len(result.stdout.splitlines()) == 1, result.stdout
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    return payload


def test_detached_start_reads_exact_file_and_emits_one_json(
    mocker, tmp_path
):
    source = "print('hello')\n"
    path = tmp_path / "job.py"
    path.write_text(source, encoding="utf-8")
    start = mocker.patch(
        "better_colab.durable_commands.BetterColabClient.start_execution",
        return_value=_execution(),
    )

    result = runner.invoke(
        app,
        [
            "execution",
            "start",
            "--session",
            "training",
            "--file",
            str(path),
            "--expected-source-sha256",
            hashlib.sha256(source.encode()).hexdigest(),
            "--idempotency-key",
            "stable",
            "--detach",
            "--format=json",
        ],
    )

    assert result.exit_code == ExitCode.OK
    assert _payload(result)["result"]["state"] == "queued"
    assert start.call_args.kwargs["source"] == source
    assert start.call_args.kwargs["detach"] is True


def test_start_uses_piped_stdin_when_file_is_absent(mocker):
    start = mocker.patch(
        "better_colab.durable_commands.BetterColabClient.start_execution",
        return_value=_execution(),
    )

    result = runner.invoke(
        app,
        [
            "execution",
            "start",
            "--session",
            "training",
            "--detach",
            "--format=json",
        ],
        input="x = 1\n",
    )

    assert result.exit_code == ExitCode.OK
    assert start.call_args.kwargs["source"] == "x = 1\n"
    assert start.call_args.kwargs["provenance"] == {"kind": "stdin"}


def test_attached_terminal_error_is_success_envelope_with_exit_one(mocker):
    mocker.patch(
        "better_colab.durable_commands.BetterColabClient.start_execution",
        return_value=_wait(
            "error",
        ).model_copy(
            update={
                "error_name": "ValueError",
                "error_value": "bad",
                "traceback": ["trace"],
            }
        ),
    )

    result = runner.invoke(
        app,
        [
            "execution",
            "start",
            "--session",
            "training",
            "--format=json",
        ],
        input="raise ValueError('bad')\n",
    )

    assert result.exit_code == ExitCode.EXECUTION_FAILED
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["result"]["state"] == "error"
    assert payload["result"]["error_name"] == "ValueError"


def test_status_observes_terminal_error_with_exit_zero(mocker):
    mocker.patch(
        "better_colab.durable_commands.BetterColabClient.execution_status",
        return_value=_execution(
            "error",
            error_name="ValueError",
            error_value="bad",
        ),
    )

    result = runner.invoke(
        app,
        [
            "execution",
            "status",
            EXECUTION_ID,
            "--format=json",
        ],
    )

    assert result.exit_code == ExitCode.OK
    assert _payload(result)["result"]["state"] == "error"


def test_wait_timeout_is_observation_exit_124_and_does_not_cancel(mocker):
    wait = mocker.patch(
        "better_colab.durable_commands.BetterColabClient.wait_execution",
        return_value=_wait("running", timed_out=True),
    )
    cancel = mocker.patch(
        "better_colab.durable_commands.BetterColabClient.cancel_execution"
    )

    result = runner.invoke(
        app,
        [
            "execution",
            "wait",
            EXECUTION_ID,
            "--timeout",
            "0.01",
            "--format=json",
        ],
    )

    assert result.exit_code == ExitCode.WAIT_TIMEOUT
    assert _payload(result)["result"]["wait_timed_out"] is True
    wait.assert_called_once()
    cancel.assert_not_called()


def test_attached_wait_timeout_uses_exit_124(mocker):
    mocker.patch(
        "better_colab.durable_commands.BetterColabClient.start_execution",
        return_value=_wait("running", timed_out=True),
    )

    result = runner.invoke(
        app,
        [
            "execution",
            "start",
            "--session",
            "training",
            "--wait-timeout",
            "0.01",
            "--format=json",
        ],
        input="import time; time.sleep(10)\n",
    )

    assert result.exit_code == ExitCode.WAIT_TIMEOUT
    assert _payload(result)["result"]["state"] == "running"


def test_list_cancel_and_output_are_compact_json(mocker):
    mocker.patch(
        "better_colab.durable_commands.BetterColabClient.list_executions",
        return_value=ExecutionListResult(executions=[_execution()]),
    )
    mocker.patch(
        "better_colab.durable_commands.BetterColabClient.cancel_execution",
        return_value=_execution("interrupted"),
    )
    mocker.patch(
        "better_colab.durable_commands.BetterColabClient.execution_output",
        return_value=OutputPage(
            execution_id=EXECUTION_ID,
            events=[],
            output_complete=True,
        ),
    )

    listed = runner.invoke(
        app,
        ["execution", "list", "--format=json"],
    )
    cancelled = runner.invoke(
        app,
        ["execution", "cancel", EXECUTION_ID, "--format=json"],
    )
    output = runner.invoke(
        app,
        ["execution", "output", EXECUTION_ID, "--format=json"],
    )

    assert _payload(listed)["result"]["executions"][0]["execution_id"] == EXECUTION_ID
    assert _payload(cancelled)["result"]["state"] == "interrupted"
    assert _payload(output)["result"]["events"] == []


def test_detach_and_wait_timeout_are_mutually_exclusive():
    result = runner.invoke(
        app,
        [
            "execution",
            "start",
            "--session",
            "training",
            "--detach",
            "--wait-timeout",
            "1",
            "--format=json",
        ],
        input="pass\n",
    )

    assert result.exit_code == ExitCode.USAGE


def test_notebook_start_captures_path_namespaced_cell_source(mocker, tmp_path):
    path = tmp_path / "job.ipynb"
    source = "stateful_value = 41\n"
    nbformat.write(
        nbformat.v4.new_notebook(
            cells=[
                nbformat.v4.new_code_cell(source, id="setup"),
                nbformat.v4.new_code_cell("print(stateful_value)", id="use"),
            ]
        ),
        path,
    )
    start = mocker.patch(
        "better_colab.durable_commands.BetterColabClient.start_execution",
        return_value=_execution(),
    )

    result = runner.invoke(
        app,
        [
            "execution",
            "start",
            "--session",
            "training",
            "--notebook",
            str(path),
            "--cell-index",
            "0",
            "--expected-source-sha256",
            hashlib.sha256(source.encode()).hexdigest(),
            "--detach",
            "--format=json",
        ],
    )

    assert result.exit_code == ExitCode.OK
    assert start.call_args.kwargs["source"] == source
    provenance = start.call_args.kwargs["provenance"]
    assert provenance["kind"] == "notebook_cell"
    assert provenance["path"] == str(path.resolve())
    assert provenance["cell_id"] == "setup"
    assert provenance["cell_index"] == 0
    assert provenance["notebook_id"] == hashlib.sha256(
        str(path.resolve()).encode()
    ).hexdigest()


def test_notebook_execution_requires_an_id_even_when_selected_by_index(
    mocker,
    tmp_path,
):
    path = tmp_path / "missing.ipynb"
    path.write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "execution_count": None,
                        "metadata": {},
                        "outputs": [],
                        "source": "pass",
                    }
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ),
        encoding="utf-8",
    )
    start = mocker.patch(
        "better_colab.durable_commands.BetterColabClient.start_execution"
    )

    result = runner.invoke(
        app,
        [
            "execution",
            "start",
            "--session",
            "training",
            "--notebook",
            str(path),
            "--cell-index",
            "0",
            "--detach",
            "--format=json",
        ],
    )

    assert result.exit_code == ExitCode.CONFLICT
    assert _payload(result)["error"]["code"] == "CELL_ID_REQUIRED"
    start.assert_not_called()
