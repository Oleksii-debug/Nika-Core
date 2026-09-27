from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.runtime.contracts import RuntimeOutcome, RuntimeResult
from nika_core.runtime.session_store import RuntimeSessionStore


class _BehavioralText(str):
    def strip(self, *args, **kwargs):
        del args, kwargs
        return "trusted"


def _store_with_task(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(workspace_id="workspace", agent_id="agent")
    return store, task.task_id


def _raw_session(store: SQLiteStore, task_id: str):
    with store.connection() as conn:
        return conn.execute(
            """
            SELECT task_id, runtime_id, thread_id, resume_token, outcome, updated_at
            FROM runtime_sessions
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()


@pytest.mark.parametrize("field_name", ["runtime_id", "thread_id"])
def test_record_active_rejects_behavioral_identity_carriers_without_write(
    tmp_path,
    field_name: str,
) -> None:
    store, task_id = _store_with_task(tmp_path)
    sessions = RuntimeSessionStore(store)
    values = {
        "task_id": task_id,
        "runtime_id": "runtime",
        "thread_id": "thread",
        "resume_token": "resume",
    }
    values[field_name] = _BehavioralText("")

    with pytest.raises(TypeError, match=field_name):
        sessions.record_active(**values)

    assert _raw_session(store, task_id) is None


def test_record_active_uses_underlying_resume_token_text_not_overridden_strip(
    tmp_path,
) -> None:
    store, task_id = _store_with_task(tmp_path)
    sessions = RuntimeSessionStore(store)

    with pytest.raises(ValueError, match="resume token"):
        sessions.record_active(
            task_id=task_id,
            runtime_id="runtime",
            thread_id="thread",
            resume_token=_BehavioralText(""),
        )

    assert _raw_session(store, task_id) is None


def test_lookup_rejects_noncanonical_task_identity_carrier(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    sessions = RuntimeSessionStore(store)
    sessions.record_active(
        task_id=task_id,
        runtime_id="runtime",
        thread_id="thread",
        resume_token="resume",
    )

    with pytest.raises(TypeError, match="task_id"):
        sessions.get(_BehavioralText(task_id))

    persisted = sessions.get(task_id)
    assert persisted is not None
    assert persisted.task_id == task_id


def test_persisted_blob_identity_fails_closed_without_normalization(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    sessions = RuntimeSessionStore(store)
    sessions.record_active(
        task_id=task_id,
        runtime_id="runtime",
        thread_id="thread",
        resume_token="resume",
    )

    with store.connection() as conn:
        conn.execute(
            "UPDATE runtime_sessions SET runtime_id = CAST(? AS BLOB) WHERE task_id = ?",
            ("forged-runtime", task_id),
        )

    with pytest.raises(RuntimeError, match=r"runtime_id.*SQLite storage class"):
        sessions.get(task_id)

    raw = _raw_session(store, task_id)
    assert raw is not None
    assert raw["runtime_id"] == b"forged-runtime"


def test_persisted_terminal_outcome_cannot_become_resumable_authority(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    sessions = RuntimeSessionStore(store)
    sessions.record_active(
        task_id=task_id,
        runtime_id="runtime",
        thread_id="thread",
        resume_token="resume",
    )

    with store.connection() as conn:
        conn.execute(
            "UPDATE runtime_sessions SET outcome = ? WHERE task_id = ?",
            (RuntimeOutcome.COMPLETED.value, task_id),
        )

    with pytest.raises(RuntimeError, match="outcome is not resumable"):
        sessions.get(task_id)

    raw = _raw_session(store, task_id)
    assert raw is not None
    assert raw["outcome"] == RuntimeOutcome.COMPLETED.value


def test_naive_session_epoch_is_not_silently_laundered_on_result_write(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    sessions = RuntimeSessionStore(store)
    sessions.record_active(
        task_id=task_id,
        runtime_id="runtime",
        thread_id="thread",
        resume_token="resume-1",
    )
    corrupted_epoch = "2026-09-27T12:00:00"
    with store.connection() as conn:
        conn.execute(
            "UPDATE runtime_sessions SET updated_at = ? WHERE task_id = ?",
            (corrupted_epoch, task_id),
        )

    with pytest.raises(RuntimeError, match="updated_at must be timezone-aware"):
        sessions.record_result(
            task_id=task_id,
            runtime_id="runtime",
            thread_id="thread",
            result=RuntimeResult(
                outcome=RuntimeOutcome.PAUSED,
                resume_token="resume-2",
            ),
        )

    raw = _raw_session(store, task_id)
    assert raw is not None
    assert raw["resume_token"] == "resume-1"
    assert raw["outcome"] == "__ACTIVE__"
    assert raw["updated_at"] == corrupted_epoch


def test_canonical_session_round_trip_preserves_utc_epoch(tmp_path) -> None:
    store, task_id = _store_with_task(tmp_path)
    sessions = RuntimeSessionStore(store)
    sessions.record_active(
        task_id=task_id,
        runtime_id="runtime",
        thread_id="thread",
        resume_token="resume-1",
    )

    active = sessions.get(task_id)
    assert active is not None
    active_time = datetime.fromisoformat(active.updated_at)
    assert active_time.utcoffset() == timedelta(0)

    sessions.record_result(
        task_id=task_id,
        runtime_id="runtime",
        thread_id="thread",
        result=RuntimeResult(
            outcome=RuntimeOutcome.PAUSED,
            resume_token="resume-2",
        ),
    )

    paused = sessions.get(task_id)
    assert paused is not None
    assert paused.outcome is RuntimeOutcome.PAUSED
    assert paused.resume_token == "resume-2"
    paused_time = datetime.fromisoformat(paused.updated_at)
    assert paused_time.utcoffset() == timedelta(0)
    assert paused_time >= active_time
