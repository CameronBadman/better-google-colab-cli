"""The ``better-colab`` command-line entry point."""

from better_colab import commands
from colab_cli.cli import create_app


app = create_app(
    help_text="Better Colab CLI",
    include_drive=False,
    include_legacy_skill=False,
)
commands.register(app)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
