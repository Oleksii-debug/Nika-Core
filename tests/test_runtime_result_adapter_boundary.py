from __future__ import annotations

import asyncio

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeErrorCode,
    RuntimeEvent,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
    RuntimeResumeRequest,
    canonical_runtime_result,
)
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
    IdempotencyStatus,
)


_PRIVATE_MARKER = "SYNTHETIC_UNTRUSTED_ADAPTER_SECRET"


class _HostileResult:
    def __getattribute__(self, name: str):
        raise AssertionError(f"{_PRIVATE_MARKER}: accessed {name}")


class _ResultSubclass(RuntimeResult):
    pass


class _HostileBool:
    def __bool__(self):
        raise AssertionError(f"{_PRIVATE_MARKER}: boolean conversion attempted")


def _forged_result() -> RuntimeResult:
    result = RuntimeResult(outcome=RuntimeOutcome.COMPLETED)
    object.__setattr__(result, "outcome", "completed")
    return result


def _forged_events() -> RuntimeResult:
    result = RuntimeResult(outcome=RuntimeOutcome.COMPLETED)
    object.__setattr__(result, "events", object())
    return result


def _forged_event_sequence() -> RuntimeResult:
    event = RuntimeEvent(sequence=0, event_type="runtime.test")
    object.__setattr__(event, "sequence", "0")
    return RuntimeResult(outcome=RuntimeOutcome.COMPLETED, events=(event,))


def _invalid_event_payload() -> RuntimeResult:
    event = RuntimeEvent(
        sequence=0,
        event_type="runtime.test",
        payload={"unserializable": object()},
    )
    return RuntimeResult(outcome=RuntimeOutcome.COMPLETED, events=(event,))


def _forged_output() -> RuntimeResult:
    result = RuntimeResult(outcome=RuntimeOutcome.COMPLETED)
    object.__setattr__(result, "output", object())
    return result


def _forged_resume_token() -> RuntimeResult:
    result = RuntimeResult(outcome=RuntimeOutcome.COMPLETED)
    object.__setattr__(result, "resume_token", object())
    return result


def _forged_error() -> RuntimeResult:
    result = RuntimeResult(outcome=RuntimeOutcome.FAILED, error="failure")
    object.__setattr__(result, "error", object())
    return result


def _unserializable_nested_output() -> RuntimeResult:
    return RuntimeResult(
        outcome=RuntimeOutcome.COMPLETED,
        output={"nested": {"unsupported": object()}},
    )


def _nonfinite_output() -> RuntimeResult:
    return RuntimeResult(outcome=RuntimeOutcome.COMPLETED, output={"value": float("nan")})


def _invalid_output_key() -> RuntimeResult:
    return RuntimeResult(outcome=RuntimeOutcome.COMPLETED, output={1: "non-string"})


def _invalid_nested_output_key() -> RuntimeResult:
    return RuntimeResult(
        outcome=RuntimeOutcome.COMPLETED,
        output={"nested": [{1: "coerced-key"}]},
    )


def _invalid_nested_event_payload_key() -> RuntimeResult:
    return RuntimeResult(
        outcome=RuntimeOutcome.COMPLETED,
        events=(RuntimeEvent(0, "runtime.proof", {"nested": {"value": {True: "coerced"}}}),),
    )


def _invalid_utf8_error() -> RuntimeResult:
    return RuntimeResult(outcome=RuntimeOutcome.FAILED, error="\ud800")


def _invalid_utf8_resume_token() -> RuntimeResult:
    return RuntimeResult(outcome=RuntimeOutcome.PAUSED, resume_token="\ud800")


def _invalid_utf8_event_type() -> RuntimeResult:
    return RuntimeResult(
        outcome=RuntimeOutcome.COMPLETED,
        events=(RuntimeEvent(0, "\ud800"),),
    )




def _invalid_control_event_type() -> RuntimeResult:
    return RuntimeResult(
        outcome=RuntimeOutcome.COMPLETED,
        events=(RuntimeEvent(0, "runtime.proof\x00forged"),),
    )


def _invalid_bidi_event_type() -> RuntimeResult:
    return RuntimeResult(
        outcome=RuntimeOutcome.COMPLETED,
        events=(RuntimeEvent(0, "runtime.proof\u202eforged"),),
    )


def _invalid_utf8_event_payload() -> RuntimeResult:
    return RuntimeResult(
        outcome=RuntimeOutcome.COMPLETED,
        events=(RuntimeEvent(0, "runtime.test", {"text": "\ud800"}),),
    )


def _overridden_event_sequence() -> RuntimeResult:
    return RuntimeResult(
        outcome=RuntimeOutcome.COMPLETED,
        events=(RuntimeEvent(0, "runtime.test", {"sequence": 100}),),
    )


def _forged_nika_control_event() -> RuntimeResult:
    return RuntimeResult(
        outcome=RuntimeOutcome.COMPLETED,
        events=(RuntimeEvent(0, "runtime.cancel_accepted"),),
    )


_BAD_RESULTS = (
    lambda: None,
    object,
    _HostileResult,
    _forged_result,
    _forged_events,
    _forged_event_sequence,
    _invalid_event_payload,
    _forged_output,
    _forged_resume_token,
    _forged_error,
    lambda: _ResultSubclass(outcome=RuntimeOutcome.COMPLETED),
    _unserializable_nested_output,
    _nonfinite_output,
    _invalid_output_key,
    _invalid_nested_output_key,
    _invalid_nested_event_payload_key,
    _invalid_utf8_error,
    _invalid_utf8_resume_token,
    _invalid_utf8_event_type,
    _invalid_control_event_type,
    _invalid_bidi_event_type,
    _invalid_utf8_event_payload,
    _overridden_event_sequence,
    _forged_nika_control_event,
)


class _Adapter:
    runtime_id = "malformed-result-runtime"
    capabilities = frozenset({RuntimeCapability.CANCELLATION, RuntimeCapability.DURABLE_RESUME})

    def __init__(self, result: object, *, cancel_response: object = True) -> None:
        self.result = result
        self.cancel_response = cancel_response
        self.run_calls = 0
        self.resume_calls = 0
        self.cancel_calls = 0

    async def run(self, request: RuntimeRequest):
        del request
        self.run_calls += 1
        return self.result

    async def resume(self, request: RuntimeResumeRequest):
        del request
        self.resume_calls += 1
        return self.result

    async def cancel(self, *, task_id: str, thread_id: str):
        del task_id, thread_id
        self.cancel_calls += 1
        return self.cancel_response

    async def probe_resume(self, *, task_id: str, thread_id: str, resume_token: str):
        del task_id, thread_id, resume_token
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="existing durable checkpoint",
            checkpoint_id="checkpoint:malformed-result-runtime",
        )


def _ready(tmp_path):
    store = SQLiteStore(tmp_path / "Українські дані" / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(workspace_id="default", agent_id="nika.default")
    queue.transition(task.task_id, TaskState.READY)
    audit = AuditLog(store)
    return store, queue, task.task_id, audit, TaskRuntimeCoordinator(queue, audit)


@pytest.mark.parametrize("factory", _BAD_RESULTS)
def test_invalid_run_result_fails_closed_and_does_not_orphan_running_task(
    tmp_path, factory
):
    _, queue, task_id, audit, coordinator = _ready(tmp_path)
    runtime = _Adapter(factory())

    result = asyncio.run(coordinator.start(runtime, RuntimeRequest(task_id, "thread-1")))

    assert runtime.run_calls == 1
    assert result.outcome is RuntimeOutcome.FAILED
    assert result.error_code is RuntimeErrorCode.INTERNAL
    assert result.error == "runtime execution failed"
    assert queue.get(task_id).state is TaskState.FAILED
    assert coordinator.sessions.get(task_id) is None
    events = audit.list_for(entity_type="task", entity_id=task_id)
    assert events[-1].event_type == "runtime.finished"
    assert _PRIVATE_MARKER not in str(events)


@pytest.mark.parametrize("factory", _BAD_RESULTS)
def test_invalid_saved_resume_completes_claim_without_persisting_invalid_result(
    tmp_path, factory
):
    store, queue, task_id, audit, coordinator = _ready(tmp_path)
    queue.transition(task_id, TaskState.RUNNING)
    coordinator.sessions.record_active(
        task_id=task_id,
        runtime_id=_Adapter.runtime_id,
        thread_id="thread-1",
        resume_token="token-1",
    )
    runtime = _Adapter(factory())

    result = asyncio.run(coordinator.resume_saved(runtime, task_id=task_id))

    assert runtime.run_calls == 0
    assert runtime.resume_calls == 1
    assert result.outcome is RuntimeOutcome.FAILED
    assert result.error_code is RuntimeErrorCode.INTERNAL
    assert result.error == "runtime resume failed"
    assert queue.get(task_id).state is TaskState.FAILED
    assert coordinator.sessions.get(task_id) is None
    claims = [
        item for item in IdempotencyLedger(store).list_for_task(task_id)
        if item.operation_type == "runtime.recovery_resume"
    ]
    assert len(claims) == 1
    assert claims[0].status is IdempotencyStatus.COMPLETED
    assert _PRIVATE_MARKER not in str(claims)
    assert _PRIVATE_MARKER not in str(audit.list_for(entity_type="task", entity_id=task_id))


def test_valid_result_preserves_normal_coordinator_outcome_and_output(tmp_path):
    _, queue, task_id, _, coordinator = _ready(tmp_path)
    original = RuntimeResult(outcome=RuntimeOutcome.COMPLETED, output={"done": True})

    result = asyncio.run(
        coordinator.start(_Adapter(original), RuntimeRequest(task_id, "thread-1"))
    )

    assert result == original
    assert result.output == {"done": True}
    assert queue.get(task_id).state is TaskState.COMPLETED


def test_valid_runtime_events_are_snapshotted_before_audit(tmp_path):
    _, queue, task_id, audit, coordinator = _ready(tmp_path)
    original = RuntimeResult(
        outcome=RuntimeOutcome.COMPLETED,
        events=(RuntimeEvent(0, "runtime.proof", {"status": "ok"}),),
    )

    result = asyncio.run(
        coordinator.start(_Adapter(original), RuntimeRequest(task_id, "thread-1"))
    )

    assert result == original
    assert queue.get(task_id).state is TaskState.COMPLETED
    events = audit.list_for(entity_type="task", entity_id=task_id)
    assert "runtime.proof" in [event.event_type for event in events]


def test_canonical_result_rejects_forgery_without_consulting_hostile_properties():
    for factory in _BAD_RESULTS:
        with pytest.raises((TypeError, ValueError)):
            canonical_runtime_result(factory())


@pytest.mark.parametrize(
    "event_type",
    (
        "runtime.started",
        "runtime.finished",
        "runtime.cancel_accepted",
        "runtime.recovery_claim_completed",
        "runtime.recovery_auto_resume_failed",
    ),
)
def test_adapter_cannot_impersonate_authoritative_audit_events(event_type):
    result = RuntimeResult(
        outcome=RuntimeOutcome.COMPLETED,
        events=(RuntimeEvent(0, event_type),),
    )
    with pytest.raises(ValueError, match="cannot impersonate"):
        canonical_runtime_result(result)


def test_legitimate_langgraph_approval_event_remains_supported():
    result = RuntimeResult(
        outcome=RuntimeOutcome.WAITING_APPROVAL,
        resume_token="token-1",
        events=(RuntimeEvent(0, "runtime.approval_requested", {"value": "review"}),),
    )
    assert canonical_runtime_result(result) == result


def test_result_snapshot_detaches_nested_event_payload_and_output():
    nested_output = {"items": [{"count": 1}]}
    nested_payload = {"items": [{"status": "ready"}]}
    original = RuntimeResult(
        outcome=RuntimeOutcome.COMPLETED,
        output={"nested": nested_output, "текст": "перевірка"},
        events=(RuntimeEvent(0, "runtime.proof", {"nested": nested_payload}),),
    )

    canonical = canonical_runtime_result(original)
    nested_output["items"][0]["count"] = 999
    nested_payload["items"][0]["status"] = "changed"

    assert canonical.output == {
        "nested": {"items": [{"count": 1}]},
        "текст": "перевірка",
    }
    assert canonical.events[0].payload == {
        "nested": {"items": [{"status": "ready"}]}
    }
    assert canonical.output is not original.output
    assert canonical.events[0].payload is not original.events[0].payload


@pytest.mark.parametrize("response", (None, 0, 1, "false", object(), _HostileBool()))
def test_malformed_pause_acknowledgement_keeps_external_effect_uncertain(
    tmp_path, response
):
    store, queue, task_id, audit, coordinator = _ready(tmp_path)
    queue.transition(task_id, TaskState.RUNNING)
    coordinator.sessions.record_active(
        task_id=task_id,
        runtime_id=_Adapter.runtime_id,
        thread_id="thread-1",
        resume_token="token-1",
    )
    runtime = _Adapter(None, cancel_response=response)

    with pytest.raises(
        TypeError,
        match="pause cancellation acknowledgement must be boolean",
    ):
        asyncio.run(coordinator.pause(runtime, task_id=task_id, thread_id="thread-1"))

    assert runtime.cancel_calls == 1
    assert queue.get(task_id).state is TaskState.RUNNING
    assert coordinator.sessions.get(task_id) is not None
    operations = [
        item
        for item in IdempotencyLedger(store).list_for_task(task_id)
        if item.operation_type == "runtime.pause"
    ]
    assert len(operations) == 1
    assert operations[0].status is IdempotencyStatus.UNCERTAIN
    events = audit.list_for(entity_type="task", entity_id=task_id)
    assert [event.event_type for event in events] == [
        "runtime.pause_requested",
        "runtime.pause_uncertain",
    ]
    assert events[-1].payload["error_type"] == "TypeError"
    assert _PRIVATE_MARKER not in str(events)

    with pytest.raises(IdempotencyConflictError, match="already has durable authority"):
        asyncio.run(coordinator.pause(runtime, task_id=task_id, thread_id="thread-1"))
    assert runtime.cancel_calls == 1


@pytest.mark.parametrize("response", (None, 0, 1, "false", object(), _HostileBool()))
def test_malformed_cancel_acknowledgement_keeps_external_effect_uncertain(
    tmp_path, response
):
    store, queue, task_id, audit, coordinator = _ready(tmp_path)
    queue.transition(task_id, TaskState.RUNNING)
    coordinator.sessions.record_active(
        task_id=task_id,
        runtime_id=_Adapter.runtime_id,
        thread_id="thread-1",
        resume_token="token-1",
    )
    runtime = _Adapter(None, cancel_response=response)

    with pytest.raises(TypeError, match="cancellation acknowledgement must be boolean"):
        asyncio.run(coordinator.cancel(runtime, task_id=task_id, thread_id="thread-1"))

    assert runtime.cancel_calls == 1
    assert queue.get(task_id).state is TaskState.RUNNING
    assert coordinator.sessions.get(task_id) is not None
    operations = IdempotencyLedger(store).list_for_task(task_id)
    assert len(operations) == 1
    assert operations[0].operation_type == "runtime.cancel"
    assert operations[0].status is IdempotencyStatus.UNCERTAIN
    events = audit.list_for(entity_type="task", entity_id=task_id)
    assert [event.event_type for event in events] == [
        "runtime.cancel_requested",
        "runtime.cancel_uncertain",
    ]
    assert events[-1].payload["error"] == "TypeError"
    assert _PRIVATE_MARKER not in str(events)
    with pytest.raises(IdempotencyConflictError, match="pending or uncertain"):
        asyncio.run(coordinator.cancel(runtime, task_id=task_id, thread_id="thread-1"))
    assert runtime.cancel_calls == 1


@pytest.mark.parametrize(
    "character", ("\x00", "\n", "\u007f", "\u0085", "\u2028", "\u2029",
                  "\u202e", "\u2066")
)
def test_runtime_event_type_control_characters_are_rejected(character):
    result = RuntimeResult(
        outcome=RuntimeOutcome.COMPLETED,
        events=(RuntimeEvent(0, f"reference.{character}completed"),),
    )
    with pytest.raises(ValueError, match="control or formatting characters"):
        canonical_runtime_result(result)


def test_valid_unicode_runtime_event_names_remain_supported():
    result = RuntimeResult(
        outcome=RuntimeOutcome.COMPLETED,
        events=(RuntimeEvent(0, "подія.готова", {"підсумок": True}),),
    )
    canonical = canonical_runtime_result(result)
    assert canonical.events == result.events
