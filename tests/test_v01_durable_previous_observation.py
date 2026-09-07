from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.research.models import ExtractedDocument, ResearchWorkspace, SourceKind, SourceSpec
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
