from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol

from nika_core.kernel.audit import AuditLog
from nika_core.runtime.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
    IdempotencyRecord,
    IdempotencyStatus,
)


class ToolRisk(StrEnum):
    READ_ONLY = "read_only"
    LOCAL_WRITE = "local_write"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    HIGH_IMPACT = "high_impact"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    tool_id: str
    description: str
    risk: ToolRisk = ToolRisk.READ_ONLY
    timeout_seconds: float = 30.0
    input_schema: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_tool_identity(self.tool_id, label="tool_id")
        if type(self.risk) is not ToolRisk:
            raise ValueError("risk must be a ToolRisk value")
        # Tool calls must have a real deadline; NaN, infinity and bool bypass <= 0.
        if (
            type(self.timeout_seconds) not in (int, float)
            or not 0 < self.timeout_seconds <= 86_400
        ):
            raise ValueError("timeout_seconds must be finite and between 0 and 86400")


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    tool_id: str
    arguments: dict[str, object]
    # Compatibility-only caller metadata. A positive value is never execution authority.
    approved: bool = False
    task_id: str | None = None
    # Host-internal identity. ToolExecutor always overwrites caller input with the trusted
    # policy result before consulting durable effect evidence.
    authorization: ToolAuthorization | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ToolAuthorization:
    """Trusted, stable identity returned by an exact-effect host policy."""

    tool_id: str
    task_id: str
    risk: ToolRisk
    arguments_fingerprint: str
    effect_fingerprint: str
    approval_fingerprint: str

    def __post_init__(self) -> None:
        required = (
            self.tool_id,
            self.task_id,
            self.arguments_fingerprint,
            self.effect_fingerprint,
            self.approval_fingerprint,
        )
        if type(self.risk) is not ToolRisk:
            raise ValueError("authorization risk must be a ToolRisk value")
        for value in required:
            if type(value) is not str or not value.strip():
                raise ValueError("tool authorization identities must be nonempty text")
            try:
                encoded = value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("tool authorization must use valid UTF-8") from exc
            if len(encoded) > 512:
                raise ValueError("tool authorization identity exceeds 512 UTF-8 bytes")

    def matches(self, *, spec: ToolSpec, call: ToolCall) -> bool:
        try:
            admitted = _snapshot_tool_authorization(self)
        except (TypeError, ValueError):
            return False
        if not (
            admitted.tool_id == spec.tool_id == call.tool_id
            and admitted.task_id == call.task_id
            and admitted.risk is spec.risk
        ):
            return False
        try:
            return admitted.arguments_fingerprint == tool_arguments_fingerprint(call.arguments)
        except (TypeError, ValueError):
            # Malformed model/tool arguments are never affirmative authorization.
            return False


def _snapshot_tool_authorization(value: ToolAuthorization) -> ToolAuthorization:
    """Revalidate and detach an authorization token at every trust boundary."""
    if type(value) is not ToolAuthorization:
        raise ValueError("invalid tool authorization carrier")
    return ToolAuthorization(
        tool_id=value.tool_id,
        task_id=value.task_id,
        risk=value.risk,
        arguments_fingerprint=value.arguments_fingerprint,
        effect_fingerprint=value.effect_fingerprint,
        approval_fingerprint=value.approval_fingerprint,
    )


def _canonical_tool_arguments(arguments: Mapping[str, object]) -> str:
    # Reuse ActionIntent's bounded NFC authority; never retain caller-owned containers.
    from nika_core.security.policy import _canonical_arguments

    try:
        encoded, _frozen = _canonical_arguments(arguments)
    except (TypeError, ValueError):
        raise
    except Exception as exc:
        raise ValueError("tool arguments must be deterministic JSON-compatible data") from exc
    return encoded


def tool_arguments_fingerprint(arguments: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_tool_arguments(arguments).encode("utf-8")).hexdigest()


def _snapshot_tool_arguments(arguments: Mapping[str, object]) -> dict[str, object]:
    # The canonical encoder has already detached and normalized all nested caller data.
    snapshot = json.loads(_canonical_tool_arguments(arguments))
    assert type(snapshot) is dict
    return snapshot


def _require_tool_identity(value: object, *, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    if len(value) > 512:
        raise ValueError(f"{label} exceeds maximum UTF-8 byte length")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc
    if len(encoded) > 512:
        raise ValueError(f"{label} exceeds maximum UTF-8 byte length")
    return value


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    tool_id: str
    output: object | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class ToolHandler(Protocol):
    async def __call__(self, arguments: dict[str, object]) -> object: ...


# Trusted-host policy boundary. Runtime/model callers may supply ToolCall data, but only
# host-composed policy may grant positive execution authority.
ApprovalPolicy = Callable[
    [ToolSpec, ToolCall],
    Awaitable[ToolAuthorization | bool | None],
]


class ToolEffectConflictError(RuntimeError):
    """Raised when durable tool-effect evidence conflicts or is unresolved."""


@dataclass(frozen=True, slots=True)
class ToolEffectReservation:
    operation_key: str
    completed_result: Mapping[str, object] | None = None
    task_id: str | None = None
    operation_type: str | None = None
    input_fingerprint: str | None = None
    created_at: str | None = None


class ToolEffectGuard:
    """Thin durable reserve/act/finalize guard for external tool effects."""

    _OPERATION_TYPE = "tool.external_effect"

    def __init__(self, ledger: IdempotencyLedger) -> None:
        self._ledger = ledger

    def reserve(self, *, spec: ToolSpec, call: ToolCall) -> ToolEffectReservation:
        task_id = _require_tool_identity(call.task_id, label="task_id")
        call_id = _require_tool_identity(call.call_id, label="call_id")
        tool_id = _require_tool_identity(call.tool_id, label="tool_id")
        if tool_id != spec.tool_id:
            raise ValueError("tool_id does not match registered tool specification")
        try:
            # A direct guard caller gets the same bounded, detached argument boundary.
            admitted_call = replace(
                call, arguments=_snapshot_tool_arguments(call.arguments)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("durable tool arguments must be JSON-compatible") from exc
        if admitted_call.authorization is not None:
            try:
                approved = _snapshot_tool_authorization(admitted_call.authorization)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid durable host authorization") from exc
            admitted_call = replace(admitted_call, authorization=approved)
            if not approved.matches(spec=spec, call=admitted_call):
                raise ValueError("durable tool arguments do not match host authorization")

        operation_key = self._operation_key(task_id=task_id, call_id=call_id)
        input_fingerprint = self._fingerprint(spec=spec, call=admitted_call)
        try:
            record, created = self._ledger.reserve_once(
                operation_key=operation_key,
                task_id=task_id,
                operation_type=self._OPERATION_TYPE,
                input_fingerprint=input_fingerprint,
            )
        except IdempotencyConflictError as exc:
            raise ToolEffectConflictError(
                "tool effect identity conflicts with durable evidence"
            ) from exc
        except sqlite3.IntegrityError:
            # A simultaneous first reservation can lose the UNIQUE(operation_key) race
            # after both callers observed absence. Re-read through the canonical ledger
            # so the loser deterministically observes the winner instead of leaking a
            # raw SQLite exception across the tool boundary.
            try:
                record, created = self._ledger.reserve_once(
                    operation_key=operation_key,
                    task_id=task_id,
                    operation_type=self._OPERATION_TYPE,
                    input_fingerprint=input_fingerprint,
                )
            except IdempotencyConflictError as exc:
                raise ToolEffectConflictError(
                    "tool effect identity conflicts with durable evidence"
                ) from exc
            except sqlite3.Error as exc:
                raise ToolEffectConflictError("tool effect reservation failed closed") from exc
            except RuntimeError as exc:
                # Strict ledger readers reject corrupt persisted evidence with
                # RuntimeError. Do not leak it or let a handler run after it.
                raise ToolEffectConflictError("tool effect evidence is invalid") from exc
        except sqlite3.Error as exc:
            raise ToolEffectConflictError("tool effect reservation failed closed") from exc
        except RuntimeError as exc:
            raise ToolEffectConflictError("tool effect evidence is invalid") from exc

        if created:
            return self._reservation_from_record(record)
        if record.status is IdempotencyStatus.COMPLETED:
            # A corrupt/missing SQLite result must never become an affirmative
            # replay with output=None. Only the canonical finalize envelope is
            # evidence that this exact external effect finished durably.
            completed = record.result
            if (
                type(completed) is not dict
                or completed.get("completed") is not True
                or "output" not in completed
            ):
                raise ToolEffectConflictError(
                    "completed tool effect has invalid durable result evidence"
                )
            try:
                json.dumps(
                    completed, allow_nan=False, ensure_ascii=False, sort_keys=True
                ).encode("utf-8")
            except (TypeError, ValueError, RecursionError) as exc:
                raise ToolEffectConflictError(
                    "completed tool effect has invalid durable result evidence"
                ) from exc
            return self._reservation_from_record(
                record,
                completed_result=dict(completed),
            )
        raise ToolEffectConflictError(
            f"tool effect is unresolved: {record.status.value}"
        )

    def complete(self, reservation: ToolEffectReservation, output: object) -> None:
        try:
            json.dumps(
                output,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            # Never certify a result as COMPLETED if restart cannot reproduce it.
            # ToolExecutor will convert this finalize failure into UNCERTAIN.
            raise ValueError("durable tool result must be JSON-compatible") from exc

        (
            operation_key,
            task_id,
            operation_type,
            input_fingerprint,
            created_at,
        ) = self._reservation_identity(reservation)
        try:
            self._ledger.complete_pending_if_matches(
                operation_key=operation_key,
                task_id=task_id,
                operation_type=operation_type,
                input_fingerprint=input_fingerprint,
                created_at=created_at,
                result={"completed": True, "output": output},
            )
        except (IdempotencyConflictError, KeyError) as exc:
            raise ToolEffectConflictError(
                "tool effect reservation changed before completion"
            ) from exc

    def mark_uncertain(self, reservation: ToolEffectReservation) -> None:
        (
            operation_key,
            task_id,
            operation_type,
            input_fingerprint,
            created_at,
        ) = self._reservation_identity(reservation)
        try:
            self._ledger.mark_pending_uncertain_if_matches(
                operation_key=operation_key,
                task_id=task_id,
                operation_type=operation_type,
                input_fingerprint=input_fingerprint,
                created_at=created_at,
            )
        except (IdempotencyConflictError, KeyError) as exc:
            raise ToolEffectConflictError(
                "tool effect reservation changed before uncertainty recording"
            ) from exc

    @staticmethod
    def _reservation_from_record(
        record: IdempotencyRecord,
        *,
        completed_result: Mapping[str, object] | None = None,
    ) -> ToolEffectReservation:
        return ToolEffectReservation(
            operation_key=record.operation_key,
            completed_result=completed_result,
            task_id=record.task_id,
            operation_type=record.operation_type,
            input_fingerprint=record.input_fingerprint,
            created_at=record.created_at,
        )

    @classmethod
    def _reservation_identity(
        cls,
        reservation: ToolEffectReservation,
    ) -> tuple[str, str, str, str, str]:
        if type(reservation) is not ToolEffectReservation:
            raise ToolEffectConflictError(
                "tool effect finalization lacks reservation authority"
            )
        return (
            cls._reservation_text(reservation.operation_key),
            cls._reservation_text(reservation.task_id),
            cls._reservation_text(reservation.operation_type),
            cls._reservation_text(reservation.input_fingerprint),
            cls._reservation_text(reservation.created_at),
        )

    @staticmethod
    def _reservation_text(value: object) -> str:
        try:
            return _require_tool_identity(value, label="reservation authority")
        except ValueError as exc:
            raise ToolEffectConflictError(
                "tool effect finalization lacks reservation authority"
            ) from exc

    @staticmethod
    def _operation_key(*, task_id: str, call_id: str) -> str:
        identity = json.dumps(
            {"call_id": call_id, "task_id": task_id},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"tool:{hashlib.sha256(identity).hexdigest()}"

    @staticmethod
    def _fingerprint(*, spec: ToolSpec, call: ToolCall) -> str:
        authorization = call.authorization
        payload = {
            "approval_fingerprint": (
                authorization.approval_fingerprint if authorization is not None else None
            ),
            "arguments": call.arguments,
            "effect_fingerprint": (
                authorization.effect_fingerprint if authorization is not None else None
            ),
            "risk": spec.risk.value,
            "tool_id": spec.tool_id,
        }
        try:
            # Direct ToolEffectGuard.reserve is also a public admission path:
            # bound the arguments before assembling the durable payload.
            tool_arguments_fingerprint(call.arguments)
            encoded = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("durable tool arguments must be JSON-compatible") from exc
        return hashlib.sha256(encoded).hexdigest()


def _snapshot_tool_spec(spec: ToolSpec) -> ToolSpec:
    """Detach authority metadata and schema from a boundary's caller."""
    # Reuse the bounded, UTF-8-safe JSON authority already applied to tool effects.
    # deepcopy alone accepts cycles, hostile objects and unbounded schemas.
    return replace(spec, input_schema=_snapshot_tool_arguments(spec.input_schema))


class ToolExecutor:
    def __init__(
        self,
        *,
        audit_log: AuditLog | None = None,
        approval_policy: ApprovalPolicy | None = None,
        effect_guard: ToolEffectGuard | None = None,
    ) -> None:
        self._tools: dict[str, tuple[ToolSpec, ToolHandler]] = {}
        self._audit_log = audit_log
        self._approval_policy = approval_policy
        self._effect_guard = effect_guard

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        # A frozen dataclass can still be changed through object.__setattr__, and
        # input_schema contains mutable nested data. Never let a caller's original
        # spec change the registered risk, identity or deadline after admission.
        admitted = _snapshot_tool_spec(spec)
        if admitted.tool_id in self._tools:
            raise ValueError(f"duplicate tool_id: {admitted.tool_id}")
        self._tools[admitted.tool_id] = (admitted, handler)

    def specs(self) -> tuple[ToolSpec, ...]:
        # Catalog consumers must not receive the authority-bearing registry objects.
        return tuple(
            _snapshot_tool_spec(spec)
            for spec, _handler in self._tools.values()
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            tool_id = _require_tool_identity(call.tool_id, label="tool_id")
        except ValueError:
            return ToolResult(call_id=call.call_id, tool_id="", error="invalid tool id")
        registered = self._tools.get(tool_id)
        if registered is None:
            return ToolResult(call_id=call.call_id, tool_id=call.tool_id, error="unknown tool")
        spec, handler = registered
        external = spec.risk in {ToolRisk.EXTERNAL_SIDE_EFFECT, ToolRisk.HIGH_IMPACT}
        if external:
            # Caller-controlled compatibility metadata is never positive authority.  The
            # trusted host policy must approve the exact current effect before durable replay
            # evidence is consulted or a new reservation is created.
            authorization: ToolAuthorization | None = None
            if self._approval_policy is not None:
                try:
                    decision = await self._approval_policy(_snapshot_tool_spec(spec), call)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - trusted boundary fails closed.
                    self._audit(
                        "tool.denied",
                        call,
                        spec,
                        {"reason": type(exc).__name__, "phase": "approval_policy"},
                    )
                    return ToolResult(
                        call_id=call.call_id,
                        tool_id=call.tool_id,
                        error="approval required",
                    )
                if isinstance(decision, ToolAuthorization):
                    try:
                        # Snapshot only once from caller-owned data after policy returns.
                        # Both approval matching and later execution use this snapshot.
                        approved_authorization = _snapshot_tool_authorization(decision)
                        approved_arguments = _snapshot_tool_arguments(call.arguments)
                    except (TypeError, ValueError):
                        self._audit(
                            "tool.denied",
                            call,
                            spec,
                            {"reason": "invalid_arguments"},
                        )
                    else:
                        authorized_call = replace(
                            call, arguments=approved_arguments,
                            authorization=approved_authorization
                        )
                        if approved_authorization.matches(spec=spec, call=authorized_call):
                            authorization = approved_authorization
                        else:
                            self._audit(
                                "tool.denied",
                                call,
                                spec,
                                {"reason": "exact_authorization_mismatch"},
                            )
                elif decision:
                    self._audit(
                        "tool.denied",
                        call,
                        spec,
                        {"reason": "exact_authorization_required"},
                    )
            if authorization is None:
                self._audit("tool.denied", call, spec, {"reason": "approval_required"})
                return ToolResult(
                    call_id=call.call_id,
                    tool_id=call.tool_id,
                    error="approval required",
                )
            if self._effect_guard is None:
                self._audit("tool.denied", call, spec, {"reason": "durable_guard_required"})
                return ToolResult(
                    call_id=call.call_id,
                    tool_id=call.tool_id,
                    error="durable effect guard required",
                )
            try:
                # The guard receives a separate detached copy: even an instrumented
                # reservation cannot replace the handler's approved arguments.
                guard_call = replace(
                    authorized_call,
                    arguments=_snapshot_tool_arguments(authorized_call.arguments),
                )
                reservation = self._effect_guard.reserve(
                    spec=_snapshot_tool_spec(spec), call=guard_call
                )
                call = authorized_call
            except (ToolEffectConflictError, ValueError) as exc:
                self._audit("tool.denied", call, spec, {"reason": type(exc).__name__})
                return ToolResult(
                    call_id=call.call_id,
                    tool_id=call.tool_id,
                    error="tool effect not safe to execute",
                )
            if reservation.completed_result is not None:
                self._audit("tool.replayed", call, spec, {"durable": True})
                return ToolResult(
                    call_id=call.call_id,
                    tool_id=call.tool_id,
                    output=reservation.completed_result.get("output"),
                )
        else:
            reservation = None

        self._audit("tool.started", call, spec, {})
        try:
            output = await asyncio.wait_for(handler(call.arguments), timeout=spec.timeout_seconds)
        except TimeoutError:
            self._mark_uncertain(reservation)
            self._audit("tool.failed", call, spec, {"reason": "timeout"})
            return ToolResult(call_id=call.call_id, tool_id=call.tool_id, error="tool timed out")
        except asyncio.CancelledError:
            self._mark_uncertain(reservation)
            self._audit("tool.cancelled", call, spec, {})
            raise
        except Exception as exc:  # noqa: BLE001 - normalize adapter failures at the tool boundary.
            self._mark_uncertain(reservation)
            self._audit("tool.failed", call, spec, {"reason": type(exc).__name__})
            return ToolResult(call_id=call.call_id, tool_id=call.tool_id, error="tool failed")

        if reservation is not None:
            try:
                assert self._effect_guard is not None
                self._effect_guard.complete(reservation, output)
            except Exception as exc:  # noqa: BLE001 - remote effect succeeded; fail closed on local durability.
                self._mark_uncertain(reservation)
                self._audit(
                    "tool.failed",
                    call,
                    spec,
                    {"reason": type(exc).__name__, "phase": "durable_finalize"},
                )
                return ToolResult(
                    call_id=call.call_id,
                    tool_id=call.tool_id,
                    error="tool result durability failed",
                )

        self._audit("tool.completed", call, spec, {})
        return ToolResult(call_id=call.call_id, tool_id=call.tool_id, output=output)

    def _mark_uncertain(self, reservation: ToolEffectReservation | None) -> None:
        if reservation is None or self._effect_guard is None:
            return
        try:
            self._effect_guard.mark_uncertain(reservation)
        except Exception:  # noqa: BLE001 - preserve the original tool-boundary failure.
            return

    def _audit(
        self,
        event_type: str,
        call: ToolCall,
        spec: ToolSpec,
        extra: dict[str, object],
    ) -> None:
        if self._audit_log is None:
            return
        payload: dict[str, object] = {"tool_id": spec.tool_id, "risk": spec.risk.value}
        payload.update(extra)
        try:
            entity_id = _require_tool_identity(call.call_id, label="call_id")
        except ValueError:
            # Rejection itself must remain auditable even for invalid UTF-8 carriers.
            entity_id = "invalid-tool-call-id"
        self._audit_log.append(
            event_type=event_type,
            entity_type="tool_call",
            entity_id=entity_id,
            payload=payload,
        )
