from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.scheduler import ScheduledJob, ScheduledJobStore, TriggerKind


class RecordingSQLiteStore(SQLiteStore):
    """Record which durable rows cross the SQLite/Python hydration boundary."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.record_rows = False
        self.fetched_job_ids: list[str] = []

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with super().connection() as conn:
            if self.record_rows:

                def record(cursor: sqlite3.Cursor, row: tuple[object, ...]) -> sqlite3.Row:
                    columns = [column[0] for column in cursor.description]
                    if "job_id" in columns:
                        self.fetched_job_ids.append(row[columns.index("job_id")])
                    return sqlite3.Row(cursor, row)

                conn.row_factory = record
            yield conn


def _job(job_id: str, *, enabled: bool) -> ScheduledJob:
    return ScheduledJob(
        job_id=job_id,
        action_id="maintenance.cleanup",
        trigger_kind=TriggerKind.DATE,
        trigger={"run_date": "2030-01-01T12:00:00+00:00"},
        enabled=enabled,
    )


def test_startup_does_not_fetch_disabled_large_or_corrupt_payloads(
    tmp_path: Path,
) -> None:
    sqlite = RecordingSQLiteStore(tmp_path / "Українська папка" / "nika.db")
    sqlite.initialize()
    jobs = ScheduledJobStore(sqlite)
    jobs.upsert(_job("a-disabled", enabled=False))
    jobs.upsert(_job("b-live", enabled=True))
    jobs.upsert(_job("c-disabled", enabled=False))

    with sqlite.connection() as conn:
        conn.execute(
            "UPDATE scheduled_jobs SET payload_json = ? WHERE job_id = ?",
            (sqlite3.Binary(b"x" * 1_000_000), "a-disabled"),
        )
        conn.execute(
            "UPDATE scheduled_jobs SET payload_json = ? WHERE job_id = ?",
            ('{"broken":', "c-disabled"),
        )

    sqlite.record_rows = True
    assert jobs.list_enabled() == (_job("b-live", enabled=True),)
    assert sqlite.fetched_job_ids == ["b-live"]

    sqlite.record_rows = False
    with pytest.raises(ValueError, match="persisted payload is corrupt"):
        jobs.get("a-disabled")
    with pytest.raises(ValueError, match="persisted payload is corrupt"):
        jobs.get("c-disabled")


def test_invalid_enabled_storage_still_fails_closed_without_fetching_disabled(
    tmp_path: Path,
) -> None:
    sqlite = RecordingSQLiteStore(tmp_path / "nika.db")
    sqlite.initialize()
    jobs = ScheduledJobStore(sqlite)
    jobs.upsert(_job("a-disabled", enabled=False))
    jobs.upsert(_job("b-corrupt", enabled=True))
    jobs.upsert(_job("c-live", enabled=True))

    with sqlite.connection() as conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(
            "UPDATE scheduled_jobs SET enabled = ? WHERE job_id = ?",
            ("invalid", "b-corrupt"),
        )

    sqlite.record_rows = True
    with pytest.raises(ValueError, match="persisted enabled is corrupt"):
        jobs.list_enabled()
    assert "a-disabled" not in sqlite.fetched_job_ids
    assert "b-corrupt" not in sqlite.fetched_job_ids

    sqlite.record_rows = False
    with sqlite.connection() as conn:
        conn.execute("UPDATE scheduled_jobs SET enabled = 0 WHERE job_id = ?", ("b-corrupt",))
    sqlite.fetched_job_ids.clear()
    sqlite.record_rows = True
    assert jobs.list_enabled() == (_job("c-live", enabled=True),)
    assert sqlite.fetched_job_ids == ["c-live"]


@pytest.mark.parametrize(
    ("column", "carrier"),
    (
        ("trigger_json", "blob"),
        ("payload_json", "blob"),
        ("payload_json", "oversized-text"),
    ),
)
def test_active_invalid_json_never_crosses_hydration_boundary(
    tmp_path: Path, column: str, carrier: str
) -> None:
    sqlite = RecordingSQLiteStore(tmp_path / "Українська папка" / "nika.db")
    sqlite.initialize()
    jobs = ScheduledJobStore(sqlite)
    jobs.upsert(_job("a-corrupt", enabled=True))
    jobs.upsert(_job("b-live", enabled=True))
    jobs.upsert(_job("c-disabled", enabled=False))
    value = (
        sqlite3.Binary(b"x" * 2_000_000)
        if carrier == "blob"
        else "x" * 2_000_000
    )
    with sqlite.connection() as conn:
        conn.execute(
            f"UPDATE scheduled_jobs SET {column} = ? WHERE job_id = ?",
            (value, "a-corrupt"),
        )
        # An oversized disabled history must not block a valid enabled job.
        conn.execute(
            "UPDATE scheduled_jobs SET payload_json = ? WHERE job_id = ?",
            (sqlite3.Binary(b"x" * 2_000_000), "c-disabled"),
        )

    for current in (sqlite, RecordingSQLiteStore(sqlite.path)):
        current.record_rows = True
        current_jobs = ScheduledJobStore(current)
        label = "trigger" if column == "trigger_json" else "payload"
        with pytest.raises(ValueError, match=f"persisted {label} is corrupt"):
            current_jobs.list_enabled()
        assert current.fetched_job_ids == []

    sqlite.record_rows = False
    assert jobs.set_enabled("a-corrupt", False)
    sqlite.fetched_job_ids.clear()
    sqlite.record_rows = True
    assert jobs.list_enabled() == (_job("b-live", enabled=True),)
    assert sqlite.fetched_job_ids == ["b-live"]


def test_invalid_enabled_blob_rejected_without_materializing_payload(
    tmp_path: Path,
) -> None:
    sqlite = RecordingSQLiteStore(tmp_path / "nika.db")
    sqlite.initialize()
    jobs = ScheduledJobStore(sqlite)
    jobs.upsert(_job("a-corrupt", enabled=True))
    jobs.upsert(_job("b-live", enabled=True))
    with sqlite.connection() as conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(
            "UPDATE scheduled_jobs SET enabled = ?, payload_json = ? WHERE job_id = ?",
            (
                sqlite3.Binary(b"x" * 2_000_000),
                sqlite3.Binary(b"x" * 2_000_000),
                "a-corrupt",
            ),
        )

    for current in (sqlite, RecordingSQLiteStore(sqlite.path)):
        current.record_rows = True
        with pytest.raises(ValueError, match="persisted enabled is corrupt"):
            ScheduledJobStore(current).list_enabled()
        assert current.fetched_job_ids == []

    sqlite.record_rows = False
    assert jobs.set_enabled("a-corrupt", False)
    sqlite.fetched_job_ids.clear()
    sqlite.record_rows = True
    assert jobs.list_enabled() == (_job("b-live", enabled=True),)
    assert sqlite.fetched_job_ids == ["b-live"]
