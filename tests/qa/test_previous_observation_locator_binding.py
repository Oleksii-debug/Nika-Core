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
        raise AssertionError("local-only locator-binding fixture must not fetch HTTP")


def _stack(path: Path):
    store = SQLiteStore(path)
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws", "Research"))
    source = SourceSpec("local-a", "ws", SourceKind.LOCAL_FILE, "C:/Corpus/A.txt")
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
    result = scheduled.run_scheduled(
        {
            "series_id": "series",
            "profile_id": "monitor",
            "profile_version": 1,
        }
    )
    assert result.run.result_set_id is not None
    return store, repository, profiles, network, result.run.result_set_id


def _expected() -> PreviousObservationExpectation:
    return PreviousObservationExpectation(
        series_id="series",
        workspace_id="ws",
        profile_id="monitor",
        profile_version=1,
        source_set_id="sources",
        source_set_version=1,
    )


def _loader(
    store: SQLiteStore,
    profiles: ResearchProfileRepository,
    network: NetworkResearchRepository,
) -> DurablePreviousObservationLoader:
    return DurablePreviousObservationLoader(
        store=store,
        profiles=profiles,
        network_repository=network,
    )


def test_same_source_id_reconfigured_to_new_local_locator_invalidates_old_baseline(
    tmp_path: Path,
) -> None:
    store, repository, profiles, network, _result_set_id = _stack(tmp_path / "nika.db")

    repository.upsert_source(
        SourceSpec("local-a", "ws", SourceKind.LOCAL_FILE, "C:/Corpus/B.txt")
    )

    with pytest.raises(PreviousObservationError) as caught:
        _loader(store, profiles, network).load(_expected())

    assert caught.value.code in {
        PreviousObservationErrorCode.IDENTITY_MISMATCH,
        PreviousObservationErrorCode.STALE_VERSION,
    }


def test_persisted_evidence_locator_substitution_fails_closed(
    tmp_path: Path,
) -> None:
    store, _repository, profiles, network, result_set_id = _stack(tmp_path / "nika.db")

    with store.connection() as conn:
        row = conn.execute(
            "SELECT ordinal, evidence_json FROM research_result_items "
            "WHERE result_set_id=? ORDER BY ordinal LIMIT 1",
            (result_set_id,),
        ).fetchone()
        assert row is not None
        evidence = json.loads(row["evidence_json"])
        assert evidence[0]["source_id"] == "local-a"
        assert evidence[0]["locator"] == "C:/Corpus/A.txt"
        evidence[0]["locator"] = "C:/Corpus/ATTACKER.txt"
        conn.execute(
            "UPDATE research_result_items SET evidence_json=? "
            "WHERE result_set_id=? AND ordinal=?",
            (
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                result_set_id,
                row["ordinal"],
            ),
        )

    with pytest.raises(PreviousObservationError) as caught:
        _loader(store, profiles, network).load(_expected())

    assert caught.value.code in {
        PreviousObservationErrorCode.IDENTITY_MISMATCH,
        PreviousObservationErrorCode.CORRUPT_BASELINE,
    }
