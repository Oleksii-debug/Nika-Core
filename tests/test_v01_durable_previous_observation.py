from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.research.models import (
    ExtractedDocument,
    FreshnessState,
    RefreshDisposition,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.previous_observation import (
    DurablePreviousObservationLoader,
    PreviousObservationError,
    PreviousObservationErrorCode,
    PreviousObservationExpectation,
)
from nika_core.research.profile_jobs import ResearchProfileRunService
from nika_core.research.profiles import (
    ResearchProfile,
    ResearchProfileRepository,
    ResearchSourceRef,
    ResearchSourceSet,
)
from nika_core.research.query import DeterministicResearchQueryService
from nika_core.research.repository import ResearchRepository
from nika_core.research.scheduled_profiles import ScheduledResearchProfileService
from nika_core.scheduler import ScheduledJob


class _FakeScheduler:
    def upsert(self, job: ScheduledJob) -> None:
        del job

    def remove(self, job_id: str) -> None:
        del job_id

    def pause(self, job_id: str) -> None:
        del job_id

    def resume(self, job_id: str) -> None:
        del job_id

    def start(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


class _NoopWeb:
    def refresh_source(self, source_id: str, *, task_id: str | None = None):
        del source_id, task_id
        raise AssertionError("local-only durable baseline fixture must not fetch HTTP")


def _stack(path: Path):
    store = SQLiteStore(path)
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws", "Research"))
    source = SourceSpec("local-a", "ws", SourceKind.LOCAL_FILE, "C:/Corpus/grants.txt")
    repository.upsert_source(source)
    repository.ingest_document(
        source,
        ExtractedDocument("Grant", "освітній грант baseline", "text/plain"),
    )
    profiles = ResearchProfileRepository(store)
    profiles.save_source_set(
        ResearchSourceSet(
            "sources",
            "ws",
            1,
            "Sources",
            (ResearchSourceRef("local-a", SourceKind.LOCAL_FILE),),
        )
    )
    profiles.save_profile(
        ResearchProfile(
            "monitor",
            "ws",
            1,
            "Monitor",
            "sources",
            1,
            "освітній грант",
        )
    )
    network = NetworkResearchRepository(store)
    runs = ResearchProfileRunService(
        tasks=TaskQueue(store),
        checkpoints=CheckpointService(store),
        profiles=profiles,
        network_repository=network,
        query_service=DeterministicResearchQueryService(
            store=store,
            network_repository=network,
        ),
        web=_NoopWeb(),  # type: ignore[arg-type]
    )
    scheduled = ScheduledResearchProfileService(
        store=store,
        scheduler=_FakeScheduler(),  # type: ignore[arg-type]
        profiles=profiles,
        runs=runs,
        network_repository=network,
    )
    return store, repository, profiles, network, scheduled


def _expected(*, profile_version: int = 1, source_set_version: int = 1):
    return PreviousObservationExpectation(
        series_id="series",
        workspace_id="ws",
        profile_id="monitor",
        profile_version=profile_version,
        source_set_id="sources",
        source_set_version=source_set_version,
    )


def _run_once(scheduled: ScheduledResearchProfileService):
    result = scheduled.run_scheduled(
        {
            "series_id": "series",
            "profile_id": "monitor",
            "profile_version": 1,
        }
    )
    assert result.run.result_set_id is not None
    return result


def _loader(store: SQLiteStore) -> DurablePreviousObservationLoader:
    return DurablePreviousObservationLoader(
        store=store,
        profiles=ResearchProfileRepository(store),
        network_repository=NetworkResearchRepository(store),
    )


@pytest.mark.parametrize("source_changes", [False, True])
def test_restart_loads_exact_persisted_previous_observation(
    tmp_path: Path,
    source_changes: bool,
) -> None:
    path = tmp_path / "nika.db"
    store, repository, _, network, scheduled = _stack(path)
    first = _run_once(scheduled)
    assert first.run.result_set_id is not None
    exact_before_restart = network.get_result_set(first.run.result_set_id)

    if source_changes:
        source = SourceSpec("local-a", "ws", SourceKind.LOCAL_FILE, "C:/Corpus/grants.txt")
        repository.ingest_document(
            source,
            ExtractedDocument("Grant update", "освітній грант changed", "text/plain"),
        )

    del scheduled, network, repository, store
    restarted = SQLiteStore(path)
    restarted.initialize()
    loaded = _loader(restarted).load(_expected())

    assert loaded.result_set == exact_before_restart
    assert loaded.result_set.result_set_id == first.run.result_set_id
    assert loaded.task_id == first.run.task_id


def test_missing_baseline_fails_closed(tmp_path: Path) -> None:
    store, _, _, _, _ = _stack(tmp_path / "nika.db")

    with pytest.raises(PreviousObservationError) as caught:
        _loader(store).load(_expected())

    assert caught.value.code is PreviousObservationErrorCode.MISSING_BASELINE


def test_corrupt_baseline_fails_closed_instead_of_manufacturing_change(tmp_path: Path) -> None:
    store, _, _, _, scheduled = _stack(tmp_path / "nika.db")
    first = _run_once(scheduled)
    assert first.run.result_set_id is not None
    with store.connection() as conn:
        row = conn.execute(
            "SELECT evidence_json FROM research_result_items WHERE result_set_id=? LIMIT 1",
            (first.run.result_set_id,),
        ).fetchone()
        assert row is not None
        conn.execute(
            "UPDATE research_result_items SET evidence_json='{' WHERE result_set_id=?",
            (first.run.result_set_id,),
        )

    with pytest.raises(PreviousObservationError) as caught:
        _loader(store).load(_expected())

    assert caught.value.code is PreviousObservationErrorCode.CORRUPT_BASELINE


def test_wrong_source_identity_is_rejected(tmp_path: Path) -> None:
    store, repository, _, _, scheduled = _stack(tmp_path / "nika.db")
    repository.upsert_source(
        SourceSpec("local-b", "ws", SourceKind.LOCAL_FILE, "C:/Corpus/other.txt")
    )
    first = _run_once(scheduled)
    assert first.run.result_set_id is not None
    with store.connection() as conn:
        row = conn.execute(
            "SELECT ordinal, evidence_json FROM research_result_items "
            "WHERE result_set_id=? ORDER BY ordinal LIMIT 1",
            (first.run.result_set_id,),
        ).fetchone()
        assert row is not None
        evidence = json.loads(row["evidence_json"])
        evidence[0]["source_id"] = "local-b"
        conn.execute(
            "UPDATE research_result_items SET evidence_json=? "
            "WHERE result_set_id=? AND ordinal=?",
            (json.dumps(evidence), first.run.result_set_id, row["ordinal"]),
        )

    with pytest.raises(PreviousObservationError) as caught:
        _loader(store).load(_expected())

    assert caught.value.code is PreviousObservationErrorCode.IDENTITY_MISMATCH


def test_stale_profile_or_source_set_version_is_rejected(tmp_path: Path) -> None:
    store, _, profiles, _, scheduled = _stack(tmp_path / "nika.db")
    _run_once(scheduled)
    profiles.save_source_set(
        ResearchSourceSet(
            "sources",
            "ws",
            2,
            "Sources v2",
            (ResearchSourceRef("local-a", SourceKind.LOCAL_FILE),),
        )
    )
    profiles.save_profile(
        ResearchProfile(
            "monitor",
            "ws",
            2,
            "Monitor v2",
            "sources",
            2,
            "освітній грант",
        )
    )

    with pytest.raises(PreviousObservationError) as caught:
        _loader(store).load(_expected(profile_version=2, source_set_version=2))

    assert caught.value.code is PreviousObservationErrorCode.STALE_VERSION


def test_duplicate_latest_history_timestamp_is_rejected_as_ambiguous(tmp_path: Path) -> None:
    store, _, _, _, scheduled = _stack(tmp_path / "nika.db")
    first = _run_once(scheduled)
    second = _run_once(scheduled)
    with store.connection() as conn:
        second_created_at = conn.execute(
            "SELECT created_at FROM research_profile_run_history WHERE task_id=?",
            (second.run.task_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE research_profile_run_history SET created_at=? WHERE task_id=?",
            (second_created_at, first.run.task_id),
        )

    with pytest.raises(PreviousObservationError) as caught:
        _loader(store).load(_expected())

    assert caught.value.code is PreviousObservationErrorCode.DUPLICATE_BASELINE


def test_cross_workspace_result_set_substitution_is_rejected(tmp_path: Path) -> None:
    store, repository, _, _, scheduled = _stack(tmp_path / "nika.db")
    first = _run_once(scheduled)
    assert first.run.result_set_id is not None
    repository.upsert_workspace(ResearchWorkspace("other-ws", "Other"))
    with store.connection() as conn:
        conn.execute(
            "UPDATE research_result_sets SET workspace_id='other-ws' WHERE result_set_id=?",
            (first.run.result_set_id,),
        )

    with pytest.raises(PreviousObservationError) as caught:
        _loader(store).load(_expected())

    assert caught.value.code is PreviousObservationErrorCode.IDENTITY_MISMATCH


def test_same_local_source_id_cannot_retarget_baseline_locator(tmp_path: Path) -> None:
    store, repository, _, _, scheduled = _stack(tmp_path / "nika.db")
    _run_once(scheduled)
    repository.upsert_source(
        SourceSpec("local-a", "ws", SourceKind.LOCAL_FILE, "C:/Corpus/retargeted.txt")
    )

    with pytest.raises(PreviousObservationError) as caught:
        _loader(store).load(_expected())

    assert caught.value.code is PreviousObservationErrorCode.IDENTITY_MISMATCH


def test_persisted_local_evidence_locator_substitution_fails_closed(tmp_path: Path) -> None:
    store, _, _, _, scheduled = _stack(tmp_path / "nika.db")
    first = _run_once(scheduled)
    assert first.run.result_set_id is not None
    with store.connection() as conn:
        row = conn.execute(
            "SELECT ordinal, evidence_json FROM research_result_items "
            "WHERE result_set_id=? ORDER BY ordinal LIMIT 1",
            (first.run.result_set_id,),
        ).fetchone()
        assert row is not None
        evidence = json.loads(row["evidence_json"])
        evidence[0]["locator"] = "C:/Corpus/ATTACKER.txt"
        conn.execute(
            "UPDATE research_result_items SET evidence_json=? "
            "WHERE result_set_id=? AND ordinal=?",
            (json.dumps(evidence), first.run.result_set_id, row["ordinal"]),
        )

    with pytest.raises(PreviousObservationError) as caught:
        _loader(store).load(_expected())

    assert caught.value.code is PreviousObservationErrorCode.IDENTITY_MISMATCH


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("agent_id", "foreign.agent"),
        ("workspace_id", "other-ws"),
        ("state", TaskState.RUNNING.value),
    ],
)
def test_previous_observation_requires_canonical_completed_research_task(
    tmp_path: Path,
    column: str,
    replacement: str,
) -> None:
    store, _, _, _, scheduled = _stack(tmp_path / f"{column}.db")
    first = _run_once(scheduled)
    with store.connection() as conn:
        conn.execute(
            f"UPDATE tasks SET {column}=? WHERE task_id=?",
            (replacement, first.run.task_id),
        )

    with pytest.raises(PreviousObservationError) as caught:
        _loader(store).load(_expected())

    assert caught.value.code is PreviousObservationErrorCode.IDENTITY_MISMATCH


def test_previous_observation_task_payload_must_pin_exact_versions(tmp_path: Path) -> None:
    store, _, _, _, scheduled = _stack(tmp_path / "payload.db")
    first = _run_once(scheduled)
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id=?",
            (first.run.task_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        payload["profile_version"] = 99
        conn.execute(
            "UPDATE tasks SET payload_json=? WHERE task_id=?",
            (json.dumps(payload), first.run.task_id),
        )

    with pytest.raises(PreviousObservationError) as caught:
        _loader(store).load(_expected())

    assert caught.value.code is PreviousObservationErrorCode.IDENTITY_MISMATCH


def _http_baseline(path: Path):
    store = SQLiteStore(path)
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws", "Research"))
    network = NetworkResearchRepository(store)
    declared_url = "https://declared-a.test/source"
    final_url = "https://cdn-a.test/final"
    network.register_source(SourceSpec("web-a", "ws", SourceKind.HTTP, declared_url))

    profiles = ResearchProfileRepository(store)
    profiles.save_source_set(
        ResearchSourceSet(
            "sources",
            "ws",
            1,
            "Sources",
            (ResearchSourceRef("web-a", SourceKind.HTTP),),
        )
    )
    profiles.save_profile(
        ResearchProfile(
            "monitor",
            "ws",
            1,
            "Monitor",
            "sources",
            1,
            "grant",
        )
    )

    tasks = TaskQueue(store)
    task = tasks.create(
        workspace_id="ws",
        agent_id=ResearchProfileRunService.AGENT_ID,
        payload={
            "profile_id": "monitor",
            "profile_version": 1,
            "source_set_id": "sources",
            "source_set_version": 1,
            "http_source_ids": ["web-a"],
        },
    )
    tasks.transition(task.task_id, TaskState.READY)
    tasks.transition(task.task_id, TaskState.RUNNING)

    network.record_attempt(
        source_id="web-a",
        attempt_number=1,
        disposition=RefreshDisposition.CHANGED,
        requested_url=declared_url,
        final_url=final_url,
        status_code=200,
        error_code=None,
        error_message="",
        retryable=False,
        task_id=task.task_id,
    )
    created_at = datetime.now(UTC).isoformat()
    result_set_id = "http-result"
    evidence = [
        {
            "source_id": "web-a",
            "source_kind": SourceKind.HTTP.value,
            "locator": final_url,
            "observed_at": created_at,
            "freshness": FreshnessState.CURRENT.value,
        }
    ]
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO research_result_sets(result_set_id, workspace_id, query, created_at) "
            "VALUES (?, ?, ?, ?)",
            (result_set_id, "ws", "grant", created_at),
        )
        conn.execute(
            """INSERT INTO research_result_items(
                result_set_id, ordinal, document_id, title, snippet, rank,
                why_matched, evidence_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result_set_id,
                0,
                "doc-http",
                "HTTP result",
                "grant",
                1.0,
                "literal",
                json.dumps(evidence),
            ),
        )
        conn.execute(
            "INSERT INTO research_profile_series_tasks(series_id, task_id, created_at) "
            "VALUES (?, ?, ?)",
            ("series", task.task_id, created_at),
        )
        conn.execute(
            """INSERT INTO research_profile_run_history(
                task_id, series_id, profile_id, profile_version, source_set_id,
                source_set_version, result_set_id, previous_result_set_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
            (
                task.task_id,
                "series",
                "monitor",
                1,
                "sources",
                1,
                result_set_id,
                created_at,
            ),
        )
    tasks.transition(task.task_id, TaskState.COMPLETED)
    return store, network, declared_url, final_url


def test_http_baseline_accepts_historical_redirect_provenance(tmp_path: Path) -> None:
    store, _, _, _ = _http_baseline(tmp_path / "http-ok.db")

    loaded = _loader(store).load(_expected())

    assert loaded.result_set.result_set_id == "http-result"
    assert loaded.result_set.items[0].evidence[0].locator == "https://cdn-a.test/final"


def test_same_http_source_id_retarget_requires_rebaseline(tmp_path: Path) -> None:
    store, network, _, _ = _http_baseline(tmp_path / "http-retarget.db")
    network.register_source(
        SourceSpec(
            "web-a",
            "ws",
            SourceKind.HTTP,
            "https://declared-b.test/source",
        )
    )

    with pytest.raises(PreviousObservationError) as caught:
        _loader(store).load(_expected())

    assert caught.value.code is PreviousObservationErrorCode.IDENTITY_MISMATCH
