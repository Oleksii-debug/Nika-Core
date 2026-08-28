from __future__ import annotations

import asyncio
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from threading import Barrier, Event, Lock

from nika_core.data.sqlite import SQLiteStore
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.tools import ToolCall, ToolEffectGuard, ToolExecutor, ToolRisk, ToolSpec


_RACE_ITERATIONS = 32
_WAIT_SECONDS = 10.0


def _guard(path: Path, task_id: str) -> tuple[ToolEffectGuard, IdempotencyLedger]:
    store = SQLiteStore(path)
    store.initialize()
    with store.connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO tasks(
                task_id, workspace_id, agent_id, state, payload_json, created_at, updated_at
            ) VALUES (?, 'worker20-race', 'worker20', 'created', '{}', ?, ?)
            """,
            (task_id, "2026-08-28T00:00:00+00:00", "2026-08-28T00:00:00+00:00"),
        )
    ledger = IdempotencyLedger(store)
    return ToolEffectGuard(ledger), ledger


def _spec() -> ToolSpec:
    return ToolSpec(
        tool_id="publish",
        description="worker20 observable effect",
        risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        timeout_seconds=30.0,
    )


def _call(task_id: str, *, value: str) -> ToolCall:
    return ToolCall(
        call_id="stable-effect",
        tool_id="publish",
        task_id=task_id,
        arguments={"value": value},
        approved=True,
    )


def _execute_after_barrier(
    start: Barrier,
    executor: ToolExecutor,
    call: ToolCall,
):
    start.wait(timeout=_WAIT_SECONDS)
    return asyncio.run(executor.execute(call))


def test_two_independent_executors_have_one_canonical_effect_owner_and_restart_replay(
    tmp_path: Path,
) -> None:
    for iteration in range(_RACE_ITERATIONS):
        database = tmp_path / f"same-input-{iteration}" / "state.db"
        task_id = f"worker20-same-{iteration}"
        first_guard, first_ledger = _guard(database, task_id)
        second_guard, _second_ledger = _guard(database, task_id)
        handler_started = Event()
        release_handler = Event()
        handler_lock = Lock()
        handler_calls = 0

        async def handler(arguments: dict[str, object]) -> object:
            nonlocal handler_calls
            with handler_lock:
                handler_calls += 1
            handler_started.set()
            if not release_handler.wait(timeout=_WAIT_SECONDS):
                raise AssertionError("worker20 handler release was not signalled")
            return {"published": arguments["value"]}

        first = ToolExecutor(effect_guard=first_guard)
        second = ToolExecutor(effect_guard=second_guard)
        first.register(_spec(), handler)
        second.register(_spec(), handler)
        call = _call(task_id, value="same")
        start = Barrier(3)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(_execute_after_barrier, start, first, call),
                pool.submit(_execute_after_barrier, start, second, call),
            ]
            start.wait(timeout=_WAIT_SECONDS)
            assert handler_started.wait(timeout=_WAIT_SECONDS)

            done, _pending = wait(
                futures,
                timeout=_WAIT_SECONDS,
                return_when=FIRST_COMPLETED,
            )
            assert len(done) == 1
            blocked = next(iter(done)).result(timeout=_WAIT_SECONDS)
            assert not blocked.ok
            assert blocked.error == "tool effect not safe to execute"
            assert handler_calls == 1

            release_handler.set()
            results = [future.result(timeout=_WAIT_SECONDS) for future in futures]

        assert sum(result.ok for result in results) == 1
        assert handler_calls == 1
        records = first_ledger.list_for_task(task_id)
        assert len(records) == 1
        assert records[0].status is IdempotencyStatus.COMPLETED

        restarted_guard, restarted_ledger = _guard(database, task_id)
        restarted = ToolExecutor(effect_guard=restarted_guard)
        restarted.register(_spec(), handler)
        replay = asyncio.run(restarted.execute(call))

        assert replay.ok
        assert replay.output == {"published": "same"}
        assert handler_calls == 1
        restarted_records = restarted_ledger.list_for_task(task_id)
        assert len(restarted_records) == 1
        assert restarted_records[0].status is IdempotencyStatus.COMPLETED


def test_conflicting_argument_race_never_creates_second_owner_and_restart_preserves_winner(
    tmp_path: Path,
) -> None:
    for iteration in range(_RACE_ITERATIONS):
        database = tmp_path / f"conflict-{iteration}" / "state.db"
        task_id = f"worker20-conflict-{iteration}"
        first_guard, first_ledger = _guard(database, task_id)
        second_guard, _second_ledger = _guard(database, task_id)
        handler_started = Event()
        release_handler = Event()
        handler_lock = Lock()
        handler_calls = 0

        async def handler(arguments: dict[str, object]) -> object:
            nonlocal handler_calls
            with handler_lock:
                handler_calls += 1
            handler_started.set()
            if not release_handler.wait(timeout=_WAIT_SECONDS):
                raise AssertionError("worker20 handler release was not signalled")
            return {"published": arguments["value"]}

        first = ToolExecutor(effect_guard=first_guard)
        second = ToolExecutor(effect_guard=second_guard)
        first.register(_spec(), handler)
        second.register(_spec(), handler)
        call_x = _call(task_id, value="X")
        call_y = _call(task_id, value="Y")
        start = Barrier(3)

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_x = pool.submit(_execute_after_barrier, start, first, call_x)
            future_y = pool.submit(_execute_after_barrier, start, second, call_y)
            futures = [future_x, future_y]
            start.wait(timeout=_WAIT_SECONDS)
            assert handler_started.wait(timeout=_WAIT_SECONDS)

            done, _pending = wait(
                futures,
                timeout=_WAIT_SECONDS,
                return_when=FIRST_COMPLETED,
            )
            assert len(done) == 1
            blocked = next(iter(done)).result(timeout=_WAIT_SECONDS)
            assert not blocked.ok
            assert blocked.error == "tool effect not safe to execute"
            assert handler_calls == 1

            release_handler.set()
            result_x = future_x.result(timeout=_WAIT_SECONDS)
            result_y = future_y.result(timeout=_WAIT_SECONDS)

        assert result_x.ok is not result_y.ok
        assert handler_calls == 1
        records = first_ledger.list_for_task(task_id)
        assert len(records) == 1
        assert records[0].status is IdempotencyStatus.COMPLETED

        winner_call = call_x if result_x.ok else call_y
        loser_call = call_y if result_x.ok else call_x
        winner_value = winner_call.arguments["value"]
        restarted_guard, restarted_ledger = _guard(database, task_id)
        restarted = ToolExecutor(effect_guard=restarted_guard)
        restarted.register(_spec(), handler)

        replay = asyncio.run(restarted.execute(winner_call))
        conflict = asyncio.run(restarted.execute(loser_call))

        assert replay.ok
        assert replay.output == {"published": winner_value}
        assert not conflict.ok
        assert conflict.error == "tool effect not safe to execute"
        assert handler_calls == 1
        restarted_records = restarted_ledger.list_for_task(task_id)
        assert len(restarted_records) == 1
        assert restarted_records[0].status is IdempotencyStatus.COMPLETED
