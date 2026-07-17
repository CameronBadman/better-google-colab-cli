# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from typing import Optional

import click
import typer
from typer.core import TyperGroup
from typing_extensions import Annotated

from colab_cli.auth import AuthProvider
from colab_cli.common import state, setup_logging
from colab_cli.commands import session, execution, files, automation, run, utility


class AlphabeticalGroup(TyperGroup):
    """A `TyperGroup` that lists subcommands alphabetically in `--help` output.

    Subcommands are registered in functional groups (session, execution, files,
    automation, utility), but users discovering the CLI via `colab --help` /
    `colab help` benefit from a deterministic, alphabetical listing.
    """

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted(super().list_commands(ctx))


def create_app(
    *,
    help_text: str,
    include_drive: bool,
    include_legacy_skill: bool,
) -> typer.Typer:
    """Build a CLI surface over the retained upstream command modules.

    The core distribution uses this factory to expose ``better-colab`` without
    Drive. The optional compatibility distribution points at the module-level
    ``app`` below, which retains the historical ``colab`` command set.
    """

    cli = typer.Typer(
        help=help_text,
        no_args_is_help=True,
        context_settings={"help_option_names": ["-h", "--help"]},
        cls=AlphabeticalGroup,
    )

    @cli.callback()
    def callback(
        ctx: typer.Context,
        client_oauth_config: Annotated[
            str,
            typer.Option(
                "-c",
                "--client-oauth-config",
                help="Path to client OAuth config JSON file",
            ),
        ] = os.path.expanduser("~/.colab-cli-oauth-config.json"),
        config: Annotated[
            Optional[str],
            typer.Option(
                "--config",
                help="Path to session state file (~/.config/colab-cli/sessions.json)",
            ),
        ] = None,
        logtostderr: Annotated[
            bool, typer.Option("--logtostderr", help="Log all output to stderr")
        ] = False,
        auth: Annotated[
            AuthProvider,
            typer.Option(
                "--auth",
                help=(
                    "Authentication strategy to use: 'oauth2' "
                    "(public InstalledAppFlow), or 'adc' "
                    "(Application Default Credentials)."
                ),
                case_sensitive=False,
            ),
        ] = AuthProvider.OAUTH2,
    ):
        """Configure authentication and local state for this invocation."""
        state.client_oauth_config = client_oauth_config
        state.config_path = config
        state.logtostderr = logtostderr
        state.auth_provider = auth
        if ctx.invoked_subcommand not in {
            "capabilities",
            "controller",
            "doctor",
            "execution",
            "notebook",
            "session",
        }:
            setup_logging(logtostderr)

        # Update checks are intentionally explicit. Agent-facing stdout must
        # never be changed by a background network request or cached banner.

    @cli.command(name="help")
    def help_command(
        ctx: typer.Context,
        command: Annotated[
            Optional[str], typer.Argument(help="Command to show help for")
        ] = None,
    ):
        """Show help for a command."""
        if not command:
            typer.echo(ctx.parent.get_help())
            return

        group = ctx.parent.command
        cmd = group.get_command(ctx, command)
        if cmd is None:
            typer.echo(f"No such command '{command}'.", err=True)
            raise typer.Exit(code=2)

        with click.Context(cmd, info_name=command, parent=ctx.parent) as cmd_ctx:
            typer.echo(cmd.get_help(cmd_ctx))

    session.register(cli)
    execution.register(cli)
    files.register(cli)
    automation.register(cli, include_drive=include_drive)
    run.register(cli)
    utility.register(cli, include_legacy_skill=include_legacy_skill)
    return cli


app = create_app(
    help_text="Colab CLI compatibility surface",
    include_drive=True,
    include_legacy_skill=True,
)


def main():
    app()


if __name__ == "__main__":
    main()
