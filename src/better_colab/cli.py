"""The ``better-colab`` command-line entry point."""

from better_colab import (
    commands,
    controller_commands,
    durable_commands,
    execution_commands,
    notebook_commands,
    session_commands,
)
from colab_cli.cli import create_app


app = create_app(
    help_text="Better Colab CLI",
    include_drive=False,
    durable_wrappers=True,
)
commands.register(app)
durable_commands.register(app)
execution_commands.register(app)
controller_commands.register(app)
session_commands.register(app)
notebook_commands.register(app)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
