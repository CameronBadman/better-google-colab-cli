"""Explicit lifecycle commands for the persistent local controller."""

from __future__ import annotations

import typer
from typing_extensions import Annotated

from better_colab.client import BetterColabClient
from better_colab.commands import _emit_json_operation
from better_colab.errors import BetterColabError, ExitCode
from better_colab.models import ControllerStatus, ControllerStopResult


controller_app = typer.Typer(
    help="Inspect or stop the persistent local controller",
    no_args_is_help=True,
)


def _run_text(operation, render) -> None:
    try:
        result = operation()
    except BetterColabError as error:
        typer.echo(error.error.message, err=True)
        raise typer.Exit(code=int(error.exit_code))
    render(result)


def _format(
    output_format: str,
    operation,
    render,
) -> None:
    if output_format.lower() == "json":
        _emit_json_operation(operation)
        return
    if output_format.lower() != "text":
        typer.echo("format must be 'text' or 'json'", err=True)
        raise typer.Exit(code=int(ExitCode.USAGE))
    _run_text(operation, render)


def _render_status(result: ControllerStatus) -> None:
    if not result.controller_alive:
        typer.echo("Controller: not running")
        return
    typer.echo(f"Controller: running (pid {result.pid})")
    typer.echo(f"Protocol: {result.protocol_version}")
    typer.echo(f"Active executions: {result.active_executions}")


def _render_stop(result: ControllerStopResult) -> None:
    if not result.stopping:
        typer.echo("Controller: not running")
    else:
        typer.echo("Controller stopped.")
    if result.affected:
        typer.echo(f"Marked uncertain: {len(result.affected)}")


@controller_app.command(name="start")
def start_command(
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
) -> None:
    """Start the controller if needed and return its handshake."""
    client = BetterColabClient()
    _format(output_format, client.controller_start, _render_status)


@controller_app.command(name="status")
def status_command(
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
) -> None:
    """Observe controller state without starting it."""
    client = BetterColabClient()
    _format(output_format, client.controller_status, _render_status)


@controller_app.command(name="stop")
def stop_command(
    force: Annotated[
        bool,
        typer.Option("--force", help="Mark active executions uncertain and stop"),
    ] = False,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
) -> None:
    """Stop the controller, refusing active work unless forced."""
    client = BetterColabClient()

    def operation() -> ControllerStopResult:
        return client.controller_stop(force=force)

    _format(output_format, operation, _render_stop)


def register(app: typer.Typer) -> None:
    app.add_typer(controller_app, name="controller")
