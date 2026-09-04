from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from nika_core.memory import MemoryScope, MemoryService
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus

_NAMESPACE = "v01.batch_cursor"
_OPERATION_TYPE = "v01.batch_target_effect"


class BatchCursorStateError(RuntimeError):
    """Persisted cursor state is malformed or contradicts durable effect evidence."""


class BatchCursorBlockedError(RuntimeError):
    """Automatic progress is unsafe until the durable blocker is resolved."""


class AttemptState(StrEnum):
    PENDING = "pending"
    PREPARED = "prepared"
    IN_FLIGHT = "in_flight"
    CONFIRMED = "confirmed"
    UNCERTAIN = "uncertain"


class IntentKind(StrEnum):
    TARGET = "target"
    INTER_BATCH_WAIT = "inter_batch_wait"
    RECONCILE = "reconcile"


class BatchTargetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: StrictStr
    payload: dict[str, Any] = Field(default_factory=dict)


class TargetCursor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: StrictStr
    payload: dict[str, Any]
    position: StrictInt
    batch_index: StrictInt
    batch_position: StrictInt
    input_positions: list[StrictInt]
    input_fingerprint: StrictStr
    operation_key: StrictStr
    attempt_state: AttemptState = AttemptState.PENDING
    attempts: StrictInt = 0
    confirmed_result: dict[str, Any] | None = None
    uncertain_result: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_terminal_evidence(self) -> TargetCursor:
        if self.attempts < 0:
            raise ValueError("attempts must not be negative")
        if self.attempt_state is AttemptState.CONFIRMED:
            if self.confirmed_result is None or self.uncertain_result is not None:
                raise ValueError("confirmed target requires only confirmed_result")
        elif self.attempt_state is AttemptState.UNCERTAIN:
            if self.uncertain_result is None or self.confirmed_result is not None:
                raise ValueError("uncertain target requires only uncertain_result")
        elif self.confirmed_result is not None or self.uncertain_result is not None:
            raise ValueError("non-terminal target cannot contain result evidence")
        return self


class ScheduledIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: IntentKind
    batch_index: StrictInt
    target_id: StrictStr
    not_before: StrictStr | None = None

    @model_validator(mode="after")
    def validate_deadline(self) -> ScheduledIntent:
        if self.batch_index < 0:
            raise ValueError("intent batch_index must not be negative")
        if not self.target_id.strip():
            raise ValueError("intent target_id must not be empty")
        if self.not_before is not None:
            _parse_utc(self.not_before)
        return self


class BatchCursorState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    task_id: StrictStr
    cursor_id: StrictStr
    batch_size: StrictInt
    input_count: StrictInt
    ready_batch_index: StrictInt = 0
    plan_fingerprint: StrictStr
    targets: list[TargetCursor]
    next_scheduled_intent: ScheduledIntent | None = None

    @model_validator(mode="after")
    def validate_plan(self) -> BatchCursorState:
        if not self.task_id.strip() or not self.cursor_id.strip():
            raise ValueError("cursor identities must not be empty")
        if self.batch_size <= 0 or self.input_count < 0 or self.ready_batch_index < 0:
            raise ValueError("invalid cursor numeric state")

        seen_ids: set[str] = set()
        input_positions: list[int] = []
        for index, target in enumerate(self.targets):
            if target.target_id in seen_ids:
                raise ValueError("restored cursor contains duplicate target identity")
            seen_ids.add(target.target_id)
            if target.position != index:
                raise ValueError("target positions must be contiguous")
            if target.batch_index != index // self.batch_size:
                raise ValueError("target batch_index is inconsistent")
            if target.batch_position != index % self.batch_size:
                raise ValueError("target batch_position is inconsistent")
            if (
                not target.input_positions
                or target.input_positions != sorted(set(target.input_positions))
                or any(position < 0 for position in target.input_positions)
            ):
                raise ValueError("target input_positions are malformed")
            input_positions.extend(target.input_positions)
            fingerprint = _input_fingerprint(target.target_id, target.payload)
            if target.input_fingerprint != fingerprint:
                raise ValueError("target input fingerprint mismatch")
            expected_key = _operation_key(
                self.task_id,
                self.cursor_id,
                target.target_id,
                fingerprint,
            )
            if target.operation_key != expected_key:
                raise ValueError("target operation key mismatch")

        if sorted(input_positions) != list(range(self.input_count)):
            raise ValueError("input positions do not match input_count")
        max_batch = self.targets[-1].batch_index if self.targets else 0
        if self.ready_batch_index > max_batch:
            raise ValueError("ready_batch_index exceeds plan batches")
        if self.plan_fingerprint != _plan_fingerprint(
            self.targets,
            self.batch_size,
            self.input_count,
        ):
            raise ValueError("batch plan fingerprint mismatch")
        if self.next_scheduled_intent is not None:
            intent = self.next_scheduled_intent
            target = next(
                (item for item in self.targets if item.target_id == intent.target_id),
                None,
            )
            if target is None or target.batch_index != intent.batch_index:
                raise ValueError("scheduled intent target/batch identity is invalid")
        return self

    @property
    def confirmed_count(self) -> int:
        return sum(item.attempt_state is AttemptState.CONFIRMED for item in self.targets)

    @property
    def uncertain_count(self) -> int:
        return sum(item.attempt_state is AttemptState.UNCERTAIN for item in self.targets)

    @property
    def pending_count(self) -> int:
        return len(self.targets) - self.confirmed_count - self.uncertain_count


class EffectGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execute: bool
    operation_key: str
    reason: str


class BatchCursor:
    """Nika-specific batch state over existing TASK memory and idempotency storage."""

    def __init__(
        self,
        memory: MemoryService,
        ledger: IdempotencyLedger,
        state: BatchCursorState,
    ) -> None:
        self._memory = memory
        self._ledger = ledger
        self._state = state

    @classmethod
    def create(
        cls,
        memory: MemoryService,
        ledger: IdempotencyLedger,
        *,
        task_id: str,
        cursor_id: str,
        targets: Sequence[BatchTargetSpec],
        batch_size: int,
    ) -> BatchCursor:
        task_id = _required("task_id", task_id)
        cursor_id = _required("cursor_id", cursor_id)
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")
        if memory.get(
            scope=MemoryScope.TASK,
            owner_id=task_id,
            namespace=_NAMESPACE,
            key=cursor_id,
        ) is not None:
            raise BatchCursorStateError("batch cursor already exists")

        normalized = _normalize_targets(task_id, cursor_id, targets, batch_size)
        state = BatchCursorState(
            task_id=task_id,
            cursor_id=cursor_id,
            batch_size=batch_size,
            input_count=len(targets),
            plan_fingerprint=_plan_fingerprint(normalized, batch_size, len(targets)),
            targets=normalized,
        )
        state.next_scheduled_intent = _derive_intent(state)
        cursor = cls(memory, ledger, state)
        cursor._persist()
        return cursor

    @classmethod
    def restore(
        cls,
        memory: MemoryService,
        ledger: IdempotencyLedger,
        *,
        task_id: str,
        cursor_id: str,
    ) -> BatchCursor:
        record = memory.get(
            scope=MemoryScope.TASK,
            owner_id=_required("task_id", task_id),
            namespace=_NAMESPACE,
            key=_required("cursor_id", cursor_id),
        )
        if record is None:
            raise KeyError(f"Unknown batch cursor: {cursor_id}")
        try:
            state = BatchCursorState.model_validate(record.value)
        except (TypeError, ValueError) as exc:
            raise BatchCursorStateError("malformed restored batch cursor state") from exc
        if state.task_id != task_id or state.cursor_id != cursor_id:
            raise BatchCursorStateError("restored batch cursor identity mismatch")

        cursor = cls(memory, ledger, state)
        if cursor._reconcile_effect_evidence():
            cursor._persist()
        return cursor

    @property
    def state(self) -> BatchCursorState:
        return self._state.model_copy(deep=True)

    def next_target(self) -> TargetCursor | None:
        intent = self._state.next_scheduled_intent
        if intent is None or intent.kind is not IntentKind.TARGET:
            return None
        return self._find(intent.target_id).model_copy(deep=True)

    def begin_effect(self, target_id: str) -> EffectGrant:
        target = self._find(target_id)
        if target.attempt_state is AttemptState.CONFIRMED:
            return EffectGrant(
                execute=False,
                operation_key=target.operation_key,
                reason="already_confirmed",
            )
        if self._first_uncertain() is not None:
            raise BatchCursorBlockedError("cursor is blocked by uncertain external-effect state")
        if not self._is_next_target(target):
            raise BatchCursorBlockedError("target is not the next executable cursor position")
        if target.batch_index > self._state.ready_batch_index:
            raise BatchCursorBlockedError("target batch is waiting for scheduled release")
        if target.attempt_state is AttemptState.IN_FLIGHT:
            return EffectGrant(
                execute=False,
                operation_key=target.operation_key,
                reason="effect_already_in_flight",
            )

        if target.attempt_state is AttemptState.PENDING:
            target.attempt_state = AttemptState.PREPARED
            self._state.next_scheduled_intent = _target_intent(target)
            self._persist()
            target = self._find(target_id)

        record, created = self._ledger.reserve_once(
            operation_key=target.operation_key,
            task_id=self._state.task_id,
            operation_type=_OPERATION_TYPE,
            input_fingerprint=target.input_fingerprint,
        )
        if not created:
            return self._consume_existing_reservation(target, record.status, record.result)

        target.attempt_state = AttemptState.IN_FLIGHT
        target.attempts += 1
        self._state.next_scheduled_intent = _target_intent(target)
        self._persist()
        return EffectGrant(execute=True, operation_key=target.operation_key, reason="reserved")

    def confirm(
        self,
        target_id: str,
        result: dict[str, Any],
        *,
        next_batch_not_before: datetime | None = None,
    ) -> None:
        target = self._find(target_id)
        if target.attempt_state is AttemptState.CONFIRMED:
            return
        if target.attempt_state is not AttemptState.IN_FLIGHT:
            raise BatchCursorBlockedError("only an in-flight target may be confirmed")
        clean_result = _json_copy(result)
        record = self._ledger.require(target.operation_key)
        if record.status is IdempotencyStatus.UNCERTAIN:
            raise BatchCursorBlockedError("uncertain effect requires reconciliation")
        if record.status is IdempotencyStatus.PENDING:
            record = self._ledger.complete(target.operation_key, clean_result)
        self._confirm_from_durable(target, dict(record.result or clean_result))
        self._advance(next_batch_not_before)
        self._persist()

    def mark_uncertain(self, target_id: str, evidence: dict[str, Any]) -> None:
        target = self._find(target_id)
        record = self._ledger.require(target.operation_key)
        if record.status is IdempotencyStatus.COMPLETED:
            self._confirm_from_durable(target, dict(record.result or {}))
            self._advance(None)
        else:
            if record.status is IdempotencyStatus.PENDING:
                self._ledger.mark_uncertain(target.operation_key)
            target.attempt_state = AttemptState.UNCERTAIN
            target.confirmed_result = None
            target.uncertain_result = _json_copy(evidence)
            self._state.next_scheduled_intent = _reconcile_intent(target)
        self._persist()

    def schedule_inter_batch_wait(self, not_before: datetime) -> None:
        intent = self._state.next_scheduled_intent
        if intent is None or intent.kind is not IntentKind.INTER_BATCH_WAIT:
            raise BatchCursorBlockedError("cursor is not waiting between batches")
        intent.not_before = _as_utc(not_before).isoformat()
        self._persist()

    def release_inter_batch_wait(self, *, now: datetime | None = None) -> None:
        intent = self._state.next_scheduled_intent
        if intent is None or intent.kind is not IntentKind.INTER_BATCH_WAIT:
            raise BatchCursorBlockedError("cursor is not waiting between batches")
        if intent.not_before is not None:
            current = _as_utc(now) if now is not None else datetime.now(UTC)
            if current < _parse_utc(intent.not_before):
                raise BatchCursorBlockedError("inter-batch wait deadline has not been reached")
        self._state.ready_batch_index = intent.batch_index
        self._state.next_scheduled_intent = _derive_intent(self._state)
        self._persist()

    def _reconcile_effect_evidence(self) -> bool:
        changed = False
        for target in self._state.targets:
            durable = self._ledger.get(target.operation_key)
            if durable is None:
                if target.attempt_state in {
                    AttemptState.IN_FLIGHT,
                    AttemptState.CONFIRMED,
                    AttemptState.UNCERTAIN,
                }:
                    raise BatchCursorStateError(
                        "cursor terminal/in-flight state has no idempotency evidence"
                    )
                continue
            if (
                durable.task_id != self._state.task_id
                or durable.operation_type != _OPERATION_TYPE
                or durable.input_fingerprint != target.input_fingerprint
            ):
                raise BatchCursorStateError("idempotency evidence belongs to different input")
            if durable.status is IdempotencyStatus.COMPLETED:
                result = dict(durable.result or {})
                if target.attempt_state is not AttemptState.CONFIRMED or (
                    target.confirmed_result != result
                ):
                    self._confirm_from_durable(target, result)
                    changed = True
            elif durable.status is IdempotencyStatus.UNCERTAIN:
                if target.attempt_state is not AttemptState.UNCERTAIN:
                    target.attempt_state = AttemptState.UNCERTAIN
                    target.confirmed_result = None
                    target.uncertain_result = {
                        "reason": "idempotency_ledger_uncertain_after_restart"
                    }
                    changed = True
            elif target.attempt_state is AttemptState.PREPARED:
                self._ledger.release_pending(target.operation_key)
                target.attempt_state = AttemptState.PENDING
                changed = True
            else:
                self._ledger.mark_uncertain(target.operation_key)
                target.attempt_state = AttemptState.UNCERTAIN
                target.confirmed_result = None
                target.uncertain_result = {
                    "reason": "restart_with_unresolved_pending_effect"
                }
                changed = True

        derived = _derive_intent(self._state)
        if self._state.next_scheduled_intent != derived:
            self._state.next_scheduled_intent = derived
            changed = True
        return changed

    def _consume_existing_reservation(
        self,
        target: TargetCursor,
        status: IdempotencyStatus,
        result: Any,
    ) -> EffectGrant:
        if status is IdempotencyStatus.COMPLETED:
            self._confirm_from_durable(target, dict(result or {}))
            self._advance(None)
            self._persist()
            reason = "already_confirmed"
        elif status is IdempotencyStatus.UNCERTAIN:
            target.attempt_state = AttemptState.UNCERTAIN
            target.confirmed_result = None
            target.uncertain_result = {"reason": "idempotency_ledger_uncertain"}
            self._state.next_scheduled_intent = _reconcile_intent(target)
            self._persist()
            reason = "uncertain_requires_reconciliation"
        else:
            reason = "effect_already_reserved"
        return EffectGrant(execute=False, operation_key=target.operation_key, reason=reason)

    def _advance(self, next_batch_not_before: datetime | None) -> None:
        uncertain = self._first_uncertain()
        if uncertain is not None:
            self._state.next_scheduled_intent = _reconcile_intent(uncertain)
            return
        next_target = self._first_nonconfirmed()
        if next_target is None:
            self._state.next_scheduled_intent = None
        elif next_target.batch_index > self._state.ready_batch_index:
            due = (
                _as_utc(next_batch_not_before).isoformat()
                if next_batch_not_before is not None
                else None
            )
            self._state.next_scheduled_intent = ScheduledIntent(
                kind=IntentKind.INTER_BATCH_WAIT,
                batch_index=next_target.batch_index,
                target_id=next_target.target_id,
                not_before=due,
            )
        else:
            self._state.next_scheduled_intent = _target_intent(next_target)

    def _confirm_from_durable(self, target: TargetCursor, result: dict[str, Any]) -> None:
        target.attempt_state = AttemptState.CONFIRMED
        target.confirmed_result = _json_copy(result)
        target.uncertain_result = None

    def _first_nonconfirmed(self) -> TargetCursor | None:
        return next(
            (
                target
                for target in self._state.targets
                if target.attempt_state is not AttemptState.CONFIRMED
            ),
            None,
        )

    def _first_uncertain(self) -> TargetCursor | None:
        return next(
            (
                target
                for target in self._state.targets
                if target.attempt_state is AttemptState.UNCERTAIN
            ),
            None,
        )

    def _is_next_target(self, target: TargetCursor) -> bool:
        next_target = self._first_nonconfirmed()
        return next_target is not None and next_target.target_id == target.target_id

    def _find(self, target_id: str) -> TargetCursor:
        target = next(
            (item for item in self._state.targets if item.target_id == target_id),
            None,
        )
        if target is None:
            raise KeyError(f"Unknown batch target: {target_id}")
        return target

    def _persist(self) -> None:
        try:
            self._state = BatchCursorState.model_validate(
                self._state.model_dump(mode="json")
            )
        except (TypeError, ValueError) as exc:
            raise BatchCursorStateError("refusing to persist malformed batch cursor") from exc
        self._memory.put(
            scope=MemoryScope.TASK,
            owner_id=self._state.task_id,
            namespace=_NAMESPACE,
            key=self._state.cursor_id,
            value=self._state.model_dump(mode="json"),
        )


def _normalize_targets(
    task_id: str,
    cursor_id: str,
    specs: Sequence[BatchTargetSpec],
    batch_size: int,
) -> list[TargetCursor]:
    targets: list[TargetCursor] = []
    by_id: dict[str, TargetCursor] = {}
    for input_position, spec in enumerate(specs):
        target_id = _required("target_id", spec.target_id)
        payload = _json_copy(spec.payload)
        fingerprint = _input_fingerprint(target_id, payload)
        existing = by_id.get(target_id)
        if existing is not None:
            if existing.input_fingerprint != fingerprint:
                raise BatchCursorStateError(
                    f"duplicate target_id {target_id!r} has conflicting payload"
                )
            existing.input_positions.append(input_position)
            continue
        position = len(targets)
        target = TargetCursor(
            target_id=target_id,
            payload=payload,
            position=position,
            batch_index=position // batch_size,
            batch_position=position % batch_size,
            input_positions=[input_position],
            input_fingerprint=fingerprint,
            operation_key=_operation_key(
                task_id,
                cursor_id,
                target_id,
                fingerprint,
            ),
        )
        targets.append(target)
        by_id[target_id] = target
    return targets


def _derive_intent(state: BatchCursorState) -> ScheduledIntent | None:
    uncertain = next(
        (target for target in state.targets if target.attempt_state is AttemptState.UNCERTAIN),
        None,
    )
    if uncertain is not None:
        return _reconcile_intent(uncertain)
    target = next(
        (item for item in state.targets if item.attempt_state is not AttemptState.CONFIRMED),
        None,
    )
    if target is None:
        return None
    if target.batch_index > state.ready_batch_index:
        existing = state.next_scheduled_intent
        due = (
            existing.not_before
            if existing is not None
            and existing.kind is IntentKind.INTER_BATCH_WAIT
            and existing.target_id == target.target_id
            else None
        )
        return ScheduledIntent(
            kind=IntentKind.INTER_BATCH_WAIT,
            batch_index=target.batch_index,
            target_id=target.target_id,
            not_before=due,
        )
    return _target_intent(target)


def _target_intent(target: TargetCursor) -> ScheduledIntent:
    return ScheduledIntent(
        kind=IntentKind.TARGET,
        batch_index=target.batch_index,
        target_id=target.target_id,
    )


def _reconcile_intent(target: TargetCursor) -> ScheduledIntent:
    return ScheduledIntent(
        kind=IntentKind.RECONCILE,
        batch_index=target.batch_index,
        target_id=target.target_id,
    )


def _plan_fingerprint(
    targets: Sequence[TargetCursor],
    batch_size: int,
    input_count: int,
) -> str:
    body = {
        "batch_size": batch_size,
        "input_count": input_count,
        "targets": [
            {
                "target_id": target.target_id,
                "payload": target.payload,
                "position": target.position,
                "input_positions": target.input_positions,
            }
            for target in targets
        ],
    }
    return _sha256(_canonical_json(body))


def _input_fingerprint(target_id: str, payload: dict[str, Any]) -> str:
    return _sha256(_canonical_json({"target_id": target_id, "payload": payload}))


def _operation_key(
    task_id: str,
    cursor_id: str,
    target_id: str,
    input_fingerprint: str,
) -> str:
    identity = {
        "task_id": task_id,
        "cursor_id": cursor_id,
        "target_id": target_id,
        "input_fingerprint": input_fingerprint,
    }
    return f"v01-batch:{_sha256(_canonical_json(identity))}"


def _json_copy(value: Any) -> Any:
    try:
        return json.loads(_canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise BatchCursorStateError("batch cursor values must be JSON-serializable") from exc


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _required(name: str, value: str) -> str:
    result = value.strip()
    if not result:
        raise ValueError(f"{name} must not be empty")
    return result


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("scheduled intent datetime must be timezone-aware")
    return parsed.astimezone(UTC)
