"""Low-overhead executable entry point for common machine observations."""

from __future__ import annotations

import sys
from typing import Any


def _parse_fast_status(args: list[str]) -> dict[str, Any] | None:
    config_path: str | None = None
    auth_provider = "oauth2"
    oauth_config_path: str | None = None
    index = 0
    global_values = {
        "--config": "config",
        "--auth": "auth",
        "-c": "oauth",
        "--client-oauth-config": "oauth",
    }
    while index < len(args) and args[index] != "execution":
        token = args[index]
        key, separator, inline_value = token.partition("=")
        destination = global_values.get(key)
        if destination is None:
            return None
        if separator:
            value = inline_value
        else:
            index += 1
            if index >= len(args):
                return None
            value = args[index]
        if not value:
            return None
        if destination == "config":
            config_path = value
        elif destination == "auth":
            auth_provider = value.lower()
        else:
            oauth_config_path = value
        index += 1

    if auth_provider not in {"oauth2", "adc"}:
        return None
    if args[index : index + 2] != ["execution", "status"]:
        return None
    index += 2
    execution_id: str | None = None
    include: list[str] = []
    output_format: str | None = None
    while index < len(args):
        token = args[index]
        if token in {"--format", "--include"}:
            index += 1
            if index >= len(args):
                return None
            value = args[index]
            if token == "--format":
                output_format = value
            else:
                include.append(value)
        elif token.startswith("--format="):
            output_format = token.split("=", 1)[1]
        elif token.startswith("--include="):
            include.append(token.split("=", 1)[1])
        elif token.startswith("-") or execution_id is not None:
            return None
        else:
            execution_id = token
        index += 1

    if execution_id is None or (output_format or "").lower() != "json":
        return None
    return {
        "execution_id": execution_id,
        "include": include,
        "config_path": config_path,
        "auth_provider": auth_provider,
        "oauth_config_path": oauth_config_path,
    }


def _run_fast_status(
    request: dict[str, Any],
    *,
    client_type=None,
) -> int:
    from better_colab.errors import BetterColabError
    from better_colab.protocol import (
        render_error_bytes,
        render_success_bytes,
        write_response,
    )

    if client_type is None:
        from better_colab.client import BetterColabClient

        client_type = BetterColabClient
    try:
        with client_type(
            config_path=request["config_path"],
            auth_provider=request["auth_provider"],
            oauth_config_path=request["oauth_config_path"],
        ) as client:
            result = client.execution_status(
                request["execution_id"],
                include=request["include"],
            )
        data, render_exit = render_success_bytes(result)
        exit_code = int(render_exit or 0)
    except BetterColabError as error:
        data, error_exit = render_error_bytes(error.error, error.exit_code)
        exit_code = int(error_exit)
    write_response(data)
    return exit_code


def main() -> None:
    request = _parse_fast_status(sys.argv[1:])
    if request is not None:
        exit_code = _run_fast_status(request)
        if exit_code:
            raise SystemExit(exit_code)
        return

    from better_colab.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
