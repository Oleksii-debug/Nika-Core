"""Bounded process-lifetime replay guard for authenticated bridge dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field

from ._base import (
    DEFAULT_REPLAY_CAPACITY,
    BridgePeer,
    BridgeProtocolError,
    BridgeReplayCapacityError,
    BridgeReplayConflictError,
    BridgeReplayUncertainError,
    _ReplayStatus,
)
from .message import BridgeMessage
from .reply import BridgeReply


@dataclass(slots=True)
class _ReplayRecord:
    fingerprint: str
    status: _ReplayStatus = _ReplayStatus.PENDING
    reply: BridgeReply | None = None


@dataclass(slots=True)
class BridgeReplayGuard:
    """Bounded process-lifetime dedupe; restart-safe effects still require Nika's durable ledger."""

    capacity: int = DEFAULT_REPLAY_CAPACITY
    _records: dict[tuple[BridgePeer, str], _ReplayRecord] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if type(self.capacity) is not int or not 1 <= self.capacity <= 100_000:
            raise ValueError("replay capacity must be an exact bounded positive integer")

    def begin(self, message: BridgeMessage) -> BridgeReply | None:
        key = (message.sender, message.idempotency_key)
        fingerprint = message.logical_fingerprint
        existing = self._records.get(key)
        if existing is None:
            if len(self._records) >= self.capacity:
                raise BridgeReplayCapacityError("bridge replay guard capacity is exhausted")
            self._records[key] = _ReplayRecord(fingerprint=fingerprint)
            return None
        if existing.fingerprint != fingerprint:
            raise BridgeReplayConflictError("idempotency key belongs to different logical input")
        if existing.status is _ReplayStatus.PENDING:
            raise BridgeReplayUncertainError("prior bridge attempt requires reconciliation")
        if existing.reply is None:  # pragma: no cover - internal invariant
            raise RuntimeError("completed replay record has no reply")
        return existing.reply

    def complete(self, message: BridgeMessage, reply: BridgeReply) -> None:
        if type(reply) is not BridgeReply:
            raise BridgeProtocolError("handler must return exact BridgeReply")
        key = (message.sender, message.idempotency_key)
        record = self._records.get(key)
        if record is None or record.fingerprint != message.logical_fingerprint:
            raise BridgeReplayConflictError("bridge replay record changed before completion")
        if record.status is not _ReplayStatus.PENDING:
            raise BridgeReplayConflictError("bridge replay record is already completed")
        record.reply = reply
        record.status = _ReplayStatus.COMPLETED
