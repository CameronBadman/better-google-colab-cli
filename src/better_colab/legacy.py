"""JSON v1 adapters for retained flat upstream commands."""

from __future__ import annotations

from typing import Any

import typer

from better_colab.errors import ExitCode
from better_colab.models import ErrorDetail
from better_colab.protocol import (
    render_error_bytes,
    render_success_bytes,
    write_response,
)


def wants_json(output_format: str) -> bool:
    normalized = output_format.lower()
    if normalized == "json":
        return True
    if normalized == "text":
        return False
    typer.echo("format must be 'text' or 'json'", err=True)
    raise typer.Exit(code=int(ExitCode.USAGE))


def emit_success(result: Any) -> None:
    data, exit_code = render_success_bytes(result)
    write_response(data)
    if exit_code:
        raise typer.Exit(code=int(exit_code))


def emit_error(
    code: str,
    message: str,
    *,
    exit_code: ExitCode,
    retryable: bool,
    suggested_action: str,
    details: dict[str, Any] | None = None,
) -> None:
    error = ErrorDetail(
        code=code,
        message=message,
        retryable=retryable,
        suggested_action=suggested_action,
        details=details,
    )
    data, resolved_exit = render_error_bytes(error, exit_code)
    write_response(data)
    raise typer.Exit(code=int(resolved_exit))


def resolve_session(state, requested: str | None) -> str:
    """Resolve a legacy session without printing human-only diagnostics."""
    if requested:
        return requested
    sessions = state.store.list()
    if len(sessions) == 1:
        return next(iter(sessions))
    if not sessions:
        emit_error(
            "SESSION_NOT_FOUND",
            "No active sessions found",
            exit_code=ExitCode.NOT_FOUND,
            retryable=False,
            suggested_action="ensure_session",
        )
    emit_error(
        "SESSION_REQUIRED",
        "Multiple active sessions found; specify --session",
        exit_code=ExitCode.USAGE,
        retryable=False,
        suggested_action="specify_session",
        details={"sessions": sorted(sessions)},
    )
    raise AssertionError("emit_error always exits")


def session_result(session, *, status: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": session.name,
        "endpoint": session.endpoint,
        "hardware": (
            "CPU" if session.accelerator == "NONE" else session.accelerator
        ),
        "variant": session.variant,
    }
    if status is not None:
        result["status"] = status
    return result
