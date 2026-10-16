from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from typing import Protocol

from nika_core.background_life import (
    BackgroundAction,
    BackgroundDecision,
    BackgroundWorkKind,
    OwnerPresence,
    decide_background_work,
)
from nika_core.kernel.audit import AuditIntegrityError, AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.resources.manager import ResourceManager
from nika_core.runtime.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
    IdempotencyStatus,
)

_MAX_SIGNED_64 = (1 << 63) - 1
_SOURCE_ENTITY_TYPE = "owner_presence_source"
_OBSERVED_EVENT = "background.owner_presence_observed"
_BACKGROUND_PAUSED_EVENT = "background.dispatch_paused"
_OWNER_RETURN_PAUSED_EVENT = "background.running_paused_for_owner"
_DISPATCH_OPERATION_TYPE = "background.dispatch"
_RESUME_OPERATION_TYPE = "background.resume"
_MAX_IDENTITY_LENGTH = 256
_MAX_CONFIGURED_PRESENCE_AGE_SECONDS = 60.0


def _require_identity(
    value: object,
    name: str,
    *,
    maximum: int | None = _MAX_IDENTITY_LENGTH,
) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be exact built-in str")
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty without surrounding whitespace")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8 text") from exc
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{name} is too long")
    return value


class PresenceEvidencePhase(StrEnum):
    PREFLIGHT = "preflight"
    EFFECT_RECHECK = "effect_recheck"
    EFFECT_COMMIT = "effect_commit"
    POST_GRANT_FENCE = "post_grant_fence"
    EFFECT_START_FENCE = "effect_start_fence"


@dataclass(frozen=True, slots=True)
class OwnerPresenceObservation:
    """One authoritative, timestamped owner-presence sample."""

    source_id: str
    sequence: int
    presence: OwnerPresence
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_identity(self.source_id, "source_id")
        if type(self.sequence) is not int:
            raise TypeError("sequence must be exact built-in int")
        if not 0 <= self.sequence <= _MAX_SIGNED_64:
            raise ValueError("sequence is outside the supported integer range")
        if type(self.presence) is not OwnerPresence:
            raise TypeError("presence must be OwnerPresence")
        if type(self.observed_at) is not datetime:
            raise TypeError("observed_at must be exact datetime")
        if self.observed_at.tzinfo is not UTC:
            raise ValueError("observed_at must use UTC")


class OwnerPresenceObserverPort(Protocol):
    def observe(self) -> OwnerPresenceObservation: ...


@dataclass(frozen=True, slots=True)
class BackgroundDispatchResult:
    action: BackgroundAction
    reason: str
    effect_started: bool
    effect_result: object | None = None

    @property
    def executed(self) -> bool:
        return self.effect_started


class OwnerPresenceEvidenceError(RuntimeError):
    """Presence evidence was unavailable, stale, replayed, malformed or from the wrong source."""


class BackgroundDispatchGuard:
    """Fail-closed dispatch composition over existing Nika runtime authorities.

    This class does not execute a second scheduler/runtime. It gates one caller-supplied effect
    using the #824 background admission policy, canonical ResourceManager admission, TaskQueue
    pause/resume state and AuditLog evidence. The effect can be TaskRuntimeCoordinator.start().
    """

    def __init__(
        self,
        *,
        queue: TaskQueue,
        audit: AuditLog,
        resources: ResourceManager,
        presence: OwnerPresenceObserverPort,
        source_id: str,
        max_presence_age_seconds: float = 5.0,
        max_future_skew_seconds: float = 1.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        _require_identity(source_id, "source_id")
        if type(max_presence_age_seconds) not in (int, float):
            raise TypeError("max_presence_age_seconds must be exact built-in int or float")
        if type(max_presence_age_seconds) is float and not isfinite(max_presence_age_seconds):
            raise ValueError("max_presence_age_seconds must be finite")
        if not 0 < max_presence_age_seconds <= _MAX_CONFIGURED_PRESENCE_AGE_SECONDS:
            raise ValueError(
                "max_presence_age_seconds must be in "
                f"(0, {_MAX_CONFIGURED_PRESENCE_AGE_SECONDS:g}]"
            )
        if type(max_future_skew_seconds) not in (int, float):
            raise TypeError("max_future_skew_seconds must be exact built-in int or float")
        if type(max_future_skew_seconds) is float and not isfinite(max_future_skew_seconds):
            raise ValueError("max_future_skew_seconds must be finite")
        if max_future_skew_seconds < 0:
            raise ValueError("max_future_skew_seconds must be non-negative")
        if max_future_skew_seconds > max_presence_age_seconds:
            raise ValueError(
                "max_future_skew_seconds must not exceed max_presence_age_seconds"
            )
        self._queue = queue
        self._audit = audit
        self._resources = resources
        self._presence = presence
        self._source_id = source_id
        self._max_age = float(max_presence_age_seconds)
        self._max_future_skew = float(max_future_skew_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._idempotency = IdempotencyLedger(queue.store)

    async def dispatch(
        self,
        *,
        task_id: str,
        work_kind: BackgroundWorkKind,
        effect: Callable[[], Awaitable[object]],
        owner_id: str = "living-agent",
    ) -> BackgroundDispatchResult:
        """Run one background effect only after fresh preflight and effect-time evidence."""

        self._require_dispatchable_task(task_id)
        if type(work_kind) is not BackgroundWorkKind:
            raise TypeError("work_kind must be BackgroundWorkKind")
        if not callable(effect):
            raise TypeError("effect must be callable")
        _require_identity(owner_id, "owner_id")

        for phase in (
            PresenceEvidencePhase.PREFLIGHT,
            PresenceEvidencePhase.EFFECT_RECHECK,
            PresenceEvidencePhase.EFFECT_COMMIT,
        ):
            observation = self._observe_or_pause(task_id=task_id, phase=phase)
            if observation is None:
                return BackgroundDispatchResult(
                    action=BackgroundAction.PAUSE,
                    reason="owner_presence_untrusted",
                    effect_started=False,
                )
            decision = self._policy(
                owner_id=owner_id,
                work_kind=work_kind,
                presence=observation.presence,
            )
            if not decision.allowed:
                return self._apply_denial(
                    task_id=task_id,
                    decision=decision,
                    phase=phase.value,
                )

        claim_key, existing_completed = self._reserve_dispatch_claim(
            task_id=task_id,
            work_kind=work_kind,
            owner_id=owner_id,
        )
        if existing_completed:
            self._audit.append(
                event_type="background.dispatch_duplicate_blocked",
                entity_type="task",
                entity_id=task_id,
                payload={"work_kind": work_kind.value, "reason": "already_completed"},
            )
            return BackgroundDispatchResult(
                action=BackgroundAction.DEFER,
                reason="dispatch_already_completed",
                effect_started=False,
            )

        request_id = f"background:{task_id}"
        try:
            resource_decision = self._resources.request(
                scope="background_life",
                owner_id=owner_id,
                request_id=request_id,
            )
        except Exception:
            self._idempotency.release_pending(claim_key)
            raise
        if not resource_decision.granted:
            waiting_removed = self._resources.cancel_waiting(
                scope="background_life",
                owner_id=owner_id,
                request_id=request_id,
            )
            self._audit.append(
                event_type="background.dispatch_deferred",
                entity_type="task",
                entity_id=task_id,
                payload={
                    "reason": resource_decision.reason,
                    "work_kind": work_kind.value,
                    "phase": "resource_commit",
                    "waiting_removed": waiting_removed,
                },
            )
            self._idempotency.release_pending(claim_key)
            return BackgroundDispatchResult(
                action=BackgroundAction.DEFER,
                reason=resource_decision.reason,
                effect_started=False,
            )

        effect_started = False
        try:
            observation = self._observe_or_pause(
                task_id=task_id,
                phase=PresenceEvidencePhase.POST_GRANT_FENCE,
            )
            if observation is None:
                self._idempotency.release_pending(claim_key)
                return BackgroundDispatchResult(
                    action=BackgroundAction.PAUSE,
                    reason="owner_presence_untrusted",
                    effect_started=False,
                )
            if observation.presence is not OwnerPresence.AWAY:
                decision = BackgroundDecision(
                    action=BackgroundAction.PAUSE,
                    work_kind=work_kind,
                    reason=(
                        "owner_active"
                        if observation.presence is OwnerPresence.ACTIVE
                        else "owner_presence_unknown"
                    ),
                )
                denial = self._apply_denial(
                    task_id=task_id,
                    decision=decision,
                    phase=PresenceEvidencePhase.POST_GRANT_FENCE.value,
                )
                self._idempotency.release_pending(claim_key)
                return denial

            post_grant_decision = self._policy_for_existing_grant(
                owner_id=owner_id,
                work_kind=work_kind,
                presence=observation.presence,
            )
            if not post_grant_decision.allowed:
                denial = self._apply_denial(
                    task_id=task_id,
                    decision=post_grant_decision,
                    phase="post_grant_resource_fence",
                )
                self._idempotency.release_pending(claim_key)
                return denial

            final_observation = self._observe_or_pause(
                task_id=task_id,
                phase=PresenceEvidencePhase.EFFECT_START_FENCE,
            )
            if final_observation is None:
                self._idempotency.release_pending(claim_key)
                return BackgroundDispatchResult(
                    action=BackgroundAction.PAUSE,
                    reason="owner_presence_untrusted",
                    effect_started=False,
                )
            if final_observation.presence is not OwnerPresence.AWAY:
                final_decision = BackgroundDecision(
                    action=BackgroundAction.PAUSE,
                    work_kind=work_kind,
                    reason=(
                        "owner_active"
                        if final_observation.presence is OwnerPresence.ACTIVE
                        else "owner_presence_unknown"
                    ),
                )
                denial = self._apply_denial(
                    task_id=task_id,
                    decision=final_decision,
                    phase=PresenceEvidencePhase.EFFECT_START_FENCE.value,
                )
                self._idempotency.release_pending(claim_key)
                return denial

            final_resource_decision = self._policy_for_existing_grant(
                owner_id=owner_id,
                work_kind=work_kind,
                presence=final_observation.presence,
            )
            if not final_resource_decision.allowed:
                denial = self._apply_denial(
                    task_id=task_id,
                    decision=final_resource_decision,
                    phase="effect_start_resource_fence",
                )
                self._idempotency.release_pending(claim_key)
                return denial

            self._resume_for_dispatch(task_id=task_id, work_kind=work_kind)
            effect_started = True
            try:
                result = await effect()
            except Exception as exc:
                self._idempotency.mark_uncertain(claim_key)
                self._audit.append(
                    event_type="background.dispatch_uncertain",
                    entity_type="task",
                    entity_id=task_id,
                    payload={
                        "work_kind": work_kind.value,
                        "error_type": type(exc).__name__,
                    },
                )
                raise
            if (
                inspect.isawaitable(result)
                or inspect.isgenerator(result)
                or inspect.isasyncgen(result)
            ):
                if inspect.iscoroutine(result):
                    result.close()
                self._idempotency.mark_uncertain(claim_key)
                self._audit.append(
                    event_type="background.dispatch_uncertain",
                    entity_type="task",
                    entity_id=task_id,
                    payload={
                        "work_kind": work_kind.value,
                        "error_type": "DeferredEffectResult",
                    },
                )
                raise TypeError(
                    "background effect returned deferred execution outside "
                    "the checked authority window"
                )
            self._idempotency.complete(
                claim_key,
                {"effect_started": True, "work_kind": work_kind.value},
            )
            self._audit.append(
                event_type="background.dispatch_returned",
                entity_type="task",
                entity_id=task_id,
                payload={"work_kind": work_kind.value},
            )
            return BackgroundDispatchResult(
                action=BackgroundAction.RUN,
                reason="owner_away_capacity_available",
                effect_started=True,
                effect_result=result,
            )
        except Exception:
            if not effect_started:
                record = self._idempotency.get(claim_key)
                if record is not None and record.status is IdempotencyStatus.PENDING:
                    self._idempotency.release_pending(claim_key)
            raise
        finally:
            self._resources.release(
                scope="background_life",
                owner_id=owner_id,
                request_id=request_id,
            )

    async def resume_paused(
        self,
        *,
        task_id: str,
        work_kind: BackgroundWorkKind,
        effect: Callable[[], Awaitable[object]],
        owner_id: str = "living-agent",
    ) -> BackgroundDispatchResult:
        """Continue one exact owner-return PAUSED epoch under fresh background fences."""

        pause_event_id = self._require_owner_return_paused_task(task_id)
        if type(work_kind) is not BackgroundWorkKind:
            raise TypeError("work_kind must be BackgroundWorkKind")
        if not callable(effect):
            raise TypeError("effect must be callable")
        _require_identity(owner_id, "owner_id")

        for phase in (
            PresenceEvidencePhase.PREFLIGHT,
            PresenceEvidencePhase.EFFECT_RECHECK,
            PresenceEvidencePhase.EFFECT_COMMIT,
        ):
            observation = self._observe_without_pause(task_id=task_id, phase=phase)
            if observation is None:
                return BackgroundDispatchResult(
                    action=BackgroundAction.PAUSE,
                    reason="owner_presence_untrusted",
                    effect_started=False,
                )
            decision = self._policy(
                owner_id=owner_id,
                work_kind=work_kind,
                presence=observation.presence,
            )
            if not decision.allowed:
                return self._apply_continuation_denial(
                    task_id=task_id,
                    decision=decision,
                    phase=phase.value,
                    pause_event_id=pause_event_id,
                )

        claim_key, existing_completed = self._reserve_resume_claim(
            task_id=task_id,
            work_kind=work_kind,
            owner_id=owner_id,
            pause_event_id=pause_event_id,
        )
        if existing_completed:
            self._audit.append(
                event_type="background.resume_duplicate_blocked",
                entity_type="task",
                entity_id=task_id,
                payload={
                    "work_kind": work_kind.value,
                    "pause_event_id": pause_event_id,
                    "reason": "already_completed",
                },
            )
            return BackgroundDispatchResult(
                action=BackgroundAction.DEFER,
                reason="resume_already_completed",
                effect_started=False,
            )

        request_id = f"background-resume:{task_id}:{pause_event_id}"
        try:
            resource_decision = self._resources.request(
                scope="background_life",
                owner_id=owner_id,
                request_id=request_id,
            )
        except Exception:
            self._idempotency.release_pending(claim_key)
            raise
        if not resource_decision.granted:
            waiting_removed = self._resources.cancel_waiting(
                scope="background_life",
                owner_id=owner_id,
                request_id=request_id,
            )
            self._audit.append(
                event_type="background.resume_deferred",
                entity_type="task",
                entity_id=task_id,
                payload={
                    "reason": resource_decision.reason,
                    "work_kind": work_kind.value,
                    "pause_event_id": pause_event_id,
                    "phase": "resource_commit",
                    "waiting_removed": waiting_removed,
                },
            )
            self._idempotency.release_pending(claim_key)
            return BackgroundDispatchResult(
                action=BackgroundAction.DEFER,
                reason=resource_decision.reason,
                effect_started=False,
            )

        effect_started = False
        try:
            observation = self._observe_without_pause(
                task_id=task_id,
                phase=PresenceEvidencePhase.POST_GRANT_FENCE,
            )
            if observation is None:
                self._idempotency.release_pending(claim_key)
                return BackgroundDispatchResult(
                    action=BackgroundAction.PAUSE,
                    reason="owner_presence_untrusted",
                    effect_started=False,
                )
            if observation.presence is not OwnerPresence.AWAY:
                decision = BackgroundDecision(
                    action=BackgroundAction.PAUSE,
                    work_kind=work_kind,
                    reason=(
                        "owner_active"
                        if observation.presence is OwnerPresence.ACTIVE
                        else "owner_presence_unknown"
                    ),
                )
                denial = self._apply_continuation_denial(
                    task_id=task_id,
                    decision=decision,
                    phase=PresenceEvidencePhase.POST_GRANT_FENCE.value,
                    pause_event_id=pause_event_id,
                )
                self._idempotency.release_pending(claim_key)
                return denial

            post_grant_decision = self._policy_for_existing_grant(
                owner_id=owner_id,
                work_kind=work_kind,
                presence=observation.presence,
            )
            if not post_grant_decision.allowed:
                denial = self._apply_continuation_denial(
                    task_id=task_id,
                    decision=post_grant_decision,
                    phase="post_grant_resource_fence",
                    pause_event_id=pause_event_id,
                )
                self._idempotency.release_pending(claim_key)
                return denial

            final_observation = self._observe_without_pause(
                task_id=task_id,
                phase=PresenceEvidencePhase.EFFECT_START_FENCE,
            )
            if final_observation is None:
                self._idempotency.release_pending(claim_key)
                return BackgroundDispatchResult(
                    action=BackgroundAction.PAUSE,
                    reason="owner_presence_untrusted",
                    effect_started=False,
                )
            if final_observation.presence is not OwnerPresence.AWAY:
                final_decision = BackgroundDecision(
                    action=BackgroundAction.PAUSE,
                    work_kind=work_kind,
                    reason=(
                        "owner_active"
                        if final_observation.presence is OwnerPresence.ACTIVE
                        else "owner_presence_unknown"
                    ),
                )
                denial = self._apply_continuation_denial(
                    task_id=task_id,
                    decision=final_decision,
                    phase=PresenceEvidencePhase.EFFECT_START_FENCE.value,
                    pause_event_id=pause_event_id,
                )
                self._idempotency.release_pending(claim_key)
                return denial

            final_resource_decision = self._policy_for_existing_grant(
                owner_id=owner_id,
                work_kind=work_kind,
                presence=final_observation.presence,
            )
            if not final_resource_decision.allowed:
                denial = self._apply_continuation_denial(
                    task_id=task_id,
                    decision=final_resource_decision,
                    phase="effect_start_resource_fence",
                    pause_event_id=pause_event_id,
                )
                self._idempotency.release_pending(claim_key)
                return denial

            self._require_owner_return_pause_epoch(task_id, pause_event_id)
            self._audit.append(
                event_type="background.resume_permitted",
                entity_type="task",
                entity_id=task_id,
                payload={
                    "work_kind": work_kind.value,
                    "pause_event_id": pause_event_id,
                },
            )
            effect_started = True
            try:
                result = await effect()
            except Exception as exc:
                self._idempotency.mark_uncertain(claim_key)
                self._audit.append(
                    event_type="background.resume_uncertain",
                    entity_type="task",
                    entity_id=task_id,
                    payload={
                        "work_kind": work_kind.value,
                        "pause_event_id": pause_event_id,
                        "error_type": type(exc).__name__,
                    },
                )
                raise
            if (
                inspect.isawaitable(result)
                or inspect.isgenerator(result)
                or inspect.isasyncgen(result)
            ):
                if inspect.iscoroutine(result):
                    result.close()
                self._idempotency.mark_uncertain(claim_key)
                self._audit.append(
                    event_type="background.resume_uncertain",
                    entity_type="task",
                    entity_id=task_id,
                    payload={
                        "work_kind": work_kind.value,
                        "pause_event_id": pause_event_id,
                        "error_type": "DeferredEffectResult",
                    },
                )
                raise TypeError(
                    "background resume effect returned deferred execution outside "
                    "the checked authority window"
                )
            try:
                self._require_resume_effect_advanced(task_id, pause_event_id)
            except Exception as exc:
                self._idempotency.mark_uncertain(claim_key)
                self._audit.append(
                    event_type="background.resume_uncertain",
                    entity_type="task",
                    entity_id=task_id,
                    payload={
                        "work_kind": work_kind.value,
                        "pause_event_id": pause_event_id,
                        "error_type": type(exc).__name__,
                    },
                )
                raise
            self._idempotency.complete(
                claim_key,
                {
                    "effect_started": True,
                    "work_kind": work_kind.value,
                    "pause_event_id": pause_event_id,
                },
            )
            self._audit.append(
                event_type="background.resume_returned",
                entity_type="task",
                entity_id=task_id,
                payload={
                    "work_kind": work_kind.value,
                    "pause_event_id": pause_event_id,
                },
            )
            return BackgroundDispatchResult(
                action=BackgroundAction.RUN,
                reason="owner_away_capacity_available",
                effect_started=True,
                effect_result=result,
            )
        except Exception:
            if not effect_started:
                record = self._idempotency.get(claim_key)
                if record is not None and record.status is IdempotencyStatus.PENDING:
                    self._idempotency.release_pending(claim_key)
            raise
        finally:
            self._resources.release(
                scope="background_life",
                owner_id=owner_id,
                request_id=request_id,
            )

    def _reserve_dispatch_claim(
        self,
        *,
        task_id: str,
        work_kind: BackgroundWorkKind,
        owner_id: str,
    ) -> tuple[str, bool]:
        operation_key = f"background.dispatch:{task_id}"
        fingerprint_payload = {
            "schema": "nika-background-dispatch-v1",
            "task_id": task_id,
            "work_kind": work_kind.value,
            "owner_id": owner_id,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        record, created = self._idempotency.reserve_once(
            operation_key=operation_key,
            task_id=task_id,
            operation_type=_DISPATCH_OPERATION_TYPE,
            input_fingerprint=fingerprint,
        )
        if created:
            return operation_key, False
        if record.status is IdempotencyStatus.COMPLETED:
            return operation_key, True
        raise IdempotencyConflictError(
            "background dispatch is already pending or uncertain; "
            "reconcile it before replay"
        )

    def _reserve_resume_claim(
        self,
        *,
        task_id: str,
        work_kind: BackgroundWorkKind,
        owner_id: str,
        pause_event_id: int,
    ) -> tuple[str, bool]:
        operation_key = f"background.resume:{task_id}:{pause_event_id}"
        fingerprint_payload = {
            "schema": "nika-background-resume-v1",
            "task_id": task_id,
            "work_kind": work_kind.value,
            "owner_id": owner_id,
            "pause_event_id": pause_event_id,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        record, created = self._idempotency.reserve_once(
            operation_key=operation_key,
            task_id=task_id,
            operation_type=_RESUME_OPERATION_TYPE,
            input_fingerprint=fingerprint,
        )
        if created:
            return operation_key, False
        if record.status is IdempotencyStatus.COMPLETED:
            return operation_key, True
        raise IdempotencyConflictError(
            "background resume is already pending or uncertain; "
            "reconcile it before replay"
        )

    def _policy(
        self,
        *,
        owner_id: str,
        work_kind: BackgroundWorkKind,
        presence: OwnerPresence,
    ) -> BackgroundDecision:
        if presence is not OwnerPresence.AWAY:
            return decide_background_work(
                owner_presence=presence,
                work_kind=work_kind,
                capacity=None,  # type: ignore[arg-type]
            )

        status = self._resources.status(scope="background_life", owner_id=owner_id)
        decision = decide_background_work(
            owner_presence=presence,
            work_kind=work_kind,
            capacity=status,
        )
        if status.budget.owner_id != owner_id:
            raise ValueError("resource capacity owner_id does not match background owner")
        return decision

    def _policy_for_existing_grant(
        self,
        *,
        owner_id: str,
        work_kind: BackgroundWorkKind,
        presence: OwnerPresence,
    ) -> BackgroundDecision:
        """Recheck current resource/power facts without counting this guard's own slot."""

        status = self._resources.status(scope="background_life", owner_id=owner_id)
        decide_background_work(
            owner_presence=OwnerPresence.AWAY,
            work_kind=work_kind,
            capacity=status,
        )
        if status.budget.owner_id != owner_id:
            raise ValueError("resource capacity owner_id does not match background owner")
        if status.active_count < 1:
            raise RuntimeError("background resource grant disappeared before effect")
        active_without_self = status.active_count - 1
        adjusted = replace(
            status,
            active_count=active_without_self,
            concurrency_headroom=max(
                0,
                status.budget.max_concurrent - active_without_self,
            ),
            pressure_reasons=tuple(
                reason for reason in status.pressure_reasons if reason != "concurrency_limit"
            ),
        )
        return decide_background_work(
            owner_presence=presence,
            work_kind=work_kind,
            capacity=adjusted,
        )

    def _observe_or_pause(
        self,
        *,
        task_id: str,
        phase: PresenceEvidencePhase,
    ) -> OwnerPresenceObservation | None:
        try:
            observation = self._snapshot_observation(self._presence.observe())
            self._accept_observation(task_id=task_id, phase=phase, observation=observation)
        except Exception as exc:  # noqa: BLE001 - provider/evidence boundary fails closed
            self._audit.append(
                event_type="background.owner_presence_rejected",
                entity_type="task",
                entity_id=task_id,
                payload={"phase": phase.value, "error_type": type(exc).__name__},
            )
            self._pause_task(
                task_id=task_id,
                reason="owner_presence_untrusted",
                phase=phase.value,
            )
            return None
        return observation

    def _observe_without_pause(
        self,
        *,
        task_id: str,
        phase: PresenceEvidencePhase,
    ) -> OwnerPresenceObservation | None:
        try:
            observation = self._snapshot_observation(self._presence.observe())
            self._accept_observation(task_id=task_id, phase=phase, observation=observation)
        except Exception as exc:  # noqa: BLE001 - provider/evidence boundary fails closed
            self._audit.append(
                event_type="background.owner_presence_rejected",
                entity_type="task",
                entity_id=task_id,
                payload={"phase": phase.value, "error_type": type(exc).__name__},
            )
            return None
        return observation

    @staticmethod
    def _snapshot_observation(raw: object) -> OwnerPresenceObservation:
        """Detach and revalidate untrusted provider evidence at the authority boundary."""

        if type(raw) is not OwnerPresenceObservation:
            raise OwnerPresenceEvidenceError(
                "presence observer returned a non-canonical carrier"
            )
        try:
            return OwnerPresenceObservation(
                source_id=raw.source_id,
                sequence=raw.sequence,
                presence=raw.presence,
                observed_at=raw.observed_at,
            )
        except (TypeError, ValueError) as exc:
            raise OwnerPresenceEvidenceError("presence observation is malformed") from exc

    def _accept_observation(
        self,
        *,
        task_id: str,
        phase: PresenceEvidencePhase,
        observation: OwnerPresenceObservation,
    ) -> None:
        if type(observation) is not OwnerPresenceObservation:
            raise OwnerPresenceEvidenceError("presence observation is not canonical")
        if observation.source_id != self._source_id:
            raise OwnerPresenceEvidenceError("presence observation came from the wrong source")

        now = self._clock()
        if type(now) is not datetime:
            raise OwnerPresenceEvidenceError("presence clock must return exact datetime")
        if now.tzinfo is not UTC:
            raise OwnerPresenceEvidenceError("presence clock must use UTC")

        age_seconds = (now - observation.observed_at).total_seconds()
        if age_seconds > self._max_age:
            raise OwnerPresenceEvidenceError("presence observation is stale")
        if age_seconds < -self._max_future_skew:
            raise OwnerPresenceEvidenceError("presence observation is too far in the future")

        with self._queue.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT event_id, event_type, entity_type, entity_id, "
                "payload_json, created_at FROM audit_events "
                "WHERE entity_type = ? AND entity_id = ? AND event_type = ? "
                "ORDER BY event_id DESC LIMIT 1",
                (_SOURCE_ENTITY_TYPE, self._source_id, _OBSERVED_EVENT),
            ).fetchone()
            if row is not None:
                payload = self._audit._event_from_row(row).payload
                previous_sequence = payload.get("sequence")
                previous_observed_at = payload.get("observed_at")
                if type(previous_sequence) is not int:
                    raise OwnerPresenceEvidenceError(
                        "stored presence evidence has invalid sequence"
                    )
                if not 0 <= previous_sequence <= _MAX_SIGNED_64:
                    raise OwnerPresenceEvidenceError(
                        "stored presence evidence sequence is out of range"
                    )
                if observation.sequence <= previous_sequence:
                    raise OwnerPresenceEvidenceError(
                        "presence observation sequence did not advance"
                    )
                if type(previous_observed_at) is not str:
                    raise OwnerPresenceEvidenceError(
                        "stored presence evidence has invalid timestamp"
                    )
                try:
                    previous_dt = datetime.fromisoformat(previous_observed_at)
                except ValueError as exc:
                    raise OwnerPresenceEvidenceError(
                        "stored presence evidence timestamp is invalid"
                    ) from exc
                if previous_dt.tzinfo is None or previous_dt.utcoffset() is None:
                    raise OwnerPresenceEvidenceError(
                        "stored presence evidence timestamp lacks timezone"
                    )
                if previous_dt.utcoffset().total_seconds() != 0:
                    raise OwnerPresenceEvidenceError(
                        "stored presence evidence timestamp is not UTC"
                    )
                if observation.observed_at < previous_dt:
                    raise OwnerPresenceEvidenceError(
                        "presence observation timestamp regressed"
                    )

            self._audit.append_with_connection(
                conn,
                event_type=_OBSERVED_EVENT,
                entity_type=_SOURCE_ENTITY_TYPE,
                entity_id=self._source_id,
                payload={
                    "task_id": task_id,
                    "phase": phase.value,
                    "sequence": observation.sequence,
                    "presence": observation.presence.value,
                    "observed_at": observation.observed_at.isoformat(),
                },
            )

    def _apply_denial(
        self,
        *,
        task_id: str,
        decision: BackgroundDecision,
        phase: str,
    ) -> BackgroundDispatchResult:
        if decision.action is BackgroundAction.PAUSE:
            self._pause_task(task_id=task_id, reason=decision.reason, phase=phase)
        else:
            self._audit.append(
                event_type="background.dispatch_deferred",
                entity_type="task",
                entity_id=task_id,
                payload={
                    "reason": decision.reason,
                    "work_kind": decision.work_kind.value,
                    "phase": phase,
                },
            )
        return BackgroundDispatchResult(
            action=decision.action,
            reason=decision.reason,
            effect_started=False,
        )

    def _apply_continuation_denial(
        self,
        *,
        task_id: str,
        decision: BackgroundDecision,
        phase: str,
        pause_event_id: int,
    ) -> BackgroundDispatchResult:
        self._audit.append(
            event_type="background.resume_blocked",
            entity_type="task",
            entity_id=task_id,
            payload={
                "action": decision.action.value,
                "reason": decision.reason,
                "work_kind": decision.work_kind.value,
                "phase": phase,
                "pause_event_id": pause_event_id,
            },
        )
        return BackgroundDispatchResult(
            action=decision.action,
            reason=decision.reason,
            effect_started=False,
        )

    def _pause_task(self, *, task_id: str, reason: str, phase: str) -> None:
        with self._queue.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._task_state_with_connection(conn, task_id)
            if state is TaskState.READY:
                self._queue.transition_with_connection(conn, task_id, TaskState.PAUSED)
                pause_event_id = self._latest_task_event_id_with_connection(
                    conn,
                    task_id=task_id,
                    expected_state=TaskState.PAUSED,
                )
                self._audit.append_with_connection(
                    conn,
                    event_type=_BACKGROUND_PAUSED_EVENT,
                    entity_type="task",
                    entity_id=task_id,
                    payload={
                        "reason": reason,
                        "phase": phase,
                        "task_event_id": pause_event_id,
                    },
                )
                return
            if state is not TaskState.PAUSED:
                raise ValueError(
                    "background dispatch can only pause READY or already-PAUSED tasks"
                )
            if not self._background_pause_owned_with_connection(conn, task_id):
                raise ValueError(
                    "background dispatch cannot claim an externally PAUSED task"
                )

    def _resume_for_dispatch(self, *, task_id: str, work_kind: BackgroundWorkKind) -> None:
        with self._queue.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._task_state_with_connection(conn, task_id)
            resumed = False
            if state is TaskState.PAUSED:
                if not self._background_pause_owned_with_connection(conn, task_id):
                    raise ValueError(
                        "background dispatch cannot resume a PAUSED task it does not own"
                    )
                self._queue.transition_with_connection(conn, task_id, TaskState.READY)
                resumed = True
            elif state is not TaskState.READY:
                raise ValueError("background dispatch requires READY or PAUSED task state")
            self._audit.append_with_connection(
                conn,
                event_type="background.dispatch_permitted",
                entity_type="task",
                entity_id=task_id,
                payload={"work_kind": work_kind.value, "resumed": resumed},
            )

    def _require_dispatchable_task(self, task_id: str) -> None:
        _require_identity(task_id, "task_id", maximum=None)
        with self._queue.store.connection() as conn:
            state = self._task_state_with_connection(conn, task_id)
            if state is TaskState.PAUSED:
                if not self._background_pause_owned_with_connection(conn, task_id):
                    raise ValueError(
                        "background dispatch cannot resume a PAUSED task it does not own"
                    )
                return
            if state is not TaskState.READY:
                raise ValueError(
                    "background dispatch requires a durable READY or owned-PAUSED task"
                )

    def _require_owner_return_paused_task(self, task_id: str) -> int:
        _require_identity(task_id, "task_id", maximum=None)
        with self._queue.store.connection() as conn:
            return self._owner_return_pause_event_id_with_connection(conn, task_id)

    def _require_owner_return_pause_epoch(self, task_id: str, pause_event_id: int) -> None:
        with self._queue.store.connection() as conn:
            current_pause_id = self._owner_return_pause_event_id_with_connection(
                conn,
                task_id,
            )
        if current_pause_id != pause_event_id:
            raise ValueError("owner-return pause epoch changed before resume effect")

    def _require_resume_effect_advanced(self, task_id: str, pause_event_id: int) -> None:
        with self._queue.store.connection() as conn:
            rows = conn.execute(
                "SELECT event_id, previous_state, new_state FROM task_events "
                "WHERE task_id = ? AND event_id > ? ORDER BY event_id ASC LIMIT 2",
                (task_id, pause_event_id),
            ).fetchall()
        if len(rows) < 2:
            raise RuntimeError(
                "background resume effect did not prove canonical "
                "PAUSED -> READY -> RUNNING transition"
            )
        ready_event, running_event = rows
        if (
            ready_event["previous_state"] != TaskState.PAUSED.value
            or ready_event["new_state"] != TaskState.READY.value
            or running_event["previous_state"] != TaskState.READY.value
            or running_event["new_state"] != TaskState.RUNNING.value
        ):
            raise RuntimeError(
                "background resume effect did not prove canonical "
                "PAUSED -> READY -> RUNNING transition"
            )

    @classmethod
    def _owner_return_pause_event_id_with_connection(cls, conn, task_id: str) -> int:
        latest_pause_id = cls._latest_task_event_id_with_connection(
            conn,
            task_id=task_id,
            expected_state=TaskState.PAUSED,
        )
        row = conn.execute(
            "SELECT event_id, event_type, entity_type, entity_id, "
            "payload_json, created_at FROM audit_events "
            "WHERE event_type = ? AND entity_type = ? AND entity_id = ? "
            "ORDER BY event_id DESC LIMIT 1",
            (_OWNER_RETURN_PAUSED_EVENT, "task", task_id),
        ).fetchone()
        if row is None:
            raise ValueError("PAUSED task lacks owner-return background pause provenance")
        try:
            payload = AuditLog._event_from_row(row).payload
        except AuditIntegrityError as exc:
            raise ValueError("owner-return pause provenance is malformed") from exc
        if type(payload) is not dict:
            raise ValueError("owner-return pause provenance is malformed")
        task_event_id = payload.get("task_event_id")
        if type(task_event_id) is not int or task_event_id != latest_pause_id:
            raise ValueError("owner-return pause provenance does not match current PAUSED epoch")
        return latest_pause_id

    @staticmethod
    def _latest_task_event_id_with_connection(
        conn,
        *,
        task_id: str,
        expected_state: TaskState,
    ) -> int:
        row = conn.execute(
            "SELECT event_id, new_state FROM task_events "
            "WHERE task_id = ? ORDER BY event_id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("background task has no durable task event")
        if row["new_state"] != expected_state.value:
            raise RuntimeError("background task event does not match current state")
        return int(row["event_id"])

    @classmethod
    def _background_pause_owned_with_connection(cls, conn, task_id: str) -> bool:
        latest_pause_id = cls._latest_task_event_id_with_connection(
            conn,
            task_id=task_id,
            expected_state=TaskState.PAUSED,
        )
        row = conn.execute(
            "SELECT event_id, event_type, entity_type, entity_id, "
            "payload_json, created_at FROM audit_events "
            "WHERE event_type = ? AND entity_type = ? AND entity_id = ? "
            "ORDER BY event_id DESC LIMIT 1",
            (_BACKGROUND_PAUSED_EVENT, "task", task_id),
        ).fetchone()
        if row is None:
            return False
        try:
            payload = AuditLog._event_from_row(row).payload
        except AuditIntegrityError:
            return False
        if type(payload) is not dict:
            return False
        task_event_id = payload.get("task_event_id")
        return type(task_event_id) is int and task_event_id == latest_pause_id

    @staticmethod
    def _task_state_with_connection(conn, task_id: str) -> TaskState:
        row = conn.execute("SELECT state FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown task: {task_id}")
        return TaskState(row["state"])
