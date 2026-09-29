from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.scheduler.contracts import ScheduledJob, TriggerKind
from nika_core.scheduler.store import (
    IMMUTABLE_JOB_BINDING_KEY,
    ScheduledJobStore,
)


class BehavioralJobId(str):
    def __str__(self) -> str:
        raise AssertionError("behavioral identifier conversion must not run")


def _sqlite(tmp_path: Path) -> SQLiteStore:
    sqlite = SQLiteStore(tmp_path / "nika.sqlite3")
    sqlite.initialize()
    return sqlite


def _store(tmp_path: Path) -> ScheduledJobStore:
    return ScheduledJobStore(_sqlite(tmp_path))


def _job(**overrides: object) -> ScheduledJob:
    values: dict[str, object] = {
        "job_id": "job-1",
        "action_id": "test.action",
        "trigger_kind": TriggerKind.DATE,
        "trigger": {"run_date": "2030-01-01T12:00:00+00:00"},
        "payload": {
            "task_id": "task-1",
            IMMUTABLE_JOB_BINDING_KEY: "binding-1",
            "nested": {"items": [1, "two", True, None]},
        },
        "enabled": True,
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_seconds": None,
    }
    values.update(overrides)
    return ScheduledJob(**values)  # type: ignore[arg-type]


def test_behavioral_job_carriers_fail_before_behavior(tmp_path: Path) -> None:
    class BehavioralText(str):
        def strip(self, *args: object, **kwargs: object) -> str:
            del args, kwargs
            raise AssertionError("behavioral string method must not run")

    class BehavioralInt(int):
        def __le__(self, other: object) -> bool:
            del other
            raise AssertionError("behavioral integer comparison must not run")

    class BehavioralDict(dict[str, object]):
        def items(self):
            raise AssertionError("behavioral mapping iteration must not run")

    store = _store(tmp_path)

    with pytest.raises(TypeError, match="job_id must be an exact string"):
        store.upsert(_job(job_id=BehavioralText("job-1")))
    with pytest.raises(ValueError, match="positive exact integer"):
        store.upsert(_job(max_instances=BehavioralInt(1)))
    with pytest.raises(TypeError, match="trigger must be an exact dict"):
        store.upsert(
            _job(
                trigger=BehavioralDict(
                    {"run_date": "2030-01-01T12:00:00+00:00"}
                )
            )
        )

    assert store.get("job-1") is None


def test_nested_payload_is_exact_finite_json_and_detached(tmp_path: Path) -> None:
    class BehavioralText(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            del args, kwargs
            raise AssertionError("behavioral text encoding must not run")

    store = _store(tmp_path)

    with pytest.raises(TypeError, match="exact JSON-compatible"):
        store.upsert(_job(payload={"nested": [BehavioralText("secret")]}))
    with pytest.raises(ValueError, match="non-finite"):
        store.upsert(_job(payload={"score": float("nan")}))

    nested = ["before"]
    store.upsert(_job(payload={"nested": nested}))
    nested.append("after")

    restored = store.get("job-1")
    assert restored is not None
    assert restored.payload == {"nested": ["before"]}


def test_scheduler_policy_carriers_are_exact(tmp_path: Path) -> None:
    class BoolLike(int):
        pass

    store = _store(tmp_path)

    with pytest.raises(TypeError, match="enabled and coalesce"):
        store.upsert(_job(enabled=BoolLike(1)))
    with pytest.raises(ValueError, match="positive exact integer"):
        store.upsert(_job(misfire_grace_seconds=BoolLike(60)))

    store.upsert(_job())
    with pytest.raises(TypeError, match="enabled must be an exact bool"):
        store.set_enabled("job-1", BoolLike(0))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="job_id must be an exact string"):
        store.get(BehavioralJobId("job-1"))


@pytest.mark.parametrize(
    ("column", "value", "message"),
    (
        ("enabled", "2", "persisted enabled is corrupt"),
        ("coalesce", "-1", "persisted coalesce is corrupt"),
        ("max_instances", "0", "persisted max_instances is corrupt"),
    ),
)
def test_persisted_numeric_corruption_fails_closed(
    tmp_path: Path,
    column: str,
    value: str,
    message: str,
) -> None:
    sqlite = _sqlite(tmp_path)
    store = ScheduledJobStore(sqlite)
    store.upsert(_job())

    with sqlite.connection() as conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(
            f"UPDATE scheduled_jobs SET {column} = ? WHERE job_id = ?",
            (value, "job-1"),
        )

    with pytest.raises(ValueError, match=message):
        store.get("job-1")


def test_persisted_nonfinite_json_fails_closed(tmp_path: Path) -> None:
    sqlite = _sqlite(tmp_path)
    store = ScheduledJobStore(sqlite)
    store.upsert(_job())

    with sqlite.connection() as conn:
        conn.execute(
            "UPDATE scheduled_jobs SET payload_json = ? WHERE job_id = ?",
            ('{"score":NaN}', "job-1"),
        )

    with pytest.raises(ValueError, match="persisted payload is corrupt"):
        store.get("job-1")


def test_existing_corrupt_binding_cannot_be_overwritten(tmp_path: Path) -> None:
    sqlite = _sqlite(tmp_path)
    store = ScheduledJobStore(sqlite)
    store.upsert(_job())

    with sqlite.connection() as conn:
        conn.execute(
            "UPDATE scheduled_jobs SET payload_json = ? WHERE job_id = ?",
            ('{"_nika_immutable_job_binding_v1":NaN}', "job-1"),
        )

    with pytest.raises(ValueError, match="persisted payload is corrupt"):
        store.upsert(_job(payload={IMMUTABLE_JOB_BINDING_KEY: "binding-1"}))


def test_existing_binding_cannot_be_removed_by_upsert(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert(_job())

    with pytest.raises(ValueError, match="immutable binding conflict"):
        store.upsert(_job(payload={"task_id": "task-1"}))

    restored = store.get("job-1")
    assert restored is not None
    assert restored.payload[IMMUTABLE_JOB_BINDING_KEY] == "binding-1"


def test_corrupt_existing_payload_cannot_be_silently_replaced(tmp_path: Path) -> None:
    sqlite = _sqlite(tmp_path)
    store = ScheduledJobStore(sqlite)
    store.upsert(_job())

    with sqlite.connection() as conn:
        conn.execute(
            "UPDATE scheduled_jobs SET payload_json = ? WHERE job_id = ?",
            ('{"nested":[1,2,3}', "job-1"),
        )

    with pytest.raises(ValueError, match="persisted payload is corrupt"):
        store.upsert(_job(payload={"replacement": True}))


@pytest.mark.parametrize(
    "stored_payload",
    (
        '{"_nika_immutable_job_binding_v1":7}',
        '{"_nika_immutable_job_binding_v1":"   "}',
    ),
)
def test_invalid_persisted_binding_fails_closed(
    tmp_path: Path,
    stored_payload: str,
) -> None:
    sqlite = _sqlite(tmp_path)
    store = ScheduledJobStore(sqlite)
    store.upsert(_job())

    with sqlite.connection() as conn:
        conn.execute(
            "UPDATE scheduled_jobs SET payload_json = ? WHERE job_id = ?",
            (stored_payload, "job-1"),
        )

    with pytest.raises(ValueError, match="persisted scheduled job immutable binding"):
        store.upsert(_job())


def test_binding_can_be_introduced_when_existing_job_has_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert(_job(payload={"task_id": "task-1"}))

    store.upsert(
        _job(
            payload={
                "task_id": "task-1",
                IMMUTABLE_JOB_BINDING_KEY: "binding-1",
            }
        )
    )

    restored = store.get("job-1")
    assert restored is not None
    assert restored.payload[IMMUTABLE_JOB_BINDING_KEY] == "binding-1"
