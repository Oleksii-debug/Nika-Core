from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.tools import (
    ToolCall,
    ToolEffectGuard,
    ToolExecutor,
    ToolRisk,
    ToolSpec,
)


def _guard(path: Path) -> tuple[ToolEffectGuard, IdempotencyLedger]:
    store = SQLiteStore(path)
    store.initialize()
    ledger = IdempotencyLedger(store)
    return ToolEffectGuard(ledger), ledger


def _external_spec(*, timeout_seconds: float = 30.0) -> ToolSpec:
    return ToolSpec(
        tool_id="publish",
        description="publish externally",
        risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        timeout_seconds=timeout_seconds,
    )


def test_external_tool_requires_durable_guard_even_when_approved() -> None:
    called = False

    async def handler(_arguments: dict[str, object]) -> object:
        nonlocal called
        called = True
        return "published"

    executor = ToolExecutor()
    executor.register(_external_spec(), handler)

    result = asyncio.run(
        executor.execute(
            ToolCall(
                call_id="call-1",
                tool_id="publish",
                task_id="task-1",
                arguments={"value": "hello"},
                approved=True,
            )
        )
    )

    assert not result.ok
    assert result.error == "durable effect guard required"
    assert called is False


def test_completed_external_tool_replays_after_restart_without_second_effect(
    tmp_path: Path,
) -> None:
    database = tmp_path / "ніка core" / "state.db"
    guard, ledger = _guard(database)
    calls = 0

    async def handler(arguments: dict[str, object]) -> object:
        nonlocal calls
        calls += 1
        return {"published": arguments["value"]}

    call = ToolCall(
        call_id="call-stable",
        tool_id="publish",
        task_id="task-stable",
        arguments={"value": "hello"},
        approved=True,
    )
    first = ToolExecutor(effect_guard=guard)
    first.register(_external_spec(), handler)

    first_result = asyncio.run(first.execute(call))

    assert first_result.ok
    assert first_result.output == {"published": "hello"}
    assert calls == 1
    records = ledger.list_for_task("task-stable")
    assert len(records) == 1
    assert records[0].status is IdempotencyStatus.COMPLETED

    restarted_guard, _restarted_ledger = _guard(database)
    restarted = ToolExecutor(effect_guard=restarted_guard)
    restarted.register(_external_spec(), handler)

    replay = asyncio.run(restarted.execute(call))

    assert replay.ok
    assert replay.output == {"published": "hello"}
    assert calls == 1


def test_same_call_identity_with_different_arguments_fails_closed(tmp_path: Path) -> None:
    guard, _ledger = _guard(tmp_path / "state.db")
    calls = 0

    async def handler(arguments: dict[str, object]) -> object:
        nonlocal calls
        calls += 1
        return arguments

    executor = ToolExecutor(effect_guard=guard)
    executor.register(_external_spec(), handler)
    original = ToolCall(
        call_id="same-call",
        tool_id="publish",
        task_id="task-1",
        arguments={"value": "first"},
        approved=True,
    )
    changed = ToolCall(
        call_id="same-call",
        tool_id="publish",
        task_id="task-1",
        arguments={"value": "changed"},
        approved=True,
    )

    assert asyncio.run(executor.execute(original)).ok
    result = asyncio.run(executor.execute(changed))

    assert not result.ok
    assert result.error == "tool effect not safe to execute"
    assert calls == 1


def test_timeout_becomes_uncertain_and_is_not_replayed(tmp_path: Path) -> None:
    guard, ledger = _guard(tmp_path / "state.db")
    calls = 0

    async def handler(_arguments: dict[str, object]) -> object:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return "late"

    call = ToolCall(
        call_id="timed-call",
        tool_id="publish",
        task_id="task-timeout",
        arguments={},
        approved=True,
    )
    executor = ToolExecutor(effect_guard=guard)
    executor.register(_external_spec(timeout_seconds=0.001), handler)

    first = asyncio.run(executor.execute(call))
    second = asyncio.run(executor.execute(call))

    assert first.error == "tool timed out"
    assert second.error == "tool effect not safe to execute"
    assert calls == 1
    records = ledger.list_for_task("task-timeout")
    assert len(records) == 1
    assert records[0].status is IdempotencyStatus.UNCERTAIN


def test_handler_error_becomes_uncertain_and_is_not_replayed(tmp_path: Path) -> None:
    guard, ledger = _guard(tmp_path / "state.db")
    calls = 0

    async def handler(_arguments: dict[str, object]) -> object:
        nonlocal calls
        calls += 1
        raise RuntimeError("remote outcome unknown")

    call = ToolCall(
        call_id="error-call",
        tool_id="publish",
        task_id="task-error",
        arguments={},
        approved=True,
    )
    executor = ToolExecutor(effect_guard=guard)
    executor.register(_external_spec(), handler)

    first = asyncio.run(executor.execute(call))
    second = asyncio.run(executor.execute(call))

    assert first.error == "tool failed"
    assert second.error == "tool effect not safe to execute"
    assert calls == 1
    assert ledger.list_for_task("task-error")[0].status is IdempotencyStatus.UNCERTAIN


def test_cancellation_becomes_uncertain_and_propagates(tmp_path: Path) -> None:
    guard, ledger = _guard(tmp_path / "state.db")
    started = asyncio.Event()

    async def handler(_arguments: dict[str, object]) -> object:
        started.set()
        await asyncio.sleep(60)
        return "never"

    call = ToolCall(
        call_id="cancel-call",
        tool_id="publish",
        task_id="task-cancel",
        arguments={},
        approved=True,
    )
    executor = ToolExecutor(effect_guard=guard)
    executor.register(_external_spec(), handler)

    async def scenario() -> None:
        running = asyncio.create_task(executor.execute(call))
        await started.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    asyncio.run(scenario())

    assert ledger.list_for_task("task-cancel")[0].status is IdempotencyStatus.UNCERTAIN
    replay = asyncio.run(executor.execute(call))
    assert replay.error == "tool effect not safe to execute"


def test_read_only_tool_remains_compatible_without_guard() -> None:
    async def handler(arguments: dict[str, object]) -> object:
        return arguments["value"]

    executor = ToolExecutor()
    executor.register(ToolSpec(tool_id="read", description="read"), handler)

    result = asyncio.run(
        executor.execute(ToolCall(call_id="read-1", tool_id="read", arguments={"value": 7}))
    )

    assert result.ok
    assert result.output == 7
