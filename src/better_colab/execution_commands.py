"""Agent-facing durable execution command group."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from typing_extensions import Annotated

from better_colab.durable_commands import _client_from_cli_state, execution_app
from better_colab.errors import BetterColabError, ExitCode, api_error
from better_colab.models import (
    ExecutionListResult,
    ExecutionResult,
    ExecutionWaitResult,
    OutputPage,
)
from better_colab.protocol import (
    DEFAULT_EXECUTION_LIMIT,
    DEFAULT_OUTPUT_PAGE_BYTES,
    render_error_bytes,
    render_success_bytes,
    write_response,
)


def _result_exit_code(
    result,
    *,
    attached: bool,
) -> ExitCode:
    if isinstance(result, ExecutionWaitResult) and result.wait_timed_out:
        return ExitCode.WAIT_TIMEOUT
    if attached and isinstance(result, ExecutionResult) and result.state.value in {
        "error",
        "interrupted",
        "timed_out",
        "unknown",
    }:
        return ExitCode.EXECUTION_FAILED
    return ExitCode.OK


def _json_operation(operation, *, attached: bool = False) -> None:
    try:
        result = operation()
        data, render_exit = render_success_bytes(result)
        exit_code = render_exit or _result_exit_code(result, attached=attached)
    except BetterColabError as error:
        data, exit_code = render_error_bytes(error.error, error.exit_code)
    write_response(data)
    if exit_code:
        raise typer.Exit(code=int(exit_code))


def _format_operation(
    output_format: str,
    operation,
    render,
    *,
    attached: bool = False,
) -> None:
    if output_format.lower() == "json":
        _json_operation(operation, attached=attached)
        return
    if output_format.lower() != "text":
        typer.echo("format must be 'text' or 'json'", err=True)
        raise typer.Exit(code=int(ExitCode.USAGE))
    try:
        result = operation()
    except BetterColabError as error:
        typer.echo(error.error.message, err=True)
        raise typer.Exit(code=int(error.exit_code))
    render(result)
    exit_code = _result_exit_code(result, attached=attached)
    if exit_code:
        raise typer.Exit(code=int(exit_code))


def _render_execution(result: ExecutionResult) -> None:
    typer.echo(f"{result.execution_id} {result.state.value}")
    if result.error_name:
        typer.echo(
            f"{result.error_name}: {result.error_value or ''}",
            err=True,
        )


def _render_list(result: ExecutionListResult) -> None:
    for execution in result.executions:
        _render_execution(execution)
    if result.next_cursor:
        typer.echo(f"Next cursor: {result.next_cursor}")


def _render_output(result: OutputPage) -> None:
    for event in result.events:
        if event.text:
            stream = sys.stderr if event.stream == "stderr" else sys.stdout
            stream.write(event.text)
    if result.next_cursor:
        typer.echo(f"Next cursor: {result.next_cursor}", err=True)


def _read_source(
    *,
    file: Path | None,
    notebook: Path | None,
    cell_id: str | None,
    cell_index: int | None,
) -> tuple[str, dict]:
    if notebook is not None:
        raise api_error(
            "NOTEBOOK_EXECUTION_NOT_AVAILABLE",
            "Notebook cell execution is introduced with notebook operations",
            exit_code=ExitCode.UNAVAILABLE,
            retryable=False,
            suggested_action="use_file_or_stdin",
        )
    if cell_id is not None or cell_index is not None:
        raise api_error(
            "NOTEBOOK_REQUIRED",
            "cell selection requires --notebook",
            exit_code=ExitCode.USAGE,
            retryable=False,
            suggested_action="specify_notebook",
        )
    if file is None:
        return sys.stdin.read(), {"kind": "stdin"}
    try:
        source = file.read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise api_error(
            "SOURCE_NOT_UTF8",
            f"Source file is not UTF-8: {file}",
            exit_code=ExitCode.USAGE,
            retryable=False,
            suggested_action="provide_utf8_source",
        ) from error
    except OSError as error:
        raise api_error(
            "SOURCE_UNREADABLE",
            f"Could not read source file: {file}",
            exit_code=ExitCode.NOT_FOUND,
            retryable=False,
            suggested_action="check_source_path",
            details={"error": str(error)},
        ) from error
    return source, {
        "kind": "file",
        "path": str(file.expanduser().resolve(strict=False)),
    }


@execution_app.command(name="start")
def start_command(
    session: Annotated[
        str,
        typer.Option("--session", help="Existing session name"),
    ],
    file: Annotated[
        Optional[Path],
        typer.Option("--file", help="Read exact UTF-8 source from a file"),
    ] = None,
    notebook: Annotated[
        Optional[Path],
        typer.Option("--notebook", help="Read one guarded notebook cell"),
    ] = None,
    cell_id: Annotated[
        Optional[str],
        typer.Option("--cell-id", help="Notebook cell ID"),
    ] = None,
    cell_index: Annotated[
        Optional[int],
        typer.Option("--cell-index", help="Notebook cell index"),
    ] = None,
    expected_source_sha256: Annotated[
        Optional[str],
        typer.Option(
            "--expected-source-sha256",
            help="Reject a changed source snapshot",
        ),
    ] = None,
    idempotency_key: Annotated[
        Optional[str],
        typer.Option("--idempotency-key", help="Stable retry key"),
    ] = None,
    execution_timeout: Annotated[
        Optional[float],
        typer.Option(
            "--execution-timeout",
            help="Interrupt deadline after confirmed running",
        ),
    ] = None,
    detach: Annotated[
        bool,
        typer.Option("--detach", help="Return after durable queueing"),
    ] = False,
    wait_timeout: Annotated[
        Optional[float],
        typer.Option("--wait-timeout", help="Bound only the caller's wait"),
    ] = None,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
) -> None:
    """Durably execute one exact source snapshot without allocating a session."""

    def operation() -> ExecutionResult | ExecutionWaitResult:
        if detach and wait_timeout is not None:
            raise api_error(
                "CONFLICTING_FLAGS",
                "--detach and --wait-timeout cannot be used together",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="choose_detach_or_wait_timeout",
            )
        if file is not None and notebook is not None:
            raise api_error(
                "CONFLICTING_SOURCE",
                "--file and --notebook cannot be used together",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="choose_one_source",
            )
        if cell_id is not None and cell_index is not None:
            raise api_error(
                "CONFLICTING_CELL_SELECTOR",
                "--cell-id and --cell-index cannot be used together",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="choose_one_cell_selector",
            )
        source, provenance = _read_source(
            file=file,
            notebook=notebook,
            cell_id=cell_id,
            cell_index=cell_index,
        )
        with _client_from_cli_state() as client:
            return client.start_execution(
                session=session,
                source=source,
                provenance=provenance,
                expected_source_sha256=expected_source_sha256,
                idempotency_key=idempotency_key,
                execution_timeout=execution_timeout,
                detach=detach,
                wait_timeout=wait_timeout,
            )

    _format_operation(
        output_format,
        operation,
        _render_execution,
        attached=not detach,
    )


@execution_app.command(name="status")
def status_command(
    execution_id: Annotated[str, typer.Argument(help="Execution UUID")],
    include: Annotated[
        Optional[list[str]],
        typer.Option("--include", help="Named expansion"),
    ] = None,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
) -> None:
    """Observe durable execution state without changing it."""

    def operation() -> ExecutionResult:
        with _client_from_cli_state() as client:
            return client.execution_status(execution_id, include=include)

    _format_operation(output_format, operation, _render_execution)


@execution_app.command(name="wait")
def wait_command(
    execution_id: Annotated[str, typer.Argument(help="Execution UUID")],
    timeout: Annotated[
        Optional[float],
        typer.Option("--timeout", help="Caller wait timeout"),
    ] = None,
    cursor: Annotated[
        Optional[str],
        typer.Option("--cursor", help="Opaque output cursor"),
    ] = None,
    max_bytes: Annotated[
        int,
        typer.Option("--max-bytes", help="Output page byte budget"),
    ] = DEFAULT_OUTPUT_PAGE_BYTES,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
) -> None:
    """Wait through a server-pushed condition without polling."""

    def operation() -> ExecutionWaitResult:
        with _client_from_cli_state() as client:
            return client.wait_execution(
                execution_id,
                timeout=timeout,
                cursor=cursor,
                max_bytes=max_bytes,
            )

    _format_operation(
        output_format,
        operation,
        _render_execution,
        attached=True,
    )


@execution_app.command(name="output")
def output_command(
    execution_id: Annotated[str, typer.Argument(help="Execution UUID")],
    cursor: Annotated[
        Optional[str],
        typer.Option("--cursor", help="Opaque output cursor"),
    ] = None,
    max_bytes: Annotated[
        int,
        typer.Option("--max-bytes", help="Output page byte budget"),
    ] = DEFAULT_OUTPUT_PAGE_BYTES,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
) -> None:
    """Read one stable bounded output page."""

    def operation() -> OutputPage:
        with _client_from_cli_state() as client:
            return client.execution_output(
                execution_id,
                cursor=cursor,
                max_bytes=max_bytes,
            )

    _format_operation(output_format, operation, _render_output)


@execution_app.command(name="cancel")
def cancel_command(
    execution_id: Annotated[str, typer.Argument(help="Execution UUID")],
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
) -> None:
    """Cancel queued work or request a verified running interrupt."""

    def operation() -> ExecutionResult:
        with _client_from_cli_state() as client:
            return client.cancel_execution(execution_id)

    _format_operation(output_format, operation, _render_execution)


@execution_app.command(name="list")
def list_command(
    session: Annotated[
        Optional[str],
        typer.Option("--session", help="Restrict to one session"),
    ] = None,
    cursor: Annotated[
        Optional[str],
        typer.Option("--cursor", help="Opaque collection cursor"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Maximum records in this page"),
    ] = DEFAULT_EXECUTION_LIMIT,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
) -> None:
    """List durable execution records."""

    def operation() -> ExecutionListResult:
        with _client_from_cli_state() as client:
            return client.list_executions(
                session=session,
                cursor=cursor,
                limit=limit,
            )

    _format_operation(output_format, operation, _render_list)


def register(_app: typer.Typer) -> None:
    """Commands register on durable_commands.execution_app at import time."""
