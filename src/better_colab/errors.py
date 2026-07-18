"""Stable error and process-exit contract."""

from enum import IntEnum

from better_colab.models import ErrorDetail


class ExitCode(IntEnum):
    OK = 0
    EXECUTION_FAILED = 1
    USAGE = 2
    AUTH = 3
    NOT_FOUND = 4
    CONFLICT = 5
    UNAVAILABLE = 6
    WAIT_TIMEOUT = 124


class BetterColabError(Exception):
    """Typed failure returned by the Python API and rendered by the CLI."""

    def __init__(self, error: ErrorDetail, exit_code: ExitCode):
        super().__init__(error.message)
        self.error = error
        self.exit_code = exit_code


def api_error(
    code: str,
    message: str,
    *,
    exit_code: ExitCode,
    retryable: bool,
    suggested_action: str,
    details: dict | None = None,
) -> BetterColabError:
    return BetterColabError(
        ErrorDetail(
            code=code,
            message=message,
            retryable=retryable,
            suggested_action=suggested_action,
            details=details,
        ),
        exit_code,
    )
