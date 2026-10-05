from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.batch_cursor import (
    _COMPLETION_ENVELOPE_KEY,
    _MAX_VALUE_BYTES,
    _json_copy,
    AttemptState,
    BatchCursor,
    BatchCursorState,
    BatchCursorStateError,
    BatchTargetSpec,
    TargetCursor,
)
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.memory import MemoryScope, MemoryService
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus


def _services(tmp_path: Path) -> tuple[MemoryService, IdempotencyLedger, str]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task_id = TaskQueue(store).create(
        workspace_id="batch-json-tests",
        agent_id="batch-json-tests",
    ).task_id
    return MemoryService(store), IdempotencyLedger(store), task_id


def _cursor(
    memory: MemoryService,
    ledger: IdempotencyLedger,
    *,
    task_id: str,
) -> BatchCursor:
    return BatchCursor.create(
        memory,
        ledger,
        task_id=task_id,
        cursor_id="cursor",
        targets=[BatchTargetSpec(target_id="перший", payload={"text": "привіт"})],
        batch_size=1,
    )


def _deep(depth: int) -> object:
    value: object = "end"
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize(
    "bad",
    [
        {"number": float("nan")},
        {"number": float("inf")},
        {"number": -float("inf")},
        {"bad": "\ud800"},
        {"\udfff": "bad key"},
        {1: "coerced key"},
        {"nested": {False: "coerced key"}},
        {"unsupported": object()},
        {"wide": [None] * 10_001},
        {"too_deep": _deep(33)},
        {"oversized": "я" * (_MAX_VALUE_BYTES // 2 + 1)},
        {"k" * (_MAX_VALUE_BYTES + 1): 1},
        {"integer": 1 << 4_097},
    ],
)
def test_untrusted_json_values_fail_closed(bad: dict[str, object]) -> None:
    with pytest.raises(
        BatchCursorStateError, match="JSON-serializable|valid UTF-8 JSON text"
    ):
        _json_copy(bad)


def test_cyclic_containers_fail_closed_without_recursion_error() -> None:
    cycle: dict[str, object] = {}
    cycle["self"] = cycle
    with pytest.raises(BatchCursorStateError, match="JSON-serializable"):
        _json_copy(cycle)


def test_shared_subtree_is_safe_and_detached() -> None:
    shared = {"value": "два"}
    source = {"first": shared, "second": shared}
    result = _json_copy(source)
    assert result == source
    assert result["first"] is not result["second"]


def test_exact_byte_boundary_and_depth_limit_remain_supported() -> None:
    maximum = {"x": "a" * (_MAX_VALUE_BYTES - len('{"x":""}'))}
    assert _json_copy(maximum) == maximum
    nested = {"nested": _deep(31)}
    assert _json_copy(nested) == nested


@pytest.mark.parametrize("bad_id", ["\ud800", "\udfff"])
def test_invalid_utf8_identity_fails_before_cursor_write(
    tmp_path: Path, bad_id: str
) -> None:
    memory, ledger, task_id = _services(tmp_path)
    with pytest.raises(ValueError, match="UTF-8"):
        BatchCursor.create(
            memory,
            ledger,
            task_id=task_id,
            cursor_id="cursor",
            targets=[BatchTargetSpec(target_id=bad_id)],
            batch_size=1,
        )
    assert ledger.list_for_task(task_id) == ()
    assert memory.get(
        scope=MemoryScope.TASK,
        owner_id=task_id,
        namespace="v01.batch_cursor",
        key="cursor",
    ) is None


def test_invalid_input_payload_cannot_create_cursor_or_effect(tmp_path: Path) -> None:
    memory, ledger, task_id = _services(tmp_path)
    with pytest.raises(BatchCursorStateError, match="JSON-serializable"):
        BatchCursor.create(
            memory,
            ledger,
            task_id=task_id,
            cursor_id="cursor",
            targets=[
                BatchTargetSpec(target_id="valid", payload={"number": float("nan")})
            ],
            batch_size=1,
        )
    assert ledger.list_for_task(task_id) == ()
    assert memory.get(
        scope=MemoryScope.TASK,
        owner_id=task_id,
        namespace="v01.batch_cursor",
        key="cursor",
    ) is None


@pytest.mark.parametrize(
    "bad_result",
    [
        {"number": float("nan")},
        {"nested": _deep(33)},
        {"oversized": "a" * _MAX_VALUE_BYTES},
    ],
)
def test_invalid_completion_cannot_complete_durable_effect(
    tmp_path: Path, bad_result: dict[str, object]
) -> None:
    memory, ledger, task_id = _services(tmp_path)
    cursor = _cursor(memory, ledger, task_id=task_id)
    grant = cursor.begin_effect("перший")
    assert grant.execute is True

    with pytest.raises(BatchCursorStateError, match="JSON-serializable"):
        cursor.confirm("перший", bad_result)

    assert ledger.require(grant.operation_key).status is IdempotencyStatus.PENDING
    assert cursor.state.targets[0].attempt_state is AttemptState.IN_FLIGHT


def test_invalid_uncertainty_evidence_cannot_partially_mark_ledger(
    tmp_path: Path,
) -> None:
    memory, ledger, task_id = _services(tmp_path)
    cursor = _cursor(memory, ledger, task_id=task_id)
    grant = cursor.begin_effect("перший")

    with pytest.raises(BatchCursorStateError, match="JSON-serializable"):
        cursor.mark_uncertain("перший", {"number": float("nan")})

    assert ledger.require(grant.operation_key).status is IdempotencyStatus.PENDING
    assert cursor.state.targets[0].attempt_state is AttemptState.IN_FLIGHT


def test_ambiguous_legacy_completion_fails_closed_on_restart(tmp_path: Path) -> None:
    memory, ledger, task_id = _services(tmp_path)
    cursor = _cursor(memory, ledger, task_id=task_id)
    grant = cursor.begin_effect("перший")
    ledger.complete(
        grant.operation_key,
        {
            _COMPLETION_ENVELOPE_KEY: {
                "result": {"approved": False},
                "next_batch_not_before": None,
            },
            "forged_success": True,
        },
    )

    with pytest.raises(BatchCursorStateError, match="ambiguous"):
        BatchCursor.restore(
            memory,
            ledger,
            task_id=task_id,
            cursor_id="cursor",
            targets=[BatchTargetSpec(target_id="перший", payload={"text": "привіт"})],
            batch_size=1,
        )
    assert ledger.require(grant.operation_key).status is IdempotencyStatus.COMPLETED


def test_pre_envelope_legacy_result_remains_restorable(tmp_path: Path) -> None:
    memory, ledger, task_id = _services(tmp_path)
    cursor = _cursor(memory, ledger, task_id=task_id)
    grant = cursor.begin_effect("перший")
    ledger.complete(grant.operation_key, {"remote_id": "доказ"})

    restarted = BatchCursor.restore(
        memory,
        ledger,
        task_id=task_id,
        cursor_id="cursor",
        targets=[BatchTargetSpec(target_id="перший", payload={"text": "привіт"})],
        batch_size=1,
    )
    assert restarted.state.confirmed_count == 1
    assert restarted.state.targets[0].confirmed_result == {"remote_id": "доказ"}


def test_missing_durable_completion_never_accepts_a_new_caller_claim(
    tmp_path: Path,
) -> None:
    memory, ledger, task_id = _services(tmp_path)
    cursor = _cursor(memory, ledger, task_id=task_id)
    grant = cursor.begin_effect("перший")
    ledger.complete(grant.operation_key)

    with pytest.raises(BatchCursorStateError, match="missing durable result"):
        cursor.confirm("перший", {"claimed": "success"})
    assert cursor.state.targets[0].attempt_state is AttemptState.IN_FLIGHT

    with pytest.raises(BatchCursorStateError, match="missing durable result"):
        BatchCursor.restore(
            memory,
            ledger,
            task_id=task_id,
            cursor_id="cursor",
            targets=[BatchTargetSpec(target_id="перший", payload={"text": "привіт"})],
            batch_size=1,
        )
    assert ledger.require(grant.operation_key).status is IdempotencyStatus.COMPLETED


@pytest.mark.parametrize("bad_carrier", [None, [], "text", True, 3])
def test_non_object_carriers_fail_before_effect_completion_or_uncertainty(
    tmp_path: Path, bad_carrier: object
) -> None:
    memory, ledger, task_id = _services(tmp_path)
    cursor = _cursor(memory, ledger, task_id=task_id)
    grant = cursor.begin_effect("перший")

    with pytest.raises(TypeError, match="exact JSON object"):
        cursor.confirm("перший", bad_carrier)
    with pytest.raises(TypeError, match="exact JSON object"):
        cursor.mark_uncertain("перший", bad_carrier)

    assert ledger.require(grant.operation_key).status is IdempotencyStatus.PENDING
    assert cursor.state.targets[0].attempt_state is AttemptState.IN_FLIGHT


@pytest.mark.parametrize("bad_version", [True, 1.0, "1"])
def test_persisted_cursor_version_requires_exact_integer(
    tmp_path: Path,
    bad_version: object,
) -> None:
    memory, ledger, task_id = _services(tmp_path)
    state = _cursor(memory, ledger, task_id=task_id).state.model_dump(mode="json")
    state["version"] = bad_version

    with pytest.raises(ValueError, match="cursor version must be an exact integer"):
        BatchCursorState.model_validate(state)


def test_persisted_cursor_version_preserves_exact_integer(tmp_path: Path) -> None:
    memory, ledger, task_id = _services(tmp_path)
    state = _cursor(memory, ledger, task_id=task_id).state.model_dump(mode="json")
    state["version"] = 1

    restored = BatchCursorState.model_validate(state)
    assert type(restored.version) is int
    assert restored.version == 1


def test_persisted_input_count_does_not_drive_range_allocation() -> None:
    with pytest.raises(ValueError, match="input positions do not match input_count"):
        BatchCursorState.model_validate(
            {
                "version": 1,
                "task_id": "task",
                "cursor_id": "cursor",
                "batch_size": 1,
                "input_count": 1 << 62,
                "ready_batch_index": 0,
                "plan_fingerprint": "f" * 64,
                "targets": [],
                "next_scheduled_intent": None,
            }
        )


def _persisted_target_payload(
    *,
    payload: dict[str, object] | None = None,
    attempt_state: AttemptState = AttemptState.PENDING,
    confirmed_result: dict[str, object] | None = None,
    uncertain_result: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "target_id": "persisted-target",
        "payload": {"ok": True} if payload is None else payload,
        "position": 0,
        "batch_index": 0,
        "batch_position": 0,
        "input_positions": [0],
        "input_fingerprint": "f" * 64,
        "operation_key": "persisted-operation",
        "attempt_state": attempt_state.value,
        "attempts": 1 if attempt_state is not AttemptState.PENDING else 0,
        "confirmed_result": confirmed_result,
        "uncertain_result": uncertain_result,
    }


@pytest.mark.parametrize(
    ("carrier", "match"),
    [
        (
            _persisted_target_payload(
                payload={"oversized": "a" * _MAX_VALUE_BYTES},
            ),
            "target payload must satisfy bounded JSON admission",
        ),
        (
            _persisted_target_payload(
                payload={"too_deep": _deep(33)},
            ),
            "target payload must satisfy bounded JSON admission",
        ),
        (
            _persisted_target_payload(
                attempt_state=AttemptState.CONFIRMED,
                confirmed_result={"oversized": "a" * _MAX_VALUE_BYTES},
            ),
            "confirmed_result must satisfy bounded JSON admission",
        ),
        (
            _persisted_target_payload(
                attempt_state=AttemptState.UNCERTAIN,
                uncertain_result={"too_deep": _deep(33)},
            ),
            "uncertain_result must satisfy bounded JSON admission",
        ),
    ],
)
def test_persisted_target_values_use_bounded_json_admission(
    carrier: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        TargetCursor.model_validate(carrier)

