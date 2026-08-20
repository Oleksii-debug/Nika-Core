from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import httpx

from nika_core.data.schema import MIGRATIONS, SCHEMA_VERSION
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.research.blobs import ContentAddressedBlobStore
from nika_core.research.http import HttpxResearchFetcher
from nika_core.research.models import (
    ExtractedDocument,
    ResearchEvidence,
    ResearchResultItem,
    SourceKind,
    SourceSpec,
)
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.profile_jobs import ResearchProfileRunService
from nika_core.research.profiles import (
    ResearchProfile,
    ResearchProfileRepository,
    ResearchSourceRef,
    ResearchSourceSet,
)
from nika_core.research.query import (
    DeterministicResearchQueryService,
    ResearchQuerySpec,
    ResearchSearchFilters,
)
from nika_core.research.repository import ResearchRepository
from nika_core.research.scheduled_profiles import (
    ResearchDeltaKind,
    ScheduledResearchProfileService,
)
from nika_core.research.web_service import HttpResearchService
from nika_core.research.models import ResearchWorkspace
from nika_core.scheduler import ScheduledJob, TriggerKind

PUBLIC_IP = "93.184.216.34"


class _FakeScheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, ScheduledJob] = {}

    def upsert(self, job: ScheduledJob) -> None:
        self.jobs[job.job_id] = job

    def remove(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)

    def pause(self, job_id: str) -> None:
        del job_id

    def resume(self, job_id: str) -> None:
        del job_id

    def start(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


class _NoopWeb:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def refresh_source(self, source_id: str, *, task_id: str | None = None):
        self.calls.append((source_id, task_id))
        raise AssertionError("local-only scheduled profile must not fetch HTTP")


def _local_stack(tmp_path: Path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws", "Research"))
    source = SourceSpec("local-a", "ws", SourceKind.LOCAL_FILE, "C:/Corpus/grants.txt")
    repository.upsert_source(source)
    repository.ingest_document(
        source,
        ExtractedDocument(
            "Grant notice",
            "Український освітній грант для студентів.",
            "text/plain",
        ),
    )
    profiles = ResearchProfileRepository(store)
    profiles.save_source_set(
        ResearchSourceSet(
            "grant-sources",
            "ws",
            1,
            "Grant sources",
            (ResearchSourceRef("local-a", SourceKind.LOCAL_FILE),),
        )
    )
    profiles.save_profile(
        ResearchProfile(
            "grant-monitor",
            "ws",
            1,
            "Grant monitor",
            "grant-sources",
            1,
            "освітній грант",
        )
    )
    network = NetworkResearchRepository(store)
    tasks = TaskQueue(store)
    checkpoints = CheckpointService(store)
    query = DeterministicResearchQueryService(store=store, network_repository=network)
    web = _NoopWeb()
    runs = ResearchProfileRunService(
        tasks=tasks,
        checkpoints=checkpoints,
        profiles=profiles,
        network_repository=network,
        query_service=query,
        web=web,  # type: ignore[arg-type]
    )
    scheduler = _FakeScheduler()
    scheduled = ScheduledResearchProfileService(
        store=store,
        scheduler=scheduler,  # type: ignore[arg-type]
        profiles=profiles,
        runs=runs,
        network_repository=network,
    )
    return store, profiles, tasks, runs, scheduler, scheduled, web


def test_migration_13_upgrades_real_schema_12(tmp_path: Path) -> None:
    path = tmp_path / "v12.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for version in range(1, 13):
        for statement in MIGRATIONS[version]:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'fixture')",
            (version,),
        )
    conn.commit()
    conn.close()

    store = SQLiteStore(path)
    store.initialize()

    assert SCHEMA_VERSION == 13
    assert store.schema_version() == 13
    with store.connection() as check:
        names = {
            row["name"]
            for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {
        "research_profile_series_tasks",
        "research_profile_run_history",
        "research_profile_delta_items",
    } <= names


def test_schedule_reuses_scheduler_contract_and_latest_profile_policy(tmp_path: Path) -> None:
    _, _, _, _, scheduler, scheduled, _ = _local_stack(tmp_path)
    job = scheduled.upsert_schedule(
        schedule_id="grant-hourly",
        profile_id="grant-monitor",
        trigger_kind=TriggerKind.INTERVAL,
        trigger={"seconds": 3600},
    )

    assert scheduler.jobs["grant-hourly"] == job
    assert job.action_id == ScheduledResearchProfileService.ACTION_ID
    assert job.payload == {
        "series_id": "grant-hourly",
        "profile_id": "grant-monitor",
        "profile_version": None,
    }
    assert job.max_instances == 1
    assert job.coalesce is True


def test_first_recurring_run_is_new_and_unchanged_second_run_is_empty(tmp_path: Path) -> None:
    store, _, _, _, _, scheduled, web = _local_stack(tmp_path)
    payload: dict[str, Any] = {
        "series_id": "grant-hourly",
        "profile_id": "grant-monitor",
        "profile_version": None,
    }

    first = scheduled.run_scheduled(payload)
    assert first.run.state == "completed"
    assert first.delta is not None
    assert [item.kind for item in first.delta.items] == [ResearchDeltaKind.NEW]
    assert web.calls == []

    second = scheduled.run_scheduled(payload)
    assert second.run.state == "completed"
    assert second.run.task_id != first.run.task_id
    assert second.delta is not None
    assert second.delta.previous_result_set_id == first.run.result_set_id
    assert second.delta.items == ()
    assert "No new or changed matching results." in scheduled.render_delta_text(second.delta)

    restarted = SQLiteStore(store.path)
    restarted.initialize()
    with restarted.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_profile_run_history").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM research_profile_delta_items").fetchone()[0] == 1


def test_completed_unrecorded_run_is_reconciled_without_second_task(tmp_path: Path) -> None:
    store, _, tasks, runs, _, scheduled, _ = _local_stack(tmp_path)
    task_id = runs.create_job("grant-monitor")
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO research_profile_series_tasks(series_id, task_id, created_at) "
            "VALUES ('grant-hourly', ?, 'fixture')",
            (task_id,),
        )
    completed = runs.run(task_id)
    assert completed.state == "completed"
    assert completed.result_set_id is not None

    recovered = scheduled.run_scheduled(
        {
            "series_id": "grant-hourly",
            "profile_id": "grant-monitor",
            "profile_version": None,
        }
    )
    assert recovered.run.task_id == task_id
    assert recovered.delta is not None
    with store.connection() as conn:
        task_count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE agent_id=?",
            (ResearchProfileRunService.AGENT_ID,),
        ).fetchone()[0]
        history_count = conn.execute(
            "SELECT COUNT(*) FROM research_profile_run_history WHERE task_id=?",
            (task_id,),
        ).fetchone()[0]
    assert task_count == 1
    assert history_count == 1
    assert tasks.get(task_id).state.value == "COMPLETED"


def test_delta_changed_uses_stable_source_identity_not_redirect_locator() -> None:
    old = ResearchResultItem(
        ordinal=0,
        document_id="old-doc",
        title="Old",
        snippet="old",
        rank=0.0,
        why_matched="fixture",
        evidence=(
            ResearchEvidence(
                source_id="web-a",
                source_kind=SourceKind.HTTP,
                locator="https://old.example/final",
                observed_at="2026-01-01T00:00:00+00:00",
            ),
        ),
    )
    current = ResearchResultItem(
        ordinal=0,
        document_id="new-doc",
        title="New",
        snippet="new",
        rank=0.0,
        why_matched="fixture",
        evidence=(
            ResearchEvidence(
                source_id="web-a",
                source_kind=SourceKind.HTTP,
                locator="https://new.example/final",
                observed_at="2026-01-02T00:00:00+00:00",
            ),
        ),
    )

    delta = ScheduledResearchProfileService._classify_delta((current,), (old,))
    assert len(delta) == 1
    assert delta[0].kind is ResearchDeltaKind.CHANGED
    assert delta[0].previous_document_id == "old-doc"


def test_scoped_http_query_excludes_historical_snapshot_after_change(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        del request
        calls += 1
        body = (
            "Освітній грант стара редакція"
            if calls == 1
            else "Освітній грант нова редакція"
        )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            content=body.encode("utf-8"),
        )

    store = SQLiteStore(tmp_path / "http.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws", "Research"))
    network = NetworkResearchRepository(store)
    fetcher = HttpxResearchFetcher(
        resolver=lambda host, port: (PUBLIC_IP,),
        transport=httpx.MockTransport(handler),
    )
    web = HttpResearchService(
        repository=repository,
        network_repository=network,
        blob_store=ContentAddressedBlobStore(tmp_path / "blobs"),
        fetcher=fetcher,
        sleeper=lambda _: None,
    )
    web.register_source(SourceSpec("web-a", "ws", SourceKind.HTTP, "https://example.com/a"))
    first = web.refresh_source("web-a")
    second = web.refresh_source("web-a")
    assert first.document_id is not None
    assert second.document_id is not None
    assert second.document_id != first.document_id

    query = DeterministicResearchQueryService(store=store, network_repository=network)
    execution = query.execute(
        ResearchQuerySpec(
            workspace_id="ws",
            text="освітній грант",
            filters=ResearchSearchFilters(source_ids=("web-a",)),
        )
    )
    assert [item.document_id for item in execution.result_set.items] == [second.document_id]
