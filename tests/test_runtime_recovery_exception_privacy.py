from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.recovery import (
    RecoveryCandidate,
    RecoveryDisposition,
    RuntimeRecoveryService,
)
from nika_core.runtime.registry import RuntimeRegistry


@pytest.mark.parametrize("failure_type", [OSError, RuntimeError])
def test_provider_error_text_is_not_persisted_during_startup_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[Exception],
) -> None:
    store = SQLiteStore(tmp_path / "Дані Nika" / "ніка.db")
    store.initialize()
    queue = TaskQueue(store)
    audit = AuditLog(store)
    registry = RuntimeRegistry()
    recovery = RuntimeRecoveryService(queue=queue, audit=audit, runtimes=registry)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "Resume under policy"},
    )
    candidate = RecoveryCandidate(
        task_id=task.task_id,
        runtime_id="test-runtime",
        thread_id="test-thread",
        task_state=TaskState.RUNNING,
        stored_outcome=None,
        disposition=RecoveryDisposition.AUTO_RESUME_CRASH,
        reason="eligible for checkpoint preflight",
    )
    secret = "PRIVATE_API_KEY_AND_LOCAL_PATH"
    failure = failure_type(secret)

    def fail_runtime_lookup(_runtime_id: str) -> None:
        raise failure

    monkeypatch.setattr(recovery, "inspect", lambda: (candidate,))
    monkeypatch.setattr(registry, "get", fail_runtime_lookup)

    result = asyncio.run(recovery.resume_safe_crash_sessions(max_count=1))

    assert len(result) == 1
    assert not result[0].succeeded
    assert result[0].candidate is candidate
    assert result[0].error == failure_type.__name__
    events = audit.list_for(entity_type="task", entity_id=task.task_id)
    assert [event.event_type for event in events] == [
        "runtime.recovery_auto_resume_failed"
    ]
    assert events[0].payload["error"] == failure_type.__name__
    assert events[0].payload["runtime_id"] == "test-runtime"
    assert events[0].payload["thread_id"] == "test-thread"
    assert secret not in json.dumps(events[0].payload, ensure_ascii=False)
    assert secret not in result[0].error
