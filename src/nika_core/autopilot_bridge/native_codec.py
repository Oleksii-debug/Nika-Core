"""Bounded Chrome Native Messaging frame codec."""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO

from ._base import MAX_NATIVE_MESSAGE_BYTES, BridgeFrameError
from .message import BridgeMessage


@dataclass(slots=True)
class NativeMessageCodec:
    """Chrome Native Messaging 4-byte little-endian length framing with a hard byte bound."""

    maximum_message_bytes: int = MAX_NATIVE_MESSAGE_BYTES

    def __post_init__(self) -> None:
        if (
            type(self.maximum_message_bytes) is not int
            or not 1 <= self.maximum_message_bytes <= (2**32 - 1)
        ):
            raise ValueError("maximum_message_bytes must be an exact bounded positive integer")

    def read(self, stream: BinaryIO) -> BridgeMessage | None:
        header = stream.read(4)
        if header == b"":
            return None
        if type(header) is not bytes or len(header) != 4:
            raise BridgeFrameError("truncated or invalid Native Messaging frame header")
        size = int.from_bytes(header, byteorder="little", signed=False)
        if size < 2 or size > self.maximum_message_bytes:
            raise BridgeFrameError("Native Messaging frame length is outside the configured bound")
        body = self._read_exact(stream, size)
        return BridgeMessage.from_json_bytes(body)

    def write(self, stream: BinaryIO, message: BridgeMessage) -> None:
        body = message.to_json_bytes()
        if len(body) > self.maximum_message_bytes:
            raise BridgeFrameError("encoded Native Messaging frame exceeds the configured bound")
        self._write_all(
            stream,
            len(body).to_bytes(4, byteorder="little", signed=False),
        )
        self._write_all(stream, body)
        flush = getattr(stream, "flush", None)
        if callable(flush):
            flush()

    @staticmethod
    def _read_exact(stream: BinaryIO, size: int) -> bytes:
        remaining = size
        chunks: list[bytes] = []
        while remaining:
            chunk = stream.read(remaining)
            if not chunk:
                raise BridgeFrameError("truncated Native Messaging frame body")
            if type(chunk) is not bytes:
                raise BridgeFrameError("Native Messaging stream returned non-bytes data")
            if len(chunk) > remaining:
                raise BridgeFrameError("Native Messaging stream exceeded the requested frame size")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _write_all(stream: BinaryIO, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            written = stream.write(data[offset:])
            if written is None:
                # Buffered file objects may legally report None while accepting the whole chunk.
                return
            if type(written) is not int or written <= 0 or written > len(data) - offset:
                raise BridgeFrameError("Native Messaging stream failed a bounded frame write")
            offset += written
