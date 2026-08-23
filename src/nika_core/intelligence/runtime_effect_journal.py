from __future__ import annotations

import hashlib
import json

from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicEffectConflictError,
    DeterministicEffectReservation,
    DeterministicEffectStatus,
)
from nika_core.runtime.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
    IdempotencyStatus,
)

_OPERATION_TYPE = "deterministic.tool_action"


class RuntimeIdempotencyEffectJournal:
    """Adapt Nika's existing runtime idempotency ledger to deterministic tool actions."""

    def __init__(self, ledger: IdempotencyLedger) -> None:
        self._ledger = ledger

    def unresolved_operation_keys(self, *, task_id: str) -> tuple[str, ...]:
        if not task_id.strip():
            raise ValueError("task_id must not be empty")
        return tuple(
            record.operation_key
            for record in self._ledger.list_for_task(task_id)
            if record.status in {IdempotencyStatus.PENDING, IdempotencyStatus.UNCERTAIN}
        )

    def reserve(
        self,
        *,
        task_id: str,
        action: DeterministicAction,
    ) -> DeterministicEffectReservation:
        if not task_id.strip():
            raise ValueError("task_id must not be empty")
        if action.tool_id is None:
            raise ValueError("durable effect reservation requires a tool action")

        operation_key = self._operation_key(
            task_id=task_id,
            action_id=action.action_id,
        )
        fingerprint = self._action_fingerprint(action)
        try:
            record, created = self._ledger.reserve_once(
                operation_key=operation_key,
                task_id=task_id,
                operation_type=_OPERATION_TYPE,
                input_fingerprint=fingerprint,
            )
        except IdempotencyConflictError as exc:
            raise DeterministicEffectConflictError(
                "deterministic action effect identity conflicts with durable evidence"
            ) from exc
        return DeterministicEffectReservation(
            operation_key=operation_key,
            status=DeterministicEffectStatus(record.status.value),
            created=created,
        )

    def complete(self, operation_key: str) -> None:
        self._ledger.complete(operation_key)

    def mark_uncertain(self, operation_key: str) -> None:
        self._ledger.mark_uncertain(operation_key)

    def release_pending(self, operation_key: str) -> None:
        self._ledger.release_pending(operation_key)

    @staticmethod
    def _operation_key(*, task_id: str, action_id: str) -> str:
        identity = json.dumps(
            {
                "action_id": action_id,
                "task_id": task_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"deterministic:{hashlib.sha256(identity).hexdigest()}"

    @staticmethod
    def _action_fingerprint(action: DeterministicAction) -> str:
        payload = {
            "action_id": action.action_id,
            "adds": sorted(action.adds),
            "arguments": action.arguments,
            "forbids": sorted(action.forbids),
            "removes": sorted(action.removes),
            "requires": sorted(action.requires),
            "tool_id": action.tool_id,
        }
        try:
            encoded = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "durable deterministic tool arguments must be JSON-compatible"
            ) from exc
        return hashlib.sha256(encoded).hexdigest()


__all__ = ["RuntimeIdempotencyEffectJournal"]
