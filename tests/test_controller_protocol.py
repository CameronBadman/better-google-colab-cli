import asyncio
import json
import struct

import pytest

from better_colab.controller_protocol import (
    FrameTooLargeError,
    ProtocolError,
    encode_frame,
    read_frame,
)
from better_colab.protocol import (
    INTERNAL_PROTOCOL_VERSION,
    MAX_CONTROLLER_REQUEST_BYTES,
)


async def _decode(data: bytes):
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return await read_frame(reader)


def test_length_prefixed_protocol_round_trips_compact_json():
    payload = {
        "protocol_version": INTERNAL_PROTOCOL_VERSION,
        "request_id": "request-one",
        "method": "hello",
        "params": {},
    }
    encoded = encode_frame(payload)

    decoded = asyncio.run(_decode(encoded))

    assert decoded == payload
    body_length = struct.unpack(">I", encoded[:4])[0]
    assert body_length == len(encoded) - 4
    assert b" " not in encoded[4:]


def test_protocol_rejects_oversized_frames_before_reading_body():
    with pytest.raises(FrameTooLargeError):
        asyncio.run(
            _decode(struct.pack(">I", MAX_CONTROLLER_REQUEST_BYTES + 1))
        )


def test_protocol_rejects_non_object_json():
    body = json.dumps(["not", "an", "object"]).encode()

    with pytest.raises(ProtocolError, match="object"):
        asyncio.run(_decode(struct.pack(">I", len(body)) + body))
