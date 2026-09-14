"""Structural ports used by bridge hosts and durable replay composition."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from .message import BridgeMessage
from .reply import BridgeReply


class BridgeRequestHandler(Protocol):
    def handle(self, message: BridgeMessage) -> BridgeReply: ...


class DurableIdempotencyRecord(Protocol):
    status: object
    result: Mapping[str, object] | None


class DurableIdempotencyLedger(Protocol):
    """Structural port implemented by Nika's canonical runtime IdempotencyLedger."""

    def reserve_once(
        self,
        *,
        operation_key: str,
        task_id: str,
        operation_type: str,
        input_fingerprint: str,
    ) -> tuple[DurableIdempotencyRecord, bool]: ...

    def complete(
        self,
        operation_key: str,
        result: Mapping[str, object] | None = None,
    ) -> DurableIdempotencyRecord: ...

    def mark_uncertain(self, operation_key: str) -> DurableIdempotencyRecord: ...
