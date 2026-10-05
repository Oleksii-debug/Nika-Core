from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.tools import (
    ToolAuthorization,
    ToolCall,
    ToolEffectGuard,
    ToolEffectReservation,
    ToolExecutor,
    ToolRisk,
    ToolSpec,
    tool_arguments_fingerprint,
)

SPEC = ToolSpec(
    tool_id="external.publish",
    description="Publish a user-approved report",
    risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
)


def _authorization(*, task_id: str = "task-1") -> ToolAuthorization:
    return ToolAuthorization(
        tool_id=SPEC.tool_id,
        task_id=task_id,
        risk=SPEC.risk,
        arguments_fingerprint=tool_arguments_fingerprint({"command": "safe"}),
        effect_fingerprint="approved-effect",
        approval_fingerprint="host-approved-effect",
    )


def _durable_guard(
    path: Path, task_id: str = "task-1"
) -> tuple[ToolEffectGuard, IdempotencyLedger]:
    store = SQLiteStore(path)
    store.initialize()
    with store.connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO tasks(
                task_id, workspace_id, agent_id, state, payload_json, created_at, updated_at
            ) VALUES (?, 'proof', 'worker', 'created', '{}', ?, ?)
            """,
            (task_id, "2026-10-05T00:00:00+00:00", "2026-10-05T00:00:00+00:00"),
        )
    ledger = IdempotencyLedger(store)
    return ToolEffectGuard(ledger), ledger


class SwapAfterFirstTraversal(dict[str, object]):
    def __init__(self) -> None:
        super().__init__(command="safe")
        self.traversals = 0

    def items(self):  # type: ignore[override]
        self.traversals += 1
        if self.traversals == 1:
            self["command"] = "danger"
            return (("command", "safe"),)
        return super().items()


def test_post_approval_argument_swap_cannot_reach_external_handler(tmp_path: Path) -> None:
    guard, ledger = _durable_guard(tmp_path / "approved.db")
    observed: list[str] = []

    async def policy(_spec: ToolSpec, _call: ToolCall) -> ToolAuthorization:
        return _authorization()

    async def handler(arguments: dict[str, object]) -> object:
        observed.append(str(arguments["command"]))
        return {"published": arguments["command"]}

    caller_arguments = SwapAfterFirstTraversal()
    executor = ToolExecutor(approval_policy=policy, effect_guard=guard)
    executor.register(SPEC, handler)
    result = asyncio.run(executor.execute(ToolCall(
        call_id="call-1", tool_id=SPEC.tool_id,
        task_id="task-1", arguments=caller_arguments,
    )))
    assert result.ok
    assert result.output == {"published": "safe"}
    assert observed == ["safe"]
    assert caller_arguments["command"] == "danger"
    assert caller_arguments.traversals == 1
    records = ledger.list_for_task("task-1")
    assert len(records) == 1
    assert records[0].status is IdempotencyStatus.COMPLETED


def test_policy_time_mutation_is_denied_before_reservation(tmp_path: Path) -> None:
    guard, ledger = _durable_guard(tmp_path / "policy-swap.db")
    arguments: dict[str, object] = {"command": "safe"}
    invoked = False

    async def policy(_spec: ToolSpec, _call: ToolCall) -> ToolAuthorization:
        arguments["command"] = "danger"
        return _authorization()

    async def handler(_arguments: dict[str, object]) -> object:
        nonlocal invoked
        invoked = True
        return {"published": True}

    executor = ToolExecutor(approval_policy=policy, effect_guard=guard)
    executor.register(SPEC, handler)
    result = asyncio.run(executor.execute(ToolCall(
        call_id="call-2", tool_id=SPEC.tool_id, task_id="task-1",
        arguments=arguments,
    )))
    assert result.error == "approval required"
    assert not invoked
    assert ledger.list_for_task("task-1") == ()


def test_guard_mutation_does_not_change_handler_arguments() -> None:
    observed: list[str] = []
    guard_seen: list[str] = []

    class MutatingGuard:
        def reserve(self, *, spec: ToolSpec, call: ToolCall) -> ToolEffectReservation:
            assert spec is SPEC
            guard_seen.append(str(call.arguments["command"]))
            call.arguments["command"] = "danger"
            return ToolEffectReservation(operation_key="instrumented-reservation")

        def complete(self, reservation: ToolEffectReservation, output: object) -> None:
            assert reservation.operation_key == "instrumented-reservation"
            assert output == "safe"

    async def policy(_spec: ToolSpec, _call: ToolCall) -> ToolAuthorization:
        return _authorization()

    async def handler(arguments: dict[str, object]) -> object:
        observed.append(str(arguments["command"]))
        return arguments["command"]

    executor = ToolExecutor(
        approval_policy=policy,
        effect_guard=MutatingGuard(),  # type: ignore[arg-type]
    )
    executor.register(SPEC, handler)
    result = asyncio.run(executor.execute(ToolCall(
        call_id="call-3", tool_id=SPEC.tool_id, task_id="task-1",
        arguments={"command": "safe"},
    )))
    assert result.ok
    assert result.output == "safe"
    assert guard_seen == ["safe"]
    assert observed == ["safe"]


def test_normalized_unicode_arguments_replay_as_canonical_data(tmp_path: Path) -> None:
    guard, ledger = _durable_guard(tmp_path / "unicode.db")
    seen: list[dict[str, object]] = []

    async def policy(_spec: ToolSpec, _call: ToolCall) -> ToolAuthorization:
        return ToolAuthorization(
            tool_id=SPEC.tool_id,
            task_id="task-1",
            risk=SPEC.risk,
            arguments_fingerprint=tool_arguments_fingerprint({"é": ["Київ", "😀"]}),
            effect_fingerprint="unicode-effect",
            approval_fingerprint="unicode-approval",
        )

    async def handler(arguments: dict[str, object]) -> object:
        seen.append(arguments)
        return {"key": next(iter(arguments))}

    executor = ToolExecutor(approval_policy=policy, effect_guard=guard)
    executor.register(SPEC, handler)
    call = ToolCall(
        call_id="unicode-call", tool_id=SPEC.tool_id, task_id="task-1",
        arguments={"e\u0301": ["Київ", "😀"]},
    )
    assert asyncio.run(executor.execute(call)).output == {"key": "é"}
    assert asyncio.run(executor.execute(call)).output == {"key": "é"}
    assert seen == [{"é": ["Київ", "😀"]}]
    assert ledger.list_for_task("task-1")[0].status is IdempotencyStatus.COMPLETED


def test_direct_guard_rejects_authorization_argument_mismatch_without_reservation() -> None:
    class ReservationTrap:
        def reserve_once(self, **_kwargs: object) -> None:
            pytest.fail("a mismatched authorization must not reserve an effect")

    guard = ToolEffectGuard(ReservationTrap())  # type: ignore[arg-type]
    call = ToolCall(
        call_id="call-mismatch", tool_id=SPEC.tool_id, task_id="task-1",
        arguments={"command": "danger"}, authorization=_authorization(),
    )
    with pytest.raises(ValueError, match="do not match host authorization"):
        guard.reserve(spec=SPEC, call=call)


@pytest.mark.parametrize(
    ("field", "bad"),
    (
        ("task_id", None),
        ("task_id", 42),
        ("task_id", chr(0xD800)),
        ("task_id", "x" * 513),
        ("task_id", "😀" * 129),
        ("call_id", ""),
        ("call_id", 42),
        ("call_id", chr(0xD800)),
        ("call_id", "x" * 513),
        ("call_id", "😀" * 129),
    ),
)
def test_direct_guard_rejects_invalid_identity_before_ledger(
    field: str, bad: object
) -> None:
    class ReservationTrap:
        def reserve_once(self, **_kwargs: object) -> None:
            pytest.fail("invalid identity must not reach durable ledger")

    guard = ToolEffectGuard(ReservationTrap())  # type: ignore[arg-type]
    attributes: dict[str, object] = {
        "task_id": "task-1", "call_id": "call-1",
        "tool_id": SPEC.tool_id, "arguments": {"command": "safe"},
    }
    attributes[field] = bad
    call = ToolCall(**attributes)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=field):
        guard.reserve(spec=SPEC, call=call)


def test_valid_cyrillic_and_astral_identities_preserve_operation_key(
    tmp_path: Path,
) -> None:
    task_id = "завдання-😀"
    guard, ledger = _durable_guard(tmp_path / "valid-identities.db", task_id=task_id)
    call = ToolCall(
        call_id="виклик-😀", tool_id=SPEC.tool_id, task_id=task_id,
        arguments={"command": "safe"},
    )
    reservation = guard.reserve(spec=SPEC, call=call)
    assert reservation.operation_key.startswith("tool:")
    assert len(ledger.list_for_task(task_id)) == 1


@pytest.mark.parametrize("field", ("task_id", "call_id"))
def test_invalid_unicode_identity_returns_controlled_executor_failure(
    tmp_path: Path, field: str
) -> None:
    guard, ledger = _durable_guard(tmp_path / "invalid-identity.db")
    invoked = False

    async def policy(_spec: ToolSpec, call: ToolCall) -> ToolAuthorization:
        return _authorization(task_id=call.task_id or "")

    async def handler(_arguments: dict[str, object]) -> object:
        nonlocal invoked
        invoked = True
        return {"published": True}

    args: dict[str, object] = {
        "task_id": "task-1", "call_id": "call-4",
        "tool_id": SPEC.tool_id, "arguments": {"command": "safe"},
    }
    args[field] = chr(0xD800)
    executor = ToolExecutor(approval_policy=policy, effect_guard=guard)
    executor.register(SPEC, handler)
    result = asyncio.run(executor.execute(ToolCall(**args)))  # type: ignore[arg-type]
    assert result.error == "tool effect not safe to execute"
    assert not invoked
    assert ledger.list_for_task("task-1") == ()


@pytest.mark.parametrize("invalid_id", (7, chr(0xD800)))
def test_rejected_malformed_call_id_is_audited_without_unicode_escape(
    tmp_path: Path, invalid_id: object
) -> None:
    store = SQLiteStore(tmp_path / "malformed-audit.db")
    store.initialize()
    audit = AuditLog(store)
    called = False

    async def policy(_spec: ToolSpec, _call: ToolCall) -> ToolAuthorization:
        return _authorization()

    async def handler(_arguments: dict[str, object]) -> object:
        nonlocal called
        called = True
        return {"published": True}

    executor = ToolExecutor(
        approval_policy=policy,
        effect_guard=ToolEffectGuard(IdempotencyLedger(store)),
        audit_log=audit,
    )
    executor.register(SPEC, handler)
    result = asyncio.run(executor.execute(ToolCall(
        call_id=invalid_id,  # type: ignore[arg-type] - corrupted runtime carrier
        tool_id=SPEC.tool_id,
        task_id="task-1",
        arguments={"command": "safe"},
    )))
    assert result.error == "tool effect not safe to execute"
    assert not called
    events = audit.list_for(
        entity_type="tool_call", entity_id="invalid-tool-call-id"
    )
    assert len(events) == 1
    assert events[0].event_type == "tool.denied"
    assert events[0].payload["reason"] == "ValueError"


def test_512_byte_identity_boundary_is_supported(tmp_path: Path) -> None:
    task_id = "x" * 512
    guard, ledger = _durable_guard(tmp_path / "identity-bound.db", task_id=task_id)
    call = ToolCall(
        call_id="😀" * 128, tool_id=SPEC.tool_id, task_id=task_id,
        arguments={"command": "safe"},
    )
    reservation = guard.reserve(spec=SPEC, call=call)
    assert reservation.operation_key.startswith("tool:")
    assert len(ledger.list_for_task(task_id)) == 1
