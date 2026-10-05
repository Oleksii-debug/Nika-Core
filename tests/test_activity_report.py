from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from nika_core.activity_report import ActivityCount, DailyActivityReportService
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.memory.contracts import MemoryScope
from nika_core.memory.service import MemoryService
from nika_core.resources.contracts import ResourceSnapshot


class _QueryOnlyTrackingStore(SQLiteStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.observed_query_only: int | None = None

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with super().connection() as conn:
            yield conn
            row = conn.execute("PRAGMA query_only").fetchone()
            assert row is not None
            self.observed_query_only = int(row[0])


class _AuditMutationConnection:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        after_grouped_audit_read: Callable[[], None],
    ) -> None:
        self._conn = conn
        self._after_grouped_audit_read = after_grouped_audit_read
        self._fired = False

    def execute(self, sql: str, parameters=()):
        cursor = self._conn.execute(sql, parameters)
        if (
            not self._fired
            and "FROM audit_events WHERE created_at" in sql
            and "GROUP BY event_type" in sql
        ):
            self._fired = True
            self._after_grouped_audit_read()
        return cursor


class _ConcurrentAuditMutationStore(SQLiteStore):
    def __init__(self, path: Path, *, created_at: str) -> None:
        super().__init__(path)
        self._created_at = created_at
        self.writer_committed = False

    @contextmanager
    def connection(self) -> Iterator[_AuditMutationConnection]:
        with super().connection() as conn:
            yield _AuditMutationConnection(
                conn,
                after_grouped_audit_read=self._commit_memory_event,
            )

    def _commit_memory_event(self) -> None:
        writer = sqlite3.connect(self.path, timeout=2.0)
        try:
            writer.execute(
                "INSERT INTO audit_events(event_type, entity_type, entity_id, payload_json, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    "memory.upserted",
                    "memory",
                    "concurrent-memory",
                    "{}",
                    self._created_at,
                ),
            )
            writer.commit()
            self.writer_committed = True
        finally:
            writer.close()


class _ResourceObserver:
    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=12.5,
            memory_percent=34.0,
            available_memory_bytes=4_000_000_000,
            battery_percent=77.0,
            power_plugged=True,
        )


class _StaticResourceObserver:
    def __init__(self, value: object) -> None:
        self.value = value

    def snapshot(self) -> ResourceSnapshot:
        return self.value  # type: ignore[return-value]


class _FailingResourceObserver:
    def snapshot(self) -> ResourceSnapshot:
        raise RuntimeError("provider-secret-must-not-escape")


class _BehavioralDatetime(datetime):
    def utcoffset(self) -> object:
        raise AssertionError("datetime behavior must not run before exact-type admission")


class _BehavioralDate(date):
    def timetuple(self) -> object:
        raise AssertionError("date behavior must not run before exact-type admission")


def _prepared_store(tmp_path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.sqlite3")
    store.initialize()
    return store


def test_report_projects_canonical_truth_without_sensitive_payloads(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    inside = "2026-09-12T10:00:00+00:00"
    outside = "2026-09-13T00:00:00+00:00"
    unsafe_event = "learning.consulted\nunsafe\x1b[31m\x07\u200d"

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
            (unsafe_event, "task", "task-1", '{"secret":"AUDIT_SECRET"}', inside),
        )
        conn.execute(
            "INSERT INTO audit_events(event_type, entity_type, entity_id, payload_json, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (
                "memory.upserted",
                "memory",
                "agent:agent-1:learning:lesson-1",
                '{"secret":"MEMORY_AUDIT_SECRET"}',
                inside,
            ),
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
    assert report.audit_events == (
        ActivityCount(unsafe_event, 1),
        ActivityCount("memory.upserted", 1),
    )
    assert report.experiment_transitions == (ActivityCount("completed", 1),)
    assert report.research_documents_added == 1
    assert report.memory_update_events == 1
    assert report.resource_snapshot is not None

    rendered = report.render_text()
    assert "learning.consulted unsafe [31m=1" in rendered
    assert "Події оновлення пам'яті: 1" in rendered
    assert "\x1b" not in rendered
    assert "\x07" not in rendered
    assert "\u200d" not in rendered
    assert "CPU 12.5%" in rendered
    assert "батарея 77.0%" in rendered
    assert "TASK_SECRET" not in rendered
    assert "AUDIT_SECRET" not in rendered
    assert "MEMORY_AUDIT_SECRET" not in rendered
    assert "MEMORY_SECRET" not in rendered
    assert "DOC_SECRET" not in rendered
    assert "Private title" not in rendered
    assert "ARCHIVED" not in rendered


@pytest.mark.parametrize(
    "observed",
    [
        object(),
        ResourceSnapshot(float("nan"), 34.0, 4_000_000_000),
        ResourceSnapshot(12.5, 101.0, 4_000_000_000),
        ResourceSnapshot(12.5, 34.0, -1),
        ResourceSnapshot(12.5, 34.0, 4_000_000_000, battery_percent=101.0),
        ResourceSnapshot(12.5, 34.0, 4_000_000_000, power_plugged=1),
    ],
)
def test_report_omits_invalid_resource_evidence(tmp_path, observed: object) -> None:
    store = _prepared_store(tmp_path)
    report = DailyActivityReportService(
        store,
        resource_observer=_StaticResourceObserver(observed),
    ).build_window(
        start=datetime(2026, 9, 12, tzinfo=UTC),
        end=datetime(2026, 9, 13, tzinfo=UTC),
    )

    assert report.resource_snapshot is None
    assert "Ресурси: поточний знімок не надано." in report.render_text()


def test_report_bounds_grouped_audit_categories_and_discloses_truncation(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    inside = "2026-09-12T10:00:00+00:00"
    with store.connection() as conn:
        conn.executemany(
            "INSERT INTO audit_events(event_type, entity_type, entity_id, payload_json, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            [
                (f"audit.category.{index:02d}", "test", f"entity-{index}", "{}", inside)
                for index in range(25)
            ],
        )

    report = DailyActivityReportService(store).build_window(
        start=datetime(2026, 9, 12, tzinfo=UTC),
        end=datetime(2026, 9, 13, tzinfo=UTC),
    )

    assert len(report.audit_events) == 20
    assert report.audit_events[0] == ActivityCount("audit.category.00", 1)
    assert report.audit_events[-1] == ActivityCount("audit.category.19", 1)
    assert "audit.category.20" not in report.render_text()
    assert any(
        "20 категорій" in limitation and "події аудиту" in limitation
        for limitation in report.limitations
    )


def test_report_contains_resource_observer_failure_without_leaking_details(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    report = DailyActivityReportService(
        store,
        resource_observer=_FailingResourceObserver(),
    ).build_window(
        start=datetime(2026, 9, 12, tzinfo=UTC),
        end=datetime(2026, 9, 13, tzinfo=UTC),
    )

    rendered = report.render_text()
    assert report.resource_snapshot is None
    assert "поточний знімок не надано" in rendered
    assert "provider-secret-must-not-escape" not in rendered


def test_report_detaches_accepted_resource_snapshot(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    observed = ResourceSnapshot(
        cpu_percent=12,
        memory_percent=34,
        available_memory_bytes=4_000_000_000,
        logical_cpu_count=8,
        total_memory_bytes=16_000_000_000,
        process_rss_bytes=256_000_000,
        battery_percent=77,
        power_plugged=False,
    )

    report = DailyActivityReportService(
        store,
        resource_observer=_StaticResourceObserver(observed),
    ).build_window(
        start=datetime(2026, 9, 12, tzinfo=UTC),
        end=datetime(2026, 9, 13, tzinfo=UTC),
    )

    assert report.resource_snapshot == ResourceSnapshot(
        cpu_percent=12.0,
        memory_percent=34.0,
        available_memory_bytes=4_000_000_000,
        logical_cpu_count=8,
        total_memory_bytes=16_000_000_000,
        process_rss_bytes=256_000_000,
        battery_percent=77.0,
        power_plugged=False,
    )
    assert report.resource_snapshot is not observed


def test_report_rejects_behavioral_datetime_before_timezone_hooks(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    service = DailyActivityReportService(store)
    start = _BehavioralDatetime(2026, 9, 12, tzinfo=UTC)

    with pytest.raises(TypeError, match="start must be a built-in datetime"):
        service.build_window(
            start=start,
            end=datetime(2026, 9, 13, tzinfo=UTC),
        )


def test_report_rejects_behavioral_date_before_daily_window_construction(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    service = DailyActivityReportService(store)

    with pytest.raises(TypeError, match="day must be a built-in date"):
        service.build_utc_day(_BehavioralDate(2026, 9, 12))


def test_report_rejects_non_text_grouped_durable_label(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    inside = "2026-09-12T10:00:00+00:00"
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO audit_events(event_type, entity_type, entity_id, payload_json, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (sqlite3.Binary(b"invalid-label"), "test", "entity-1", "{}", inside),
        )

    with pytest.raises(ValueError, match="grouped activity label must use SQLite TEXT"):
        DailyActivityReportService(store).build_window(
            start=datetime(2026, 9, 12, tzinfo=UTC),
            end=datetime(2026, 9, 13, tzinfo=UTC),
        )


def test_report_uses_one_read_snapshot_across_all_projections(tmp_path) -> None:
    base_store = _prepared_store(tmp_path)
    inside = "2026-09-12T10:00:00+00:00"
    with base_store.connection() as conn:
        journal_mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()
        assert journal_mode is not None
        assert str(journal_mode[0]).casefold() == "wal"

    store = _ConcurrentAuditMutationStore(base_store.path, created_at=inside)
    report = DailyActivityReportService(store).build_window(
        start=datetime(2026, 9, 12, tzinfo=UTC),
        end=datetime(2026, 9, 13, tzinfo=UTC),
    )

    assert store.writer_committed is True
    assert report.memory_update_events == 0
    assert ActivityCount("memory.upserted", 1) not in report.audit_events
    with base_store.connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM audit_events WHERE event_type = ?",
            ("memory.upserted",),
        ).fetchone()
    assert row is not None
    assert int(row["count"]) == 1


def test_report_counts_repeated_memory_upserts_from_durable_audit_history(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    memory = MemoryService(store, AuditLog(store))
    inside = "2026-09-12T10:00:00+00:00"

    memory.put(
        scope=MemoryScope.AGENT,
        owner_id="agent-1",
        namespace="learning",
        key="lesson-1",
        value={"secret": "FIRST_MEMORY_SECRET"},
    )
    memory.put(
        scope=MemoryScope.AGENT,
        owner_id="agent-1",
        namespace="learning",
        key="lesson-1",
        value={"secret": "SECOND_MEMORY_SECRET"},
    )
    with store.connection() as conn:
        current_rows = conn.execute("SELECT COUNT(*) AS count FROM memory_records").fetchone()
        conn.execute(
            "UPDATE audit_events SET created_at = ? WHERE event_type = ?",
            (inside, "memory.upserted"),
        )

    report = DailyActivityReportService(store).build_window(
        start=datetime(2026, 9, 12, tzinfo=UTC),
        end=datetime(2026, 9, 13, tzinfo=UTC),
    )

    assert current_rows is not None
    assert int(current_rows["count"]) == 1
    assert report.memory_update_events == 2
    assert ActivityCount("memory.upserted", 2) in report.audit_events
    rendered = report.render_text()
    assert "FIRST_MEMORY_SECRET" not in rendered
    assert "SECOND_MEMORY_SECRET" not in rendered


def test_report_retains_memory_update_after_record_is_deleted(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    memory = MemoryService(store, AuditLog(store))
    inside = "2026-09-12T10:00:00+00:00"

    memory.put(
        scope=MemoryScope.AGENT,
        owner_id="agent-1",
        namespace="learning",
        key="lesson-1",
        value={"secret": "DELETED_MEMORY_SECRET"},
    )
    assert memory.delete(
        scope=MemoryScope.AGENT,
        owner_id="agent-1",
        namespace="learning",
        key="lesson-1",
    )
    with store.connection() as conn:
        current_rows = conn.execute("SELECT COUNT(*) AS count FROM memory_records").fetchone()
        conn.execute(
            "UPDATE audit_events SET created_at = ? WHERE event_type IN (?, ?)",
            (inside, "memory.upserted", "memory.deleted"),
        )

    report = DailyActivityReportService(store).build_window(
        start=datetime(2026, 9, 12, tzinfo=UTC),
        end=datetime(2026, 9, 13, tzinfo=UTC),
    )

    assert current_rows is not None
    assert int(current_rows["count"]) == 0
    assert report.memory_update_events == 1
    assert ActivityCount("memory.upserted", 1) in report.audit_events
    assert ActivityCount("memory.deleted", 1) in report.audit_events
    assert "DELETED_MEMORY_SECRET" not in report.render_text()


def test_report_uses_half_open_window_and_never_synthesizes_missing_activity(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    service = DailyActivityReportService(store)

    report = service.build_utc_day(datetime(2026, 9, 12, tzinfo=UTC).date())

    assert report.task_transitions == ()
    assert report.audit_events == ()
    assert report.experiment_transitions == ()
    assert report.research_documents_added == 0
    assert report.memory_update_events == 0
    assert report.resource_snapshot is None
    assert "немає зафіксованих подій" in report.render_text()
    assert "не синтезується" in report.render_text()


def test_report_missing_database_fails_without_creating_storage(tmp_path) -> None:
    db_path = tmp_path / "missing" / "nika.sqlite3"
    service = DailyActivityReportService(SQLiteStore(db_path))

    with pytest.raises(FileNotFoundError, match="does not exist"):
        service.build_window(
            start=datetime(2026, 9, 12, tzinfo=UTC),
            end=datetime(2026, 9, 13, tzinfo=UTC),
        )

    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_report_enables_sqlite_query_only_before_projection(tmp_path) -> None:
    db_path = tmp_path / "nika.sqlite3"
    _prepared_store(tmp_path)
    tracking_store = _QueryOnlyTrackingStore(db_path)

    DailyActivityReportService(tracking_store).build_window(
        start=datetime(2026, 9, 12, tzinfo=UTC),
        end=datetime(2026, 9, 13, tzinfo=UTC),
    )

    assert tracking_store.observed_query_only == 1


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


@pytest.mark.parametrize("section", ["task", "audit", "experiment"])
def test_report_rejects_oversized_group_label_before_projection(
    tmp_path, section: str
) -> None:
    store = _prepared_store(tmp_path)
    inside = "2026-09-12T10:00:00+00:00"
    oversized = "x" * 4097

    with store.connection() as conn:
        if section == "task":
            conn.execute(
                "INSERT INTO tasks(task_id, workspace_id, agent_id, state, payload_json, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "task-oversized-label",
                    "workspace-1",
                    "agent-1",
                    "RUNNING",
                    "{}",
                    inside,
                    inside,
                ),
            )
            conn.execute(
                "INSERT INTO task_events(task_id, previous_state, new_state, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("task-oversized-label", "QUEUED", oversized, inside),
            )
        elif section == "audit":
            conn.execute(
                "INSERT INTO audit_events(event_type, entity_type, entity_id, payload_json, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (oversized, "test", "entity-oversized-label", "{}", inside),
            )
        else:
            conn.execute(
                "INSERT INTO experiments(experiment_id, definition_json, status, "
                "selected_candidate_id, previous_champion_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "experiment-oversized-label",
                    "{}",
                    "running",
                    None,
                    None,
                    inside,
                    inside,
                ),
            )
            conn.execute(
                "INSERT INTO experiment_events(experiment_id, previous_status, new_status, "
                "selected_candidate_id, previous_champion_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "experiment-oversized-label",
                    "queued",
                    oversized,
                    None,
                    None,
                    inside,
                ),
            )

    with pytest.raises(ValueError, match="exceeds safe UTF-8 storage bound"):
        DailyActivityReportService(store).build_window(
            start=datetime(2026, 9, 12, tzinfo=UTC),
            end=datetime(2026, 9, 13, tzinfo=UTC),
        )


def test_report_accepts_group_label_at_utf8_byte_bound(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    inside = "2026-09-12T10:00:00+00:00"
    boundary_label = "я" * 2048
    assert len(boundary_label.encode("utf-8")) == 4096

    with store.connection() as conn:
        conn.execute(
            "INSERT INTO audit_events(event_type, entity_type, entity_id, payload_json, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (boundary_label, "test", "entity-boundary-label", "{}", inside),
        )

    report = DailyActivityReportService(store).build_window(
        start=datetime(2026, 9, 12, tzinfo=UTC),
        end=datetime(2026, 9, 13, tzinfo=UTC),
    )

    assert report.audit_events == (ActivityCount(boundary_label, 1),)
    rendered = report.render_text()
    assert boundary_label not in rendered
    assert "..." in rendered

