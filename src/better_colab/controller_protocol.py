"""Versioned length-prefixed JSON protocol for the local controller."""

from __future__ import annotations

import asyncio
import json
import socket
import struct
from typing import Any

from better_colab.protocol import MAX_CONTROLLER_REQUEST_BYTES


HEADER = struct.Struct(">I")


class ProtocolError(Exception):
    pass


class FrameTooLargeError(ProtocolError):
    pass


def encode_frame(
    payload: dict[str, Any],
    *,
    max_bytes: int = MAX_CONTROLLER_REQUEST_BYTES,
) -> bytes:
    if not isinstance(payload, dict):
        raise ProtocolError("protocol frame must be an object")
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(body) > max_bytes:
        raise FrameTooLargeError(
            f"frame is {len(body)} bytes; maximum is {max_bytes}"
        )
    return HEADER.pack(len(body)) + body


def decode_body(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ProtocolError("frame body is not valid UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise ProtocolError("protocol frame must be an object")
    return payload


async def read_frame(
    reader: asyncio.StreamReader,
    *,
    max_bytes: int = MAX_CONTROLLER_REQUEST_BYTES,
) -> dict[str, Any]:
    try:
        header = await reader.readexactly(HEADER.size)
    except asyncio.IncompleteReadError as error:
        raise EOFError from error
    length = HEADER.unpack(header)[0]
    if length > max_bytes:
        raise FrameTooLargeError(f"frame length {length} exceeds {max_bytes}")
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as error:
        raise ProtocolError("frame ended before declared length") from error
    return decode_body(body)


async def write_frame(
    writer: asyncio.StreamWriter,
    payload: dict[str, Any],
) -> None:
    writer.write(encode_frame(payload))
    await writer.drain()


def _recv_exact(connection: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise ProtocolError("connection closed before frame completed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame(
    connection: socket.socket,
    *,
    max_bytes: int = MAX_CONTROLLER_REQUEST_BYTES,
) -> dict[str, Any]:
    header = _recv_exact(connection, HEADER.size)
    length = HEADER.unpack(header)[0]
    if length > max_bytes:
        raise FrameTooLargeError(f"frame length {length} exceeds {max_bytes}")
    return decode_body(_recv_exact(connection, length))
