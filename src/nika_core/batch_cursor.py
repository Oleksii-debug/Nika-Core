from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from nika_core.memory import MemoryScope, MemoryService
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus

_NAMESPACE = "v01.batch_cursor"
_OPERATION_TYPE = "v01.batch_target_effect"
_COMPLETION_ENVELOPE_KEY = "__nika_batch_cursor_completion_v1__"


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
        try:
            _json_copy(self.payload)
        except BatchCursorStateError as exc:
            raise ValueError(
                "target payload must satisfy bounded JSON admission"
            ) from exc
        if self.attempt_state is AttemptState.CONFIRMED:
            if self.confirmed_result is None or self.uncertain_result is not None:
                raise ValueError("confirmed target requires only confirmed_result")
        elif self.attempt_state is AttemptState.UNCERTAIN:
            if self.uncertain_result is None or self.confirmed_result is not None:
                raise ValueError("uncertain target requires only uncertain_result")
        elif self.confirmed_result is not None or self.uncertain_result is not None:
            raise ValueError("non-terminal target cannot contain result evidence")
        for name, evidence in (
            ("confirmed_result", self.confirmed_result),
            ("uncertain_result", self.uncertain_result),
        ):
            if evidence is None:
                continue
            try:
                _json_copy(evidence)
            except BatchCursorStateError as exc:
                raise ValueError(
                    f"{name} must satisfy bounded JSON admission"
                ) from exc
        return self


class ScheduledIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: IntentKind
    batch_index: StrictInt
    target_id: StrictStr
    not_before: StrictStr | None = None
    deadline_source: Literal["completion", "scheduler"] | None = None

    @model_validator(mode="after")
    def validate_deadline(self) -> ScheduledIntent:
        if self.batch_index < 0:
            raise ValueError("intent batch_index must not be negative")
        if not self.target_id.strip():
            raise ValueError("intent target_id must not be empty")
        if self.not_before is not None:
            _parse_utc(self.not_before)
        if self.kind is not IntentKind.INTER_BATCH_WAIT and self.deadline_source is not None:
            raise ValueError("only an inter-batch wait may carry deadline_source")
        if self.deadline_source == "scheduler" and self.not_before is None:
            raise ValueError("scheduler deadline source requires not_before")
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

    @field_validator("version", mode="before")
    @classmethod
    def require_exact_version_type(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("cursor version must be an exact integer")
        return value

    @model_validator(mode="after")
    def validate_plan(self) -> BatchCursorState:
        if not self.task_id.strip() or not self.cursor_id.strip():
            raise ValueError("cursor identities must not be empty")
        if self.batch_size <= 0 or self.input_count < 0 or self.ready_batch_index < 0:
            raise ValueError("invalid cursor numeric state")

        seen_ids: set[str] = set()
        input_positions: list[int] = []
        first_unfinished: TargetCursor | None = None
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
            if target.attempt_state is AttemptState.CONFIRMED:
                if first_unfinished is not None:
                    raise ValueError("confirmed targets must form a contiguous prefix")
            elif first_unfinished is None:
                first_unfinished = target
            elif target.attempt_state is not AttemptState.PENDING:
                raise ValueError("only the first unfinished target may be active")

        ordered_input_positions = sorted(input_positions)
        if (
            len(ordered_input_positions) != self.input_count
            or any(
                position != expected
                for expected, position in enumerate(ordered_input_positions)
            )
        ):
            raise ValueError("input positions do not match input_count")
        max_batch = self.targets[-1].batch_index if self.targets else 0
        if self.ready_batch_index > max_batch:
            raise ValueError("ready_batch_index exceeds plan batches")
        if first_unfinished is None:
            allowed_ready_batches = {max_batch}
        elif (
            first_unfinished.batch_index == 0
            or first_unfinished.batch_position > 0
            or first_unfinished.attempt_state is not AttemptState.PENDING
        ):
            allowed_ready_batches = {first_unfinished.batch_index}
        else:
            allowed_ready_batches = {
                first_unfinished.batch_index - 1,
                first_unfinished.batch_index,
            }
        if self.ready_batch_index not in allowed_ready_batches:
            raise ValueError("ready_batch_index is inconsistent with cursor frontier")
        if self.plan_fingerprint != _plan_fingerprint(
            self.targets,
            self.batch_size,
            self.input_count,
        ):
            raise ValueError("batch plan fingerprint mismatch")
        intent = self.next_scheduled_intent
        if first_unfinished is None:
            if intent is not None:
                raise ValueError("completed cursor must not retain a scheduled intent")
            return self
        if intent is None:
            raise ValueError("unfinished cursor must retain a scheduled intent")
        if (
            intent.target_id != first_unfinished.target_id
            or intent.batch_index != first_unfinished.batch_index
        ):
            raise ValueError("scheduled intent must reference the cursor frontier")
        if first_unfinished.attempt_state is AttemptState.UNCERTAIN:
            expected_kind = IntentKind.RECONCILE
        elif first_unfinished.batch_index > self.ready_batch_index:
            expected_kind = IntentKind.INTER_BATCH_WAIT
        else:
            expected_kind = IntentKind.TARGET
        if intent.kind is not expected_kind:
            raise ValueError("scheduled intent kind is inconsistent with cursor frontier")
        if expected_kind is not IntentKind.INTER_BATCH_WAIT and intent.not_before is not None:
            raise ValueError("only an inter-batch wait may carry not_before")
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
        self._durable_state = state.model_copy(deep=True)
        self._persistence_blocked = False

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
        batch_size = _positive_batch_size(batch_size)
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
            next_scheduled_intent=_target_intent(normalized[0]) if normalized else None,
        )
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
        targets: Sequence[BatchTargetSpec],
        batch_size: int,
    ) -> BatchCursor:
        task_id = _required("task_id", task_id)
        cursor_id = _required("cursor_id", cursor_id)
        batch_size = _positive_batch_size(batch_size)
        record = memory.get(
            scope=MemoryScope.TASK,
            owner_id=task_id,
            namespace=_NAMESPACE,
            key=cursor_id,
        )
        if record is None:
            raise KeyError(f"Unknown batch cursor: {cursor_id}")
        try:
            state = BatchCursorState.model_validate(record.value)
        except (TypeError, ValueError) as exc:
            raise BatchCursorStateError("malformed restored batch cursor state") from exc
        if state.task_id != task_id or state.cursor_id != cursor_id:
            raise BatchCursorStateError("restored batch cursor identity mismatch")
        expected_targets = _normalize_targets(
            task_id,
            cursor_id,
            targets,
            batch_size,
        )
        expected_fingerprint = _plan_fingerprint(
            expected_targets,
            batch_size,
            len(targets),
        )
        if (
            state.batch_size != batch_size
            or state.input_count != len(targets)
            or state.plan_fingerprint != expected_fingerprint
        ):
            raise BatchCursorStateError(
                "restored batch cursor does not match current workflow input"
            )

        cursor = cls(memory, ledger, state)
        if cursor._reconcile_effect_evidence():
            cursor._persist()
        return cursor

    @property
    def state(self) -> BatchCursorState:
        self._require_persistence_authority()
        return self._state.model_copy(deep=True)

    def next_target(self) -> TargetCursor | None:
        self._require_persistence_authority()
        intent = self._state.next_scheduled_intent
        if intent is None or intent.kind is not IntentKind.TARGET:
            return None
        return self._find(intent.target_id).model_copy(deep=True)

    def begin_effect(self, target_id: str) -> EffectGrant:
        self._require_persistence_authority()
        target = self._find(target_id)
        if target.attempt_state is AttemptState.CONFIRMED:
            self._require_confirmed_durable_consistency(target)
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
        self._require_persistence_authority()
        target = self._find(target_id)
        clean_result = _json_object("result", result)
        if target.attempt_state is AttemptState.CONFIRMED:
            record = self._ledger.require(target.operation_key)
            self._require_confirmed_durable_consistency(target, record=record)
            durable_result, durable_due = _decode_completion_result(record.result)
            self._require_completion_replay_match(
                clean_result,
                next_batch_not_before,
                durable_result,
                durable_due,
            )
            return
        if target.attempt_state is not AttemptState.IN_FLIGHT:
            raise BatchCursorBlockedError("only an in-flight target may be confirmed")
        record = self._ledger.require(target.operation_key)
        self._require_durable_identity(target, record)
        if record.status is IdempotencyStatus.UNCERTAIN:
            raise BatchCursorBlockedError("uncertain effect requires reconciliation")
        if record.status is IdempotencyStatus.PENDING:
            record = self._ledger.complete(
                target.operation_key,
                _completion_envelope(clean_result, next_batch_not_before),
            )
        durable_result, durable_due = _decode_completion_result(record.result)
        self._require_completion_replay_match(
            clean_result,
            next_batch_not_before,
            durable_result,
            durable_due,
        )
        self._confirm_from_durable(target, durable_result)
        self._advance(durable_due)
        self._persist()

    def mark_uncertain(self, target_id: str, evidence: dict[str, Any]) -> None:
        self._require_persistence_authority()
        target = self._find(target_id)
        record = self._ledger.require(target.operation_key)
        self._require_durable_identity(target, record)
        if target.attempt_state is AttemptState.CONFIRMED:
            self._require_confirmed_durable_consistency(target, record=record)
            return
        if record.status is IdempotencyStatus.COMPLETED:
            durable_result, durable_due = _decode_completion_result(record.result)
            self._confirm_from_durable(target, durable_result)
            self._advance(durable_due)
        else:
            clean_evidence = _json_object("evidence", evidence)
            if record.status is IdempotencyStatus.PENDING:
                self._ledger.mark_uncertain(target.operation_key)
            target.attempt_state = AttemptState.UNCERTAIN
            target.confirmed_result = None
            target.uncertain_result = clean_evidence
            self._state.next_scheduled_intent = _reconcile_intent(target)
        self._persist()

    def schedule_inter_batch_wait(self, not_before: datetime) -> None:
        self._require_persistence_authority()
        intent = self._state.next_scheduled_intent
        if intent is None or intent.kind is not IntentKind.INTER_BATCH_WAIT:
            raise BatchCursorBlockedError("cursor is not waiting between batches")
        proposed = _as_utc(not_before)
        if intent.not_before is not None:
            current = _parse_utc(intent.not_before)
            if proposed < current:
                raise BatchCursorBlockedError(
                    "inter-batch wait deadline cannot move earlier"
                )
            if proposed == current:
                return
        intent.not_before = proposed.isoformat()
        intent.deadline_source = "scheduler"
        self._persist()

    def release_inter_batch_wait(self) -> None:
        self._require_persistence_authority()
        intent = self._state.next_scheduled_intent
        if intent is None or intent.kind is not IntentKind.INTER_BATCH_WAIT:
            raise BatchCursorBlockedError("cursor is not waiting between batches")
        if intent.not_before is not None and _utc_now() < _parse_utc(intent.not_before):
            raise BatchCursorBlockedError("inter-batch wait deadline has not been reached")
        self._state.ready_batch_index = intent.batch_index
        self._state.next_scheduled_intent = _derive_intent(self._state)
        self._persist()

    def _reconcile_effect_evidence(self) -> bool:
        changed = False
        frontier_index = next(
            (
                index
                for index, target in enumerate(self._state.targets)
                if target.attempt_state is not AttemptState.CONFIRMED
            ),
            len(self._state.targets),
        )
        durable_records = []
        for index, target in enumerate(self._state.targets):
            durable = self._ledger.get(target.operation_key)
            durable_records.append(durable)
            if durable is None:
                continue
            self._require_durable_identity(target, durable)
            if target.attempt_state is AttemptState.CONFIRMED:
                self._require_confirmed_durable_consistency(
                    target,
                    record=durable,
                    target_index=index,
                    frontier_index=frontier_index,
                )
            if index > frontier_index:
                raise BatchCursorStateError(
                    "idempotency evidence exists beyond cursor execution frontier"
                )

        for target, durable in zip(self._state.targets, durable_records, strict=True):
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
            if durable.status is IdempotencyStatus.COMPLETED:
                if target.attempt_state is not AttemptState.CONFIRMED:
                    result, durable_due = _decode_completion_result(durable.result)
                    self._confirm_from_durable(target, result)
                    self._advance(durable_due)
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
            durable_result, durable_due = _decode_completion_result(result)
            self._confirm_from_durable(target, durable_result)
            self._advance(durable_due)
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
                deadline_source="completion",
            )
        else:
            self._state.next_scheduled_intent = _target_intent(next_target)

    def _require_completion_replay_match(
        self,
        replay_result: dict[str, Any],
        replay_due: datetime | None,
        durable_result: dict[str, Any],
        durable_due: datetime | None,
    ) -> None:
        if not _canonical_json_equal(replay_result, durable_result):
            raise BatchCursorStateError(
                "confirm replay result contradicts durable completion"
            )
        replay_due_value = (
            _as_utc(replay_due).isoformat()
            if replay_due is not None
            else None
        )
        durable_due_value = (
            _as_utc(durable_due).isoformat()
            if durable_due is not None
            else None
        )
        if replay_due_value != durable_due_value:
            raise BatchCursorStateError(
                "confirm replay deadline contradicts durable completion"
            )

    def _require_durable_identity(self, target: TargetCursor, record: Any) -> None:
        if (
            record.operation_key != target.operation_key
            or record.task_id != self._state.task_id
            or record.operation_type != _OPERATION_TYPE
            or record.input_fingerprint != target.input_fingerprint
        ):
            raise BatchCursorStateError("idempotency evidence belongs to different input")

    def _require_confirmed_durable_consistency(
        self,
        target: TargetCursor,
        *,
        record: Any | None = None,
        target_index: int | None = None,
        frontier_index: int | None = None,
    ) -> None:
        durable = record if record is not None else self._ledger.require(target.operation_key)
        self._require_durable_identity(target, durable)
        if durable.status is not IdempotencyStatus.COMPLETED:
            raise BatchCursorStateError(
                "confirmed cursor target contradicts idempotency evidence"
            )
        durable_result, durable_due = _decode_completion_result(durable.result)
        if not _canonical_json_equal(target.confirmed_result, durable_result):
            raise BatchCursorStateError(
                "confirmed cursor result contradicts idempotency evidence"
            )

        if target_index is None:
            target_index = next(
                index
                for index, item in enumerate(self._state.targets)
                if item is target
            )
        if frontier_index is None:
            frontier_index = next(
                (
                    index
                    for index, item in enumerate(self._state.targets)
                    if item.attempt_state is not AttemptState.CONFIRMED
                ),
                len(self._state.targets),
            )
        intent = self._state.next_scheduled_intent
        if (
            target_index == frontier_index - 1
            and intent is not None
            and intent.kind is IntentKind.INTER_BATCH_WAIT
            and intent.deadline_source != "scheduler"
        ):
            durable_not_before = (
                _as_utc(durable_due).isoformat()
                if durable_due is not None
                else None
            )
            if intent.not_before != durable_not_before:
                raise BatchCursorStateError(
                    "confirmed cursor deadline contradicts idempotency evidence"
                )

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
        target_id = _required("target_id", target_id)
        target = next(
            (item for item in self._state.targets if item.target_id == target_id),
            None,
        )
        if target is None:
            raise KeyError(f"Unknown batch target: {target_id}")
        return target

    def _require_persistence_authority(self) -> None:
        if self._persistence_blocked:
            raise BatchCursorBlockedError(
                "batch cursor persistence outcome is unknown; restore is required"
            )

    def _persist(self) -> None:
        try:
            candidate = BatchCursorState.model_validate(
                self._state.model_dump(mode="json")
            )
        except (TypeError, ValueError) as exc:
            self._state = self._durable_state.model_copy(deep=True)
            raise BatchCursorStateError("refusing to persist malformed batch cursor") from exc

        prior = self._durable_state.model_copy(deep=True)
        try:
            self._memory.put(
                scope=MemoryScope.TASK,
                owner_id=candidate.task_id,
                namespace=_NAMESPACE,
                key=candidate.cursor_id,
                value=candidate.model_dump(mode="json"),
            )
        except Exception as exc:
            try:
                record = self._memory.get(
                    scope=MemoryScope.TASK,
                    owner_id=candidate.task_id,
                    namespace=_NAMESPACE,
                    key=candidate.cursor_id,
                )
            except Exception:  # noqa: BLE001 - unreadable authority is genuinely ambiguous
                self._state = prior
                self._persistence_blocked = True
                raise BatchCursorStateError(
                    "batch cursor persistence outcome is unknown; restore is required"
                ) from exc

            if record is None:
                self._state = prior
                self._persistence_blocked = True
                raise BatchCursorStateError(
                    "batch cursor persistence outcome conflicts with durable state; "
                    "restore is required"
                ) from exc
            try:
                persisted = BatchCursorState.model_validate(record.value)
            except (TypeError, ValueError):
                self._state = prior
                self._persistence_blocked = True
                raise BatchCursorStateError(
                    "batch cursor persistence outcome conflicts with durable state; "
                    "restore is required"
                ) from exc

            if _canonical_json_equal(
                persisted.model_dump(mode="json"),
                candidate.model_dump(mode="json"),
            ):
                self._state = candidate
                self._durable_state = candidate.model_copy(deep=True)
                self._persistence_blocked = False
            elif _canonical_json_equal(
                persisted.model_dump(mode="json"),
                prior.model_dump(mode="json"),
            ):
                self._state = prior
                self._durable_state = prior.model_copy(deep=True)
                self._persistence_blocked = False
            else:
                self._state = prior
                self._persistence_blocked = True
                raise BatchCursorStateError(
                    "batch cursor persistence outcome conflicts with durable state; "
                    "restore is required"
                ) from exc
            raise

        self._state = candidate
        self._durable_state = candidate.model_copy(deep=True)
        self._persistence_blocked = False


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
        preserve_wait = (
            existing is not None
            and existing.kind is IntentKind.INTER_BATCH_WAIT
            and existing.target_id == target.target_id
        )
        due = existing.not_before if preserve_wait else None
        deadline_source = existing.deadline_source if preserve_wait else None
        return ScheduledIntent(
            kind=IntentKind.INTER_BATCH_WAIT,
            batch_index=target.batch_index,
            target_id=target.target_id,
            not_before=due,
            deadline_source=deadline_source,
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


def _completion_envelope(
    result: dict[str, Any],
    next_batch_not_before: datetime | None,
) -> dict[str, Any]:
    due = (
        _as_utc(next_batch_not_before).isoformat()
        if next_batch_not_before is not None
        else None
    )
    return {
        _COMPLETION_ENVELOPE_KEY: {
            "result": _json_copy(result),
            "next_batch_not_before": due,
        }
    }


def _has_exact_string_keys(
    value: dict[Any, Any],
    expected: set[str],
) -> bool:
    keys: list[str] = []
    for key in value:
        if type(key) is not str:
            return False
        keys.append(key)
    return set(keys) == expected


def _decode_completion_result(
    raw: Any,
) -> tuple[dict[str, Any], datetime | None]:
    if raw is None:
        raise BatchCursorStateError("completed effect is missing durable result")
    if type(raw) is not dict:
        raise BatchCursorStateError("completed effect result is malformed")

    has_envelope_key = any(
        type(key) is str and key == _COMPLETION_ENVELOPE_KEY for key in raw
    )
    if has_envelope_key and not _has_exact_string_keys(
        raw, {_COMPLETION_ENVELOPE_KEY}
    ):
        raise BatchCursorStateError("completed effect envelope is ambiguous")

    if _has_exact_string_keys(raw, {_COMPLETION_ENVELOPE_KEY}):
        envelope = raw[_COMPLETION_ENVELOPE_KEY]
        if type(envelope) is not dict or not _has_exact_string_keys(
            envelope,
            {"result", "next_batch_not_before"},
        ):
            raise BatchCursorStateError("completed effect envelope is malformed")
        result = envelope["result"]
        due = envelope["next_batch_not_before"]
        if type(result) is not dict:
            raise BatchCursorStateError("completed effect result is malformed")
        if due is None:
            parsed_due = None
        elif isinstance(due, str):
            try:
                parsed_due = _parse_utc(due)
            except (TypeError, ValueError) as exc:
                raise BatchCursorStateError(
                    "completed effect wake deadline is malformed"
                ) from exc
        else:
            raise BatchCursorStateError("completed effect wake deadline is malformed")
        return _durable_json_object(result), parsed_due

    # Backward compatibility for already-durable pre-envelope V0.1 records.
    return _durable_json_object(raw), None


def _durable_json_object(value: dict[str, Any]) -> dict[str, Any]:
    try:
        copied = _json_copy(value)
    except BatchCursorStateError as exc:
        raise BatchCursorStateError("completed effect result is malformed") from exc
    if type(copied) is not dict:
        raise BatchCursorStateError("completed effect result is malformed")
    return copied


_MAX_VALUE_BYTES = 1_048_576
_MAX_VALUE_NODES = 10_000
_MAX_VALUE_DEPTH = 32
_MAX_INTEGER_BITS = 4_096


def _json_copy(value: Any) -> Any:
    copied = _public_json_value(value)
    try:
        serialized = _canonical_json(copied)
        encoded_size = len(serialized.encode("utf-8", errors="strict"))
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError, OverflowError) as exc:
        raise BatchCursorStateError(
            "batch cursor values must be JSON-serializable"
        ) from exc
    if encoded_size > _MAX_VALUE_BYTES:
        raise BatchCursorStateError("batch cursor values must be JSON-serializable")
    return copied


def _public_json_value(
    value: Any,
    *,
    active_containers: set[int] | None = None,
    budget: dict[str, int] | None = None,
    depth: int = 0,
) -> Any:
    budget = budget if budget is not None else {"nodes": 0, "text_bytes": 0}
    budget["nodes"] += 1
    if budget["nodes"] > _MAX_VALUE_NODES or depth > _MAX_VALUE_DEPTH:
        raise BatchCursorStateError("batch cursor values must be JSON-serializable")

    value_type = type(value)
    if value is None or value_type is bool:
        return value
    if value_type is int:
        if value.bit_length() > _MAX_INTEGER_BITS:
            raise BatchCursorStateError(
                "batch cursor values must be JSON-serializable"
            )
        return value
    if value_type is str:
        text = _public_utf8_text(value)
        if len(text) > _MAX_VALUE_BYTES:
            raise BatchCursorStateError(
                "batch cursor values must be JSON-serializable"
            )
        budget["text_bytes"] += len(text.encode("utf-8", errors="strict"))
        if budget["text_bytes"] > _MAX_VALUE_BYTES:
            raise BatchCursorStateError(
                "batch cursor values must be JSON-serializable"
            )
        return text
    if value_type is float:
        if not math.isfinite(value):
            raise BatchCursorStateError(
                "batch cursor values must be JSON-serializable"
            )
        return value
    if value_type not in {list, dict}:
        raise BatchCursorStateError("batch cursor values must be JSON-serializable")

    active_containers = active_containers if active_containers is not None else set()
    container_id = id(value)
    if container_id in active_containers:
        raise BatchCursorStateError("batch cursor values must be JSON-serializable")
    active_containers.add(container_id)
    try:
        if value_type is list:
            return [
                _public_json_value(
                    item,
                    active_containers=active_containers,
                    budget=budget,
                    depth=depth + 1,
                )
                for item in value
            ]
        copied_dict: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise BatchCursorStateError(
                    "batch cursor values must be JSON-serializable"
                )
            clean_key = _public_utf8_text(key)
            if len(clean_key) > _MAX_VALUE_BYTES:
                raise BatchCursorStateError(
                    "batch cursor values must be JSON-serializable"
                )
            budget["text_bytes"] += len(
                clean_key.encode("utf-8", errors="strict")
            )
            if budget["text_bytes"] > _MAX_VALUE_BYTES:
                raise BatchCursorStateError(
                    "batch cursor values must be JSON-serializable"
                )
            copied_dict[clean_key] = _public_json_value(
                item,
                active_containers=active_containers,
                budget=budget,
                depth=depth + 1,
            )
        return copied_dict
    except RecursionError as exc:
        raise BatchCursorStateError(
            "batch cursor values must be JSON-serializable"
        ) from exc
    finally:
        active_containers.remove(container_id)


def _json_object(name: str, value: dict[str, Any]) -> dict[str, Any]:
    if type(value) is not dict:
        raise TypeError(f"{name} must be an exact JSON object")
    copied = _json_copy(value)
    if type(copied) is not dict:
        raise BatchCursorStateError(f"{name} must remain a JSON object")
    return copied


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_json_equal(left: Any, right: Any) -> bool:
    try:
        return _canonical_json(left) == _canonical_json(right)
    except (TypeError, ValueError):
        return False


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _required(name: str, value: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be an exact string")
    result = value.strip()
    if not result:
        raise ValueError(f"{name} must not be empty")
    try:
        result.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8 text") from exc
    return result


def _public_utf8_text(value: str) -> str:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise BatchCursorStateError(
            "batch cursor values must be valid UTF-8 JSON text"
        ) from exc
    return value


def _positive_batch_size(value: int) -> int:
    if type(value) is not int:
        raise TypeError("batch_size must be an exact integer")
    if value <= 0:
        raise ValueError("batch_size must be greater than zero")
    return value


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if type(value) is not datetime:
        raise TypeError("datetime must be an exact datetime")
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    if type(value.tzinfo) is not timezone:
        raise TypeError("datetime timezone must be canonical")
    return value.astimezone(UTC)


def _parse_utc(value: str) -> datetime:
    if type(value) is not str:
        raise TypeError("scheduled intent datetime must be an exact string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("scheduled intent datetime must be timezone-aware")
    normalized = parsed.astimezone(UTC)
    if normalized.isoformat() != value:
        raise ValueError("scheduled intent datetime must use canonical UTC form")
    return normalized
