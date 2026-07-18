"""CLI commands backed directly by durable SQLite state."""

from __future__ import annotations

from typing import Optional

import typer
from typing_extensions import Annotated

from better_colab.client import BetterColabClient
from better_colab.commands import _emit_json_operation
from better_colab.errors import BetterColabError, ExitCode, api_error
from better_colab.models import PruneResult


execution_app = typer.Typer(
    help="Create and inspect durable executions",
    no_args_is_help=True,
)


def _client_from_cli_state() -> BetterColabClient:
    # Resolve the singleton dynamically so embedders and tests can replace it.
    # The root callback configures this same object before command dispatch.
    from colab_cli.common import state

    provider = getattr(state.auth_provider, "value", str(state.auth_provider))
    return BetterColabClient(
        config_path=state.config_path,
        auth_provider=provider,
        oauth_config_path=state.client_oauth_config,
    )


def _prune_operation(
    *,
    before: str,
    session: str | None,
    dry_run: bool,
    confirm: bool,
) -> PruneResult:
    if dry_run and confirm:
        raise api_error(
            "CONFLICTING_FLAGS",
            "--dry-run and --confirm cannot be used together",
            exit_code=ExitCode.USAGE,
            retryable=False,
            suggested_action="choose_dry_run_or_confirm",
        )
    with _client_from_cli_state() as client:
        return client.prune_executions(
            before=before,
            session=session,
            confirm=confirm,
        )


def _render_prune(result: PruneResult) -> None:
    action = "Would delete" if result.dry_run else "Deleted"
    typer.echo(f"{action} {result.matched if result.dry_run else result.deleted} execution(s).")
    typer.echo(f"Artifact bytes: {result.artifact_bytes}")
    for execution_id in result.execution_ids:
        typer.echo(execution_id)


@execution_app.command(name="prune")
def prune_command(
    before: Annotated[
        str,
        typer.Option("--before", help="Delete terminal data older than timestamp"),
    ],
    session: Annotated[
        Optional[str], typer.Option("--session", help="Restrict to one session")
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview only (the default)"),
    ] = False,
    confirm: Annotated[
        bool,
        typer.Option("--confirm", help="Perform the deletion"),
    ] = False,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
) -> None:
    """Prune explicitly selected terminal execution data."""

    def operation() -> PruneResult:
        return _prune_operation(
            before=before,
            session=session,
            dry_run=dry_run,
            confirm=confirm,
        )
    if output_format.lower() == "json":
        _emit_json_operation(operation)
        return
    if output_format.lower() != "text":
        typer.echo("format must be 'text' or 'json'", err=True)
        raise typer.Exit(code=int(ExitCode.USAGE))
    try:
        result = operation()
    except BetterColabError as error:
        typer.echo(error.error.message, err=True)
        raise typer.Exit(code=int(error.exit_code))
    _render_prune(result)


def register(app: typer.Typer) -> None:
    app.add_typer(execution_app, name="execution")
