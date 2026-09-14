"""Authenticated Nika <-> browser Autopilot local bridge.

Browser DOM/session execution remains owned by Autopilot. This package owns only the versioned
transport/authentication/replay contract and Nika-side dispatch seams.
"""

from ._base import (
    MAX_NATIVE_MESSAGE_BYTES,
    BridgeAuthenticationError,
    BridgeDirectionError,
    BridgeFrameError,
    BridgeMessageKind,
    BridgeOutcome,
    BridgePeer,
    BridgeProtocolError,
)
from .authentication import BridgeAuthenticator
from .channel import AuthenticatedNativeMessagingChannel
from .durable import DurableBridgeHandler
from .host import AutopilotBridgeHost
from .message import BridgeMessage
from .native_codec import NativeMessageCodec
from .replay import BridgeReplayGuard
from .reply import BridgeReply

__all__ = [
    "MAX_NATIVE_MESSAGE_BYTES",
    "AuthenticatedNativeMessagingChannel",
    "AutopilotBridgeHost",
    "BridgeAuthenticationError",
    "BridgeAuthenticator",
    "BridgeDirectionError",
    "BridgeFrameError",
    "BridgeMessage",
    "BridgeMessageKind",
    "BridgeOutcome",
    "BridgePeer",
    "BridgeProtocolError",
    "BridgeReply",
    "BridgeReplayGuard",
    "DurableBridgeHandler",
    "NativeMessageCodec",
]
