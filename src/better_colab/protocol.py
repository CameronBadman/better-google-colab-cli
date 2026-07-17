"""JSON schema v1 envelopes, pagination, and byte-budget enforcement."""

from __future__ import annotations

import json
import re
import sys
from enum import Enum
from typing import Any, Iterable, Sequence, TypeVar

from better_colab.errors import ExitCode
from better_colab.models import ErrorDetail, PublicModel


SCHEMA_VERSION = 1
INTERNAL_PROTOCOL_VERSION = 1
MAX_RESPONSE_BYTES = 262_144
DEFAULT_OUTPUT_PAGE_BYTES = 65_536
DEFAULT_EXECUTION_LIMIT = 20
DEFAULT_NOTEBOOK_CELL_LIMIT = 100
MAX_COLLECTION_LIMIT = 100
MAX_CONTROLLER_REQUEST_BYTES = 16 * 1024 * 1024

T = TypeVar("T")
_CURSOR_PATTERN = re.compile(r"^c1_([0-9a-z]+)$")


def _to_wire(value: Any) -> Any:
    if isinstance(value, PublicModel):
        return value.to_wire()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {
            str(key): _to_wire(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, (list, tuple)):
        return [_to_wire(item) for item in value]
    return value


def success_envelope(result: Any) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "result": _to_wire(result),
    }


def error_envelope(error: ErrorDetail) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "error": error.to_wire(),
    }


def _encode(envelope: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _response_too_large_bytes(max_bytes: int) -> bytes:
    error = ErrorDetail(
        code="RESPONSE_TOO_LARGE",
        message=f"Serialized response exceeds the {max_bytes}-byte limit",
        retryable=True,
        suggested_action="request_a_smaller_page",
        details={"max_bytes": max_bytes},
    )
    bounded = _encode(error_envelope(error))
    if len(bounded) > max_bytes:
        # Production's cap easily fits the stable error. This branch keeps a
        # caller-supplied test/debug cap from ever leaking the original data.
        return b'{"schema_version":1,"ok":false,"error":{"code":"RESPONSE_TOO_LARGE"}}\n'
    return bounded


def render_success_bytes(
    result: Any,
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> tuple[bytes, ExitCode]:
    encoded = _encode(success_envelope(result))
    if len(encoded) <= max_bytes:
        return encoded, ExitCode.OK

    return _response_too_large_bytes(max_bytes), ExitCode.UNAVAILABLE


def render_error_bytes(
    error: ErrorDetail,
    exit_code: ExitCode,
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> tuple[bytes, ExitCode]:
    encoded = _encode(error_envelope(error))
    if len(encoded) <= max_bytes:
        return encoded, exit_code
    return _response_too_large_bytes(max_bytes), ExitCode.UNAVAILABLE


def write_response(data: bytes) -> None:
    """Write one already-bounded response without terminal decoration."""
    sys.stdout.write(data.decode("utf-8"))


def encode_cursor(offset: int) -> str:
    if offset < 0:
        raise ValueError("cursor offsets cannot be negative")
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if offset == 0:
        encoded = "0"
    else:
        parts: list[str] = []
        value = offset
        while value:
            value, remainder = divmod(value, 36)
            parts.append(digits[remainder])
        encoded = "".join(reversed(parts))
    return f"c1_{encoded}"


def decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    match = _CURSOR_PATTERN.fullmatch(cursor)
    if match is None:
        raise ValueError("invalid cursor")
    return int(match.group(1), 36)


def page_items(
    items: Sequence[T] | Iterable[T],
    *,
    cursor: str | None,
    limit: int,
) -> tuple[list[T], str | None]:
    values = list(items)
    offset = decode_cursor(cursor)
    if offset > len(values):
        raise ValueError("cursor is beyond the collection")
    page = values[offset : offset + limit]
    next_offset = offset + len(page)
    next_cursor = encode_cursor(next_offset) if next_offset < len(values) else None
    return page, next_cursor
