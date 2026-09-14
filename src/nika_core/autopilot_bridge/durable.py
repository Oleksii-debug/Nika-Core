"""Composition with Nika's existing durable idempotency authority."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from ._base import (
    MAX_PROVENANCE_BYTES,
    BridgeOutcome,
    BridgeProtocolError,
    _canonical_json_bytes,
    _validate_bounded_text,
    _validate_lower_hex,
)
from .message import BridgeMessage
from .ports import BridgeRequestHandler, DurableIdempotencyLedger, DurableIdempotencyRecord
from .reply import BridgeReply

_DURABLE_RECEIPT_SCHEMA = "nika.autopilot.bridge-receipt/v1"
_DURABLE_OPERATION_TYPE = "autopilot_bridge_message"


@dataclass(slots=True)
class DurableBridgeHandler:
    """Compose bridge dispatch with Nika's existing durable idempotency authority.

    The ledger receipt stores only outcome/provenance/error and a SHA-256 of the reply payload,
    not raw browser/model result content. A completed retry after process restart therefore returns
    a digest-bound completion acknowledgement instead of replaying the effect or re-persisting the
    original content. PENDING/UNCERTAIN records fail closed for explicit reconciliation.
    """

    ledger: DurableIdempotencyLedger
    delegate: BridgeRequestHandler

    def handle(self, message: BridgeMessage) -> BridgeReply:
        operation_key = self._operation_key(message)
        try:
            record, created = self.ledger.reserve_once(
                operation_key=operation_key,
                task_id=message.task_id,
                operation_type=_DURABLE_OPERATION_TYPE,
                input_fingerprint=message.logical_fingerprint,
            )
        except Exception:
            # Admission failed before the delegate was called, so no bridge-owned effect occurred.
            return BridgeReply.failed(
                "durable_admission_failed",
                provenance_ref=message.provenance_ref,
            )

        if not created:
            return self._reply_for_existing(message, record)

        try:
            reply = self.delegate.handle(message)
            if type(reply) is not BridgeReply:
                raise BridgeProtocolError("handler must return exact BridgeReply")
            receipt = self._receipt(reply)
            self.ledger.complete(operation_key, receipt)
            return reply
        except Exception:
            # If the delegate effect or durable completion boundary failed, replay is unsafe.
            try:
                self.ledger.mark_uncertain(operation_key)
            except Exception:
                # A durable PENDING reservation is itself fail-closed on the next reserve_once.
                pass
            raise

    @staticmethod
    def _operation_key(message: BridgeMessage) -> str:
        material = f"{message.sender.value}\x00{message.idempotency_key}".encode("utf-8")
        return "autopilot-bridge:" + hashlib.sha256(material).hexdigest()

    @staticmethod
    def _status_value(record: DurableIdempotencyRecord) -> str:
        status = record.status
        value = getattr(status, "value", status)
        return value if type(value) is str else "invalid"

    def _reply_for_existing(
        self,
        message: BridgeMessage,
        record: DurableIdempotencyRecord,
    ) -> BridgeReply:
        status = self._status_value(record)
        if status != "completed":
            return BridgeReply.failed(
                "reconciliation_required",
                provenance_ref=message.provenance_ref,
                payload={"durable_status": status},
            )
        receipt = record.result
        if not isinstance(receipt, Mapping) or set(receipt) != {
            "schema",
            "outcome",
            "provenance_ref",
            "error_code",
            "payload_sha256",
        }:
            return BridgeReply.failed(
                "reconciliation_required",
                provenance_ref=message.provenance_ref,
                payload={"durable_status": status},
            )
        if receipt.get("schema") != _DURABLE_RECEIPT_SCHEMA:
            return BridgeReply.failed(
                "reconciliation_required",
                provenance_ref=message.provenance_ref,
                payload={"durable_status": status},
            )
        try:
            outcome = BridgeOutcome(receipt["outcome"])
            provenance_ref = _validate_bounded_text(
                receipt["provenance_ref"],
                name="durable provenance_ref",
                maximum_bytes=MAX_PROVENANCE_BYTES,
            )
            payload_sha256 = _validate_lower_hex(
                receipt["payload_sha256"],
                name="durable payload_sha256",
                length=64,
            )
            error_code = receipt["error_code"]
            if outcome is BridgeOutcome.FAILED:
                _validate_bounded_text(error_code, name="durable error_code")
            elif error_code is not None:
                raise BridgeProtocolError("non-failed durable receipt cannot carry error_code")
        except (BridgeProtocolError, TypeError, ValueError):
            return BridgeReply.failed(
                "reconciliation_required",
                provenance_ref=message.provenance_ref,
                payload={"durable_status": status},
            )

        replay_payload = {
            "durable_replay": True,
            "payload_sha256": payload_sha256,
        }
        if outcome is BridgeOutcome.FAILED:
            return BridgeReply.failed(
                error_code,
                provenance_ref=provenance_ref,
                payload=replay_payload,
            )
        return BridgeReply(
            outcome=outcome,
            payload=replay_payload,
            provenance_ref=provenance_ref,
        )

    @staticmethod
    def _receipt(reply: BridgeReply) -> dict[str, object]:
        payload_sha256 = hashlib.sha256(
            _canonical_json_bytes(reply.payload_dict())
        ).hexdigest()
        return {
            "schema": _DURABLE_RECEIPT_SCHEMA,
            "outcome": reply.outcome.value,
            "provenance_ref": reply.provenance_ref,
            "error_code": reply.error_code,
            "payload_sha256": payload_sha256,
        }
