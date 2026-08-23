from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from nika_core.kernel.audit import AuditLog


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
    # Compatibility-only caller metadata. A positive value is never execution authority.
    approved: bool = False


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


# This callback is a trusted-host policy boundary. Runtime/model callers may provide ToolCall
# data, but only a host-composed ApprovalPolicy may return positive execution authority.
ApprovalPolicy = Callable[[ToolSpec, ToolCall], Awaitable[bool]]


class ToolExecutor:
    def __init__(
        self,
        *,
        audit_log: AuditLog | None = None,
        approval_policy: ApprovalPolicy | None = None,
    ) -> None:
        self._tools: dict[str, tuple[ToolSpec, ToolHandler]] = {}
        self._audit_log = audit_log
        self._approval_policy = approval_policy

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
        if spec.risk in {ToolRisk.EXTERNAL_SIDE_EFFECT, ToolRisk.HIGH_IMPACT}:
            approved = False
            if self._approval_policy is not None:
                approved = await self._approval_policy(spec, call)
            if not approved:
                self._audit("tool.denied", call, spec, {"reason": "approval_required"})
                return ToolResult(
                    call_id=call.call_id,
                    tool_id=call.tool_id,
                    error="approval required",
                )
        self._audit("tool.started", call, spec, {})
        try:
            output = await asyncio.wait_for(handler(call.arguments), timeout=spec.timeout_seconds)
        except TimeoutError:
            self._audit("tool.failed", call, spec, {"reason": "timeout"})
            return ToolResult(call_id=call.call_id, tool_id=call.tool_id, error="tool timed out")
        except asyncio.CancelledError:
            self._audit("tool.cancelled", call, spec, {})
            raise
        except Exception as exc:  # noqa: BLE001 - normalize adapter failures at the tool boundary.
            self._audit("tool.failed", call, spec, {"reason": type(exc).__name__})
            return ToolResult(call_id=call.call_id, tool_id=call.tool_id, error="tool failed")
        self._audit("tool.completed", call, spec, {})
        return ToolResult(call_id=call.call_id, tool_id=call.tool_id, output=output)

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
