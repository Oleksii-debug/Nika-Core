from __future__ import annotations

import hashlib
import json
from threading import Lock

from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicEffectConflictError,
    DeterministicEffectReservation,
    DeterministicEffectStatus,
)
from nika_core.runtime.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
    IdempotencyRecord,
    IdempotencyStatus,
)

_OPERATION_TYPE = "deterministic.tool_action"
_UNRESOLVED = frozenset({IdempotencyStatus.PENDING, IdempotencyStatus.UNCERTAIN})


class RuntimeIdempotencyEffectJournal:
    """Adapt Nika's existing runtime idempotency ledger to deterministic tool actions."""

    def __init__(self, ledger: IdempotencyLedger) -> None:
        self._ledger = ledger
        self._reservation_lock = Lock()
        self._owned_reservations: dict[str, tuple[str, str, str]] = {}

    def unresolved_operation_keys(self, *, task_id: str) -> tuple[str, ...]:
        if not task_id.strip():
            raise ValueError("task_id must not be empty")
        return tuple(
            record.operation_key
            for record in self._unresolved_records(task_id)
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
        for record in self._unresolved_records(task_id):
            if record.operation_key != operation_key:
                return self._blocked_reservation(record)

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

        if created:
            competing = tuple(
                item
                for item in self._unresolved_records(task_id)
                if item.operation_key != operation_key
            )
            if competing:
                # No handler has run yet. Drop only our own exact PENDING reservation and block
                # behind the concurrently visible task effect instead of allowing two mutations
                # to race or deleting a reservation that was rebound after our insert.
                try:
                    self._ledger.release_pending_if_matches(
                        operation_key=operation_key,
                        task_id=record.task_id,
                        operation_type=record.operation_type,
                        input_fingerprint=record.input_fingerprint,
                    )
                except (IdempotencyConflictError, KeyError) as exc:
                    raise DeterministicEffectConflictError(
                        "deterministic action reservation changed before safe release"
                    ) from exc
                return self._blocked_reservation(competing[0])
            self._remember_reservation(record)

        return DeterministicEffectReservation(
            operation_key=operation_key,
            status=DeterministicEffectStatus(record.status.value),
            created=created,
        )

    def complete(self, operation_key: str) -> None:
        task_id, operation_type, fingerprint = self._owned_identity(operation_key)
        try:
            self._ledger.complete_pending_if_matches(
                operation_key=operation_key,
                task_id=task_id,
                operation_type=operation_type,
                input_fingerprint=fingerprint,
            )
        except (IdempotencyConflictError, KeyError) as exc:
            raise DeterministicEffectConflictError(
                "deterministic action reservation changed before completion"
            ) from exc
        self._forget_reservation(operation_key)

    def mark_uncertain(self, operation_key: str) -> None:
        task_id, operation_type, fingerprint = self._owned_identity(operation_key)
        try:
            self._ledger.mark_pending_uncertain_if_matches(
                operation_key=operation_key,
                task_id=task_id,
                operation_type=operation_type,
                input_fingerprint=fingerprint,
            )
        except (IdempotencyConflictError, KeyError) as exc:
            raise DeterministicEffectConflictError(
                "deterministic action reservation changed before uncertainty recording"
            ) from exc
        self._forget_reservation(operation_key)

    def release_pending(self, operation_key: str) -> None:
        task_id, operation_type, fingerprint = self._owned_identity(operation_key)
        try:
            self._ledger.release_pending_if_matches(
                operation_key=operation_key,
                task_id=task_id,
                operation_type=operation_type,
                input_fingerprint=fingerprint,
            )
        except (IdempotencyConflictError, KeyError) as exc:
            raise DeterministicEffectConflictError(
                "deterministic action reservation changed before safe release"
            ) from exc
        self._forget_reservation(operation_key)

    def _remember_reservation(self, record: IdempotencyRecord) -> None:
        identity = (record.task_id, record.operation_type, record.input_fingerprint)
        with self._reservation_lock:
            self._owned_reservations[record.operation_key] = identity

    def _owned_identity(self, operation_key: str) -> tuple[str, str, str]:
        with self._reservation_lock:
            identity = self._owned_reservations.get(operation_key)
        if identity is None:
            raise DeterministicEffectConflictError(
                "deterministic action finalization lacks reservation authority"
            )
        return identity

    def _forget_reservation(self, operation_key: str) -> None:
        with self._reservation_lock:
            self._owned_reservations.pop(operation_key, None)

    def _unresolved_records(self, task_id: str):
        return tuple(
            record
            for record in self._ledger.list_for_task(task_id)
            if record.status in _UNRESOLVED
        )

    @staticmethod
    def _blocked_reservation(record) -> DeterministicEffectReservation:
        return DeterministicEffectReservation(
            operation_key=record.operation_key,
            status=DeterministicEffectStatus(record.status.value),
            created=False,
        )

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
            "arguments": dict(action.arguments),
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