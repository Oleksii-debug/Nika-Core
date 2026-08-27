from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from nika_core.kernel.audit import AuditLog
from nika_core.runtime.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
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
        if not self.tool_id.strip():
            raise ValueError("tool_id must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    tool_id: str
    arguments: dict[str, object]
    approved: bool = False
    task_id: str | None = None


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


ApprovalPolicy = Callable[[ToolSpec, ToolCall], Awaitable[bool]]


class ToolEffectConflictError(RuntimeError):
    """Raised when durable tool-effect evidence conflicts or is unresolved."""


@dataclass(frozen=True, slots=True)
class ToolEffectReservation:
    operation_key: str
    completed_result: Mapping[str, object] | None = None


class ToolEffectGuard:
    """Thin durable reserve/act/finalize guard for external tool effects."""

    _OPERATION_TYPE = "tool.external_effect"

    def __init__(self, ledger: IdempotencyLedger) -> None:
        self._ledger = ledger

    def reserve(self, *, spec: ToolSpec, call: ToolCall) -> ToolEffectReservation:
        task_id = call.task_id or ""
        if not task_id.strip():
            raise ValueError("external tool call requires task_id")
        if not call.call_id.strip():
            raise ValueError("call_id must not be empty")

        operation_key = self._operation_key(task_id=task_id, call_id=call.call_id)
        input_fingerprint = self._fingerprint(spec=spec, call=call)
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
        except sqlite3.Error as exc:
            raise ToolEffectConflictError("tool effect reservation failed closed") from exc

        if created:
            return ToolEffectReservation(operation_key=operation_key)
        if record.status is IdempotencyStatus.COMPLETED:
            completed = dict(record.result or {})
            return ToolEffectReservation(
                operation_key=operation_key,
                completed_result=completed,
            )
        raise ToolEffectConflictError(
            f"tool effect is unresolved: {record.status.value}"
        )

    def complete(self, reservation: ToolEffectReservation, output: object) -> None:
        try:
            json.dumps(output, allow_nan=False, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            # Never certify a result as COMPLETED if restart cannot reproduce it.
            # ToolExecutor will convert this finalize failure into UNCERTAIN.
            raise ValueError("durable tool result must be JSON-compatible") from exc
        self._ledger.complete(
            reservation.operation_key,
            {"completed": True, "output": output},
        )

    def mark_uncertain(self, reservation: ToolEffectReservation) -> None:
        record = self._ledger.require(reservation.operation_key)
        if record.status is IdempotencyStatus.PENDING:
            self._ledger.mark_uncertain(reservation.operation_key)

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
        payload = {
            "arguments": call.arguments,
            "risk": spec.risk.value,
            "tool_id": spec.tool_id,
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
            raise ValueError("durable tool arguments must be JSON-compatible") from exc
        return hashlib.sha256(encoded).hexdigest()


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
        if spec.tool_id in self._tools:
            raise ValueError(f"duplicate tool_id: {spec.tool_id}")
        self._tools[spec.tool_id] = (spec, handler)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(spec for spec, _handler in self._tools.values())

    async def execute(self, call: ToolCall) -> ToolResult:
        registered = self._tools.get(call.tool_id)
        if registered is None:
            return ToolResult(call_id=call.call_id, tool_id=call.tool_id, error="unknown tool")
        spec, handler = registered
        external = spec.risk in {ToolRisk.EXTERNAL_SIDE_EFFECT, ToolRisk.HIGH_IMPACT}
        if external:
            approved = call.approved
            if not approved and self._approval_policy is not None:
                approved = await self._approval_policy(spec, call)
            if not approved:
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
                reservation = self._effect_guard.reserve(spec=spec, call=call)
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
        self._audit_log.append(
            event_type=event_type,
            entity_type="tool_call",
            entity_id=call.call_id,
            payload=payload,
        )