"""Agent-facing durable session health commands."""

from __future__ import annotations

import typer
from typing_extensions import Annotated

from better_colab.commands import _emit_json_operation
from better_colab.durable_commands import _client_from_cli_state
from better_colab.errors import BetterColabError, ExitCode
from better_colab.models import SessionHealthResult


session_app = typer.Typer(
    help="Ensure, inspect, probe, and stop durable sessions",
    no_args_is_help=True,
)


def _render_health(result: SessionHealthResult) -> None:
    typer.echo(f"Session: {result.name}")
    typer.echo(f"Backend: {'alive' if result.backend_alive else 'unavailable'}")
    typer.echo(
        f"Kernel: {'connected' if result.kernel_connected else 'disconnected'}"
    )
    typer.echo(
        "Execution: "
        f"{'ready' if result.kernel_execution_ready else 'not ready'}"
    )
    if result.kernel_probe_error:
        typer.echo(f"Probe error: {result.kernel_probe_error}")


def _format_operation(output_format: str, operation) -> None:
    normalized = output_format.lower()
    if normalized == "json":
        _emit_json_operation(operation)
        return
    if normalized != "text":
        typer.echo("format must be 'text' or 'json'", err=True)
        raise typer.Exit(code=int(ExitCode.USAGE))
    try:
        result = operation()
    except BetterColabError as error:
        typer.echo(error.error.message, err=True)
        raise typer.Exit(code=int(error.exit_code))
    _render_health(result)


@session_app.command(name="status")
def status_command(
    name: Annotated[str, typer.Argument(help="Session name")],
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
) -> None:
    """Observe cached connection health without executing code."""

    def operation() -> SessionHealthResult:
        with _client_from_cli_state() as client:
            return client.session_status(name)

    _format_operation(output_format, operation)


@session_app.command(name="probe")
def probe_command(
    name: Annotated[str, typer.Argument(help="Session name")],
    timeout: Annotated[
        float,
        typer.Option("--timeout", help="Probe deadline in seconds"),
    ] = 10,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
) -> None:
    """Execute and validate a no-history nonce readiness probe."""

    def operation() -> SessionHealthResult:
        with _client_from_cli_state() as client:
            return client.session_probe(name, timeout=timeout)

    _format_operation(output_format, operation)


def register(app: typer.Typer) -> None:
    app.add_typer(session_app, name="session")
