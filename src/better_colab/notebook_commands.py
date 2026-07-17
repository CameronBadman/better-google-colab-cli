"""Agent-facing guarded notebook document commands."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Optional

import typer
from typing_extensions import Annotated

from better_colab.durable_commands import _client_from_cli_state
from better_colab.execution_commands import _format_operation
from better_colab.errors import ExitCode, api_error
from better_colab.models import (
    NotebookCell,
    NotebookCellsResult,
    NotebookIdsResult,
    NotebookWriteResult,
)
from better_colab.protocol import DEFAULT_NOTEBOOK_CELL_LIMIT


notebook_app = typer.Typer(
    help="Inspect and guard local notebook documents",
    no_args_is_help=True,
)
ids_app = typer.Typer(
    help="Manage explicit notebook cell IDs",
    no_args_is_help=True,
)
notebook_app.add_typer(ids_app, name="ids")


def _render_cells(result: NotebookCellsResult) -> None:
    for cell in result.cells:
        typer.echo(
            f"{cell.index} {cell.cell_type} "
            f"{cell.cell_id or '-'} {cell.source_sha256}"
        )
    if result.next_cursor:
        typer.echo(f"Next cursor: {result.next_cursor}")


def _render_cell(result: NotebookCell) -> None:
    typer.echo(result.source)


@notebook_app.command(name="cells")
def cells_command(
    path: Annotated[Path, typer.Argument(help="Local notebook path")],
    cursor: Annotated[
        Optional[str],
        typer.Option("--cursor", help="Opaque collection cursor"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Maximum cells in this page"),
    ] = DEFAULT_NOTEBOOK_CELL_LIMIT,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
) -> None:
    """List cell metadata without source or outputs."""

    def operation() -> NotebookCellsResult:
        with _client_from_cli_state() as client:
            return client.notebook_cells(path, cursor=cursor, limit=limit)

    _format_operation(output_format, operation, _render_cells)


@notebook_app.command(name="cell")
def cell_command(
    path: Annotated[Path, typer.Argument(help="Local notebook path")],
    cell_id: Annotated[
        Optional[str],
        typer.Option("--cell-id", help="Path-namespaced cell ID"),
    ] = None,
    index: Annotated[
        Optional[int],
        typer.Option("--index", help="Zero-based cell index"),
    ] = None,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
) -> None:
    """Inspect one cell with source but without outputs."""

    def operation() -> NotebookCell:
        with _client_from_cli_state() as client:
            return client.notebook_cell(path, cell_id=cell_id, index=index)

    _format_operation(output_format, operation, _render_cell)


@notebook_app.command(name="update")
def update_command(
    path: Annotated[Path, typer.Argument(help="Local notebook path")],
    cell_id: Annotated[
        Optional[str],
        typer.Option("--cell-id", help="Path-namespaced cell ID"),
    ] = None,
    index: Annotated[
        Optional[int],
        typer.Option("--index", help="Zero-based cell index"),
    ] = None,
    file: Annotated[
        Optional[Path],
        typer.Option("--file", help="Read replacement UTF-8 source"),
    ] = None,
    expected_sha256: Annotated[
        Optional[str],
        typer.Option("--expected-sha256", help="Expected current source hash"),
    ] = None,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
) -> None:
    """Atomically replace one cell source under an optional hash guard."""

    def operation() -> NotebookCell:
        if file is None:
            source = sys.stdin.read()
        else:
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
        with _client_from_cli_state() as client:
            return client.update_notebook_cell(
                path,
                source=source,
                cell_id=cell_id,
                index=index,
                expected_sha256=expected_sha256,
            )

    _format_operation(output_format, operation, _render_cell)


def _render_ids(result: NotebookIdsResult) -> None:
    for cell_id in result.assigned:
        typer.echo(cell_id)


@ids_app.command(name="assign")
def ids_assign_command(
    path: Annotated[Path, typer.Argument(help="Local notebook path")],
    expected_notebook_sha256: Annotated[
        str,
        typer.Option(
            "--expected-notebook-sha256",
            help="Expected exact notebook file hash",
        ),
    ],
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
) -> None:
    """Explicitly assign missing IDs under a notebook hash guard."""

    def operation() -> NotebookIdsResult:
        with _client_from_cli_state() as client:
            return client.assign_notebook_ids(
                path,
                expected_notebook_sha256=expected_notebook_sha256,
            )

    _format_operation(output_format, operation, _render_ids)


def _render_write(result: NotebookWriteResult) -> None:
    typer.echo(
        f"Wrote {result.outputs_written} output(s) to "
        f"{result.path}#{result.cell_id}"
    )


@notebook_app.command(name="write-output")
def write_output_command(
    execution_id: Annotated[str, typer.Argument(help="Execution UUID")],
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
) -> None:
    """Explicitly write complete guarded output to its original cell."""

    def operation() -> NotebookWriteResult:
        with _client_from_cli_state() as client:
            return client.write_notebook_output(execution_id)

    _format_operation(output_format, operation, _render_write)


def register(app: typer.Typer) -> None:
    app.add_typer(notebook_app, name="notebook")
