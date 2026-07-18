from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from better_colab import (
    ExecutionState,
    ExecutionWaitResult,
    OutputEvent,
    OutputPage,
    SessionStopResult,
    SessionSummary,
)
from better_colab.cli import app


runner = CliRunner()
EXECUTION_ID = "00000000-0000-4000-8000-000000000b01"


def _wait(
    *,
    state: ExecutionState = ExecutionState.FINISHED,
    error_name: str | None = None,
    error_value: str | None = None,
    events: list[OutputEvent] | None = None,
) -> ExecutionWaitResult:
    return ExecutionWaitResult(
        execution_id=EXECUTION_ID,
        session="job",
        state=state,
        source_sha256="a" * 64,
        output_complete=True,
        dispatch_confirmed=True,
        reply_received=True,
        idle_received=True,
        error_name=error_name,
        error_value=error_value,
        created_at="2026-07-18T00:00:00Z",
        updated_at="2026-07-18T00:00:01Z",
        output=OutputPage(
            execution_id=EXECUTION_ID,
            events=events or [],
            output_complete=True,
        ),
    )


def _client_context(mocker, mock_common_state):
    # Keep the pre-implementation compatibility path safely mocked so the
    # initial TDD failure cannot allocate a real runtime.
    mock_common_state.client.assign.return_value = SimpleNamespace(
        endpoint="legacy-endpoint",
        runtime_proxy_info=SimpleNamespace(
            token="token",
            url="https://runtime.example",
        ),
    )
    mocker.patch(
        "colab_cli.commands.run.spawn_keep_alive",
        return_value=123,
    )
    context = mocker.MagicMock()
    client = context.__enter__.return_value
    mocker.patch(
        "colab_cli.commands.run._client_from_cli_state",
        return_value=context,
        create=True,
    )
    client.ensure_session.return_value = SessionSummary(
        name="job",
        endpoint="endpoint",
        hardware="CPU",
        variant="DEFAULT",
        status="ready",
    )
    client.stop_session.return_value = SessionStopResult(
        name="job",
        stopped=True,
    )
    return client


def test_better_flat_run_composes_typed_session_and_execution(
    mocker,
    mock_common_state,
    tmp_path,
):
    script = tmp_path / "job.py"
    script.write_text("print('hello')\n", encoding="utf-8")
    client = _client_context(mocker, mock_common_state)
    client.start_execution.return_value = _wait(
        events=[
            OutputEvent(
                cursor="cursor",
                event_type="stream",
                stream="stdout",
                text="hello\n",
            )
        ]
    )

    result = runner.invoke(
        app,
        [
            "run",
            "-s",
            "job",
            "--timeout",
            "12",
            str(script),
            "alpha",
            "--script-flag",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    client.ensure_session.assert_called_once_with("job", gpu=None, tpu=None)
    call = client.start_execution.call_args.kwargs
    assert "sys.argv = ['job.py', 'alpha', '--script-flag']" in call["source"]
    assert call["session"] == "job"
    assert call["provenance"] == {
        "kind": "file",
        "path": str(script.resolve()),
    }
    assert call["execution_timeout"] == 12
    client.stop_session.assert_called_once_with("job")


def test_better_flat_run_keep_retains_session(
    mocker,
    mock_common_state,
    tmp_path,
):
    script = tmp_path / "job.py"
    script.write_text("pass\n", encoding="utf-8")
    client = _client_context(mocker, mock_common_state)
    client.start_execution.return_value = _wait()

    result = runner.invoke(
        app,
        ["run", "-s", "job", "--keep", str(script)],
    )

    assert result.exit_code == 0
    client.stop_session.assert_not_called()


def test_better_flat_run_returns_one_and_cleans_up_on_user_error(
    mocker,
    mock_common_state,
    tmp_path,
):
    script = tmp_path / "job.py"
    script.write_text("raise ValueError('bad')\n", encoding="utf-8")
    client = _client_context(mocker, mock_common_state)
    client.start_execution.return_value = _wait(
        state=ExecutionState.ERROR,
        error_name="ValueError",
        error_value="bad",
        events=[
            OutputEvent(
                cursor="one",
                event_type="stream",
                stream="stdout",
                text="before\n",
            ),
            OutputEvent(
                cursor="two",
                event_type="error",
                error_name="ValueError",
                error_value="bad",
                traceback=["ValueError: bad\n"],
            ),
        ],
    )

    result = runner.invoke(app, ["run", "-s", "job", str(script)])

    assert result.exit_code == 1
    assert result.stdout == "before\n"
    assert "ValueError: bad" in result.stderr
    client.stop_session.assert_called_once_with("job")


@pytest.mark.parametrize(
    ("error_value", "expected_exit"),
    [("0", 0), ("7", 7), ("boom", 1)],
)
def test_better_flat_run_preserves_systemexit_without_traceback(
    mocker,
    mock_common_state,
    tmp_path,
    error_value,
    expected_exit,
):
    script = tmp_path / "job.py"
    script.write_text(f"raise SystemExit({error_value!r})\n", encoding="utf-8")
    client = _client_context(mocker, mock_common_state)
    client.start_execution.return_value = _wait(
        state=ExecutionState.ERROR,
        error_name="SystemExit",
        error_value=error_value,
        events=[
            OutputEvent(
                cursor="cursor",
                event_type="error",
                error_name="SystemExit",
                error_value=error_value,
                traceback=["An exception has occurred\n", "SystemExit\n"],
            )
        ],
    )

    result = runner.invoke(app, ["run", "-s", "job", str(script)])

    assert result.exit_code == expected_exit
    assert "An exception has occurred" not in result.output
    assert "SystemExit" not in result.output
    if error_value == "boom":
        assert "boom" in result.stderr
    client.stop_session.assert_called_once_with("job")
