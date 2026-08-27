from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.runtime.recovery import RecoveryDisposition, RuntimeRecoveryService
from nika_core.runtime.registry import RuntimeRegistry
from nika_core.runtime.session_store import RuntimeSessionStore
from nika_core.tools import ToolCall, ToolEffectGuard, ToolExecutor, ToolRisk, ToolSpec


_CHILD = textwrap.dedent(
    r"""
    import asyncio
    import os
    import sys
    from pathlib import Path

    from nika_core.data.sqlite import SQLiteStore
    from nika_core.kernel.task_queue import TaskQueue
    from nika_core.kernel.task_state import TaskState
    from nika_core.runtime.idempotency import IdempotencyLedger
    from nika_core.runtime.session_store import RuntimeSessionStore
    from nika_core.tools import ToolCall, ToolEffectGuard, ToolExecutor, ToolRisk, ToolSpec

    database = Path(sys.argv[1])
    marker = Path(sys.argv[2])
    task_file = Path(sys.argv[3])

    store = SQLiteStore(database)
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(workspace_id="worker07-crash-proof", agent_id="worker07")
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    RuntimeSessionStore(store).record_active(
        task_id=task.task_id,
        runtime_id="worker07-hard-crash",
        thread_id="worker07-thread",
        resume_token="worker07-thread",
    )
    task_file.write_text(task.task_id, encoding="utf-8")

    durable_guard = ToolEffectGuard(IdempotencyLedger(store))

    class CrashBeforeCompletion:
        def reserve(self, *, spec, call):
            return durable_guard.reserve(spec=spec, call=call)

        def mark_uncertain(self, reservation):
            durable_guard.mark_uncertain(reservation)

        def complete(self, reservation, output):
            del reservation, output
            os._exit(86)

    async def publish(arguments):
        with marker.open("a", encoding="utf-8") as handle:
            handle.write(f"published:{arguments['value']}\n")
            handle.flush()
            os.fsync(handle.fileno())
        return {"published": arguments["value"]}

    executor = ToolExecutor(effect_guard=CrashBeforeCompletion())
    executor.register(
        ToolSpec(
            tool_id="publish",
            description="observable external effect stand-in",
            risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
            timeout_seconds=30.0,
        ),
        publish,
    )
    asyncio.run(
        executor.execute(
            ToolCall(
                call_id="worker07-stable-call",
                tool_id="publish",
                task_id=task.task_id,
                arguments={"value": "hello"},
                approved=True,
            )
        )
    )
    raise AssertionError("hard-crash seam was not reached")
    """
)


def _spec() -> ToolSpec:
    return ToolSpec(
        tool_id="publish",
        description="observable external effect stand-in",
        risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        timeout_seconds=30.0,
    )


def _call(task_id: str) -> ToolCall:
    return ToolCall(
        call_id="worker07-stable-call",
        tool_id="publish",
        task_id=task_id,
        arguments={"value": "hello"},
        approved=True,
    )


def _marker_lines(marker: Path) -> list[str]:
    return marker.read_text(encoding="utf-8").splitlines()


def test_hard_crash_after_external_effect_requires_uncertain_reconciliation(tmp_path: Path) -> None:
    root = tmp_path / "ніка crash window"
    root.mkdir()
    database = root / "durable state.db"
    marker = root / "external effect.txt"
    task_file = root / "task id.txt"

    crashed = subprocess.run(
        [sys.executable, "-c", _CHILD, str(database), str(marker), str(task_file)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert crashed.returncode == 86, crashed.stderr
    task_id = task_file.read_text(encoding="utf-8")
    assert _marker_lines(marker) == ["published:hello"]

    store = SQLiteStore(database)
    store.initialize()
    ledger = IdempotencyLedger(store)
    records = ledger.list_for_task(task_id)
    assert len(records) == 1
    assert records[0].status is IdempotencyStatus.PENDING
    operation_key = records[0].operation_key

    async def would_duplicate(arguments: dict[str, object]) -> object:
        with marker.open("a", encoding="utf-8") as handle:
            handle.write(f"duplicate:{arguments['value']}\n")
        return {"published": arguments["value"]}

    restarted = ToolExecutor(effect_guard=ToolEffectGuard(ledger))
    restarted.register(_spec(), would_duplicate)
    blocked = asyncio.run(restarted.execute(_call(task_id)))

    assert not blocked.ok
    assert blocked.error == "tool effect not safe to execute"
    assert _marker_lines(marker) == ["published:hello"]

    recovery = RuntimeRecoveryService(
        queue=TaskQueue(store),
        audit=AuditLog(store),
        runtimes=RuntimeRegistry(),
        sessions=RuntimeSessionStore(store),
        idempotency=ledger,
    )
    candidate = next(item for item in recovery.inspect() if item.task_id == task_id)

    assert candidate.disposition is RecoveryDisposition.RECONCILE_SIDE_EFFECTS
    assert candidate.unresolved_operation_keys == (operation_key,)
    recovered = ledger.require(operation_key)
    assert recovered.status is IdempotencyStatus.UNCERTAIN

    ledger.reconcile_completed(
        operation_key,
        result={"completed": True, "output": {"published": "hello"}},
    )
    replayed = asyncio.run(restarted.execute(_call(task_id)))

    assert replayed.ok
    assert replayed.output == {"published": "hello"}
    assert _marker_lines(marker) == ["published:hello"]
    assert ledger.require(operation_key).status is IdempotencyStatus.COMPLETED
