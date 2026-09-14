"""Authenticated duplex channel over Native Messaging streams."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import BinaryIO

from ._base import BridgeDirectionError, BridgePeer
from .authentication import BridgeAuthenticator
from .message import BridgeMessage
from .native_codec import NativeMessageCodec


@dataclass(slots=True)
class AuthenticatedNativeMessagingChannel:
    """Authenticated duplex channel for Nika-initiated or Autopilot-initiated work."""

    input_stream: BinaryIO
    output_stream: BinaryIO
    authenticator: BridgeAuthenticator
    local_peer: BridgePeer
    remote_peer: BridgePeer
    codec: NativeMessageCodec = field(default_factory=NativeMessageCodec)

    def __post_init__(self) -> None:
        if type(self.local_peer) is not BridgePeer or type(self.remote_peer) is not BridgePeer:
            raise ValueError("bridge peers must use canonical BridgePeer values")
        if self.local_peer is self.remote_peer:
            raise ValueError("local and remote bridge peers must differ")

    def receive(self) -> BridgeMessage | None:
        message = self.codec.read(self.input_stream)
        if message is None:
            return None
        self.authenticator.verify(message)
        if message.sender is not self.remote_peer:
            raise BridgeDirectionError("authenticated bridge message came from the wrong peer")
        return message

    def send(self, message: BridgeMessage) -> BridgeMessage:
        if message.sender is not self.local_peer:
            raise BridgeDirectionError("cannot send a message owned by the remote peer")
        signed = self.authenticator.sign(message)
        self.codec.write(self.output_stream, signed)
        return signed
