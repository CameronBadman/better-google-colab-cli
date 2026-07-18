"""Agent-oriented metadata commands."""

from __future__ import annotations

from typing import Optional

import typer
from typing_extensions import Annotated

from better_colab.client import BetterColabClient
from better_colab.errors import BetterColabError, ExitCode
from better_colab.models import CapabilitiesResult, DoctorResult
from better_colab.protocol import (
    DEFAULT_EXECUTION_LIMIT,
    render_error_bytes,
    render_success_bytes,
    write_response,
)


def _emit_json_operation(operation) -> None:
    try:
        result = operation()
        data, exit_code = render_success_bytes(result)
    except BetterColabError as error:
        data, exit_code = render_error_bytes(error.error, error.exit_code)
    write_response(data)
    if exit_code:
        raise typer.Exit(code=int(exit_code))


def _render_capabilities(result: CapabilitiesResult) -> None:
    for command in result.commands:
        typer.echo(f"{command.name}: {command.summary}")
    if result.next_cursor:
        typer.echo(f"Next cursor: {result.next_cursor}")


def _render_doctor(result: DoctorResult) -> None:
    status = "running" if result.controller_alive else "not running"
    typer.echo(f"Controller: {status}")
    typer.echo(f"State: {result.state_path}")
    typer.echo(f"Runtime: {result.runtime_dir}")
    typer.echo(f"Version: {result.package_version}")


def register(app: typer.Typer) -> None:
    @app.command(name="capabilities")
    def capabilities_command(
        command: Annotated[
            Optional[str],
            typer.Argument(help="Command name to inspect; dots stand for spaces"),
        ] = None,
        output_format: Annotated[
            str,
            typer.Option("--format", help="Output format: text or json"),
        ] = "text",
        cursor: Annotated[
            Optional[str], typer.Option("--cursor", help="Opaque page cursor")
        ] = None,
        limit: Annotated[
            int, typer.Option("--limit", help="Maximum commands in this page")
        ] = DEFAULT_EXECUTION_LIMIT,
    ) -> None:
        """Discover the compact machine-facing command contract."""
        client = BetterColabClient()
        if output_format.lower() == "json":
            _emit_json_operation(
                lambda: client.capabilities(
                    command=command,
                    cursor=cursor,
                    limit=limit,
                )
            )
            return
        if output_format.lower() != "text":
            typer.echo("format must be 'text' or 'json'", err=True)
            raise typer.Exit(code=int(ExitCode.USAGE))
        try:
            result = client.capabilities(command=command, cursor=cursor, limit=limit)
        except BetterColabError as error:
            typer.echo(error.error.message, err=True)
            raise typer.Exit(code=int(error.exit_code))
        _render_capabilities(result)

    @app.command(name="doctor")
    def doctor_command(
        output_format: Annotated[
            str,
            typer.Option("--format", help="Output format: text or json"),
        ] = "text",
    ) -> None:
        """Inspect local Better Colab health without network or auth side effects."""
        client = BetterColabClient()
        if output_format.lower() == "json":
            _emit_json_operation(client.doctor)
            return
        if output_format.lower() != "text":
            typer.echo("format must be 'text' or 'json'", err=True)
            raise typer.Exit(code=int(ExitCode.USAGE))
        _render_doctor(client.doctor())
