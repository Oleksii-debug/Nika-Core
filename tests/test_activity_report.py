from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.activity_report import ActivityCount, DailyActivityReportService
from nika_core.data.sqlite import SQLiteStore
from nika_core.resources.contracts import ResourceSnapshot


class _ResourceObserver:
    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=12.5,
            memory_percent=34.0,
            available_memory_bytes=4_000_000_000,
            battery_percent=77.0,
            power_plugged=True,
        )


def _prepared_store(tmp_path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.sqlite3")
    store.initialize()
    return store


def test_report_projects_canonical_truth_without_sensitive_payloads(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    inside = "2026-09-12T10:00:00+00:00"
    outside = "2026-09-13T00:00:00+00:00"

    with store.connection() as conn:
        conn.execute(
            "INSERT INTO tasks(task_id, workspace_id, agent_id, state, payload_json, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("task-1", "workspace-1", "agent-1", "COMPLETED", '{"secret":"TASK_SECRET"}', inside, inside),
        )
        conn.execute(
            "INSERT INTO task_events(task_id, previous_state, new_state, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("task-1", "RUNNING", "COMPLETED", inside),
        )
        conn.execute(
            "INSERT INTO task_events(task_id, previous_state, new_state, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("task-1", "COMPLETED", "ARCHIVED", outside),
        )
        conn.execute(
            "INSERT INTO audit_events(event_type, entity_type, entity_id, payload_json, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            ("learning.consulted\nunsafe", "task", "task-1", '{"secret":"AUDIT_SECRET"}', inside),
        )
        conn.execute(
            "INSERT INTO experiments(experiment_id, definition_json, status, selected_candidate_id, "
            "previous_champion_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("experiment-1", "{}", "completed", None, None, inside, inside),
        )
        conn.execute(
            "INSERT INTO experiment_events(experiment_id, previous_status, new_status, "
            "selected_candidate_id, previous_champion_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("experiment-1", "running", "completed", None, None, inside),
        )
        conn.execute(
            "INSERT INTO memory_records(scope, owner_id, namespace, memory_key, value_json, "
            "user_approved, expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("agent", "agent-1", "learning", "lesson-1", '"MEMORY_SECRET"', 1, None, inside, inside),
        )
        conn.execute(
            "INSERT INTO research_workspaces(workspace_id, name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("research-1", "Research", inside, inside),
        )
        conn.execute(
            "INSERT INTO corpus_documents(document_id, workspace_id, normalized_sha256, title, "
            "media_type, normalized_text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("document-1", "research-1", "a" * 64, "Private title", "text/plain", "DOC_SECRET", inside),
        )

    service = DailyActivityReportService(store, resource_observer=_ResourceObserver())
    report = service.build_window(
        start=datetime(2026, 9, 12, tzinfo=UTC),
        end=datetime(2026, 9, 13, tzinfo=UTC),
    )

    assert report.task_transitions == (ActivityCount("COMPLETED", 1),)
    assert report.audit_events == (ActivityCount("learning.consulted\nunsafe", 1),)
    assert report.experiment_transitions == (ActivityCount("completed", 1),)
    assert report.research_documents_added == 1
    assert report.memory_records_updated == 1
    assert report.resource_snapshot is not None

    rendered = report.render_text()
    assert "learning.consulted unsafe=1" in rendered
    assert "CPU 12.5%" in rendered
    assert "батарея 77.0%" in rendered
    assert "TASK_SECRET" not in rendered
    assert "AUDIT_SECRET" not in rendered
    assert "MEMORY_SECRET" not in rendered
    assert "DOC_SECRET" not in rendered
    assert "Private title" not in rendered
    assert "ARCHIVED" not in rendered


def test_report_uses_half_open_window_and_never_synthesizes_missing_activity(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    service = DailyActivityReportService(store)

    report = service.build_utc_day(datetime(2026, 9, 12, tzinfo=UTC).date())

    assert report.task_transitions == ()
    assert report.audit_events == ()
    assert report.experiment_transitions == ()
    assert report.research_documents_added == 0
    assert report.memory_records_updated == 0
    assert report.resource_snapshot is None
    assert "немає зафіксованих подій" in report.render_text()
    assert "не синтезується" in report.render_text()


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (
            datetime(2026, 9, 12, tzinfo=UTC).replace(tzinfo=None),
            datetime(2026, 9, 13, tzinfo=UTC),
            "start must be timezone-aware",
        ),
        (
            datetime(2026, 9, 12, tzinfo=UTC),
            datetime(2026, 9, 12, tzinfo=UTC),
            "end must be later than start",
        ),
    ],
)
def test_report_rejects_ambiguous_or_empty_windows(start, end, message, tmp_path) -> None:
    store = _prepared_store(tmp_path)
    service = DailyActivityReportService(store)

    with pytest.raises(ValueError, match=message):
        service.build_window(start=start, end=end)
