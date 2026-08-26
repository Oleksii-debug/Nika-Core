from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from nika_core.business_factory import (
    BusinessFactory,
    BusinessObjective,
    BusinessPolicy,
    CommunicationAuthority,
    StaleBusinessStateError,
    dump_business_snapshot,
)
from nika_core.business_factory_persistence import BusinessFactoryRepository
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import EvidenceRef, ResearchEvidencePackage


def _factory(*, objective_id: str) -> BusinessFactory:
    return BusinessFactory.start(
        objective=BusinessObjective(
            objective_id=objective_id,
            goal="Exercise PF9 durable optimistic-concurrency boundaries",
            research_package=ResearchEvidencePackage(
                package_id=f"research-{objective_id}",
                evidence=(
                    EvidenceRef(
                        evidence_id=f"evidence-{objective_id}",
                        provenance_ref="research:source:controlled:concurrency",
                        claim="Controlled concurrency evidence",
                    ),
                ),
                research_artifact_ref=f"research:artifact:{objective_id}",
            ),
        ),
        policy=BusinessPolicy(
            policy_id=f"policy-{objective_id}",
            allowed_channel_ids=("sandbox-email",),
            communication_authority=CommunicationAuthority.DRAFT_ONLY,
        ),
    )


def test_business_factory_repository_survives_restart(tmp_path) -> None:
    path = tmp_path / "business factory unicode тест.sqlite"
    store = SQLiteStore(path)
    store.initialize()
    repository = BusinessFactoryRepository(store)
    repository.initialize()

    factory = _factory(objective_id="objective-restart")
    initial = factory.snapshot()
    repository.save(initial, expected_row_version=0)

    factory.identify_opportunity(
        opportunity_id="opportunity-restart",
        title="Durable opportunity",
        evidence_ids=("evidence-objective-restart",),
    )
    advanced = factory.snapshot()
    repository.save(advanced, expected_row_version=initial.row_version)

    restarted_store = SQLiteStore(path)
    restarted_store.initialize()
    restarted_repository = BusinessFactoryRepository(restarted_store)
    restarted_repository.initialize()
    restored = restarted_repository.load("objective-restart")
    assert restored is not None
    assert dump_business_snapshot(restored) == dump_business_snapshot(advanced)


def test_business_factory_first_writer_race_has_one_typed_stale_loser(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "business-factory-race.sqlite")
    store.initialize()
    first = BusinessFactoryRepository(store)
    second = BusinessFactoryRepository(store)
    first.initialize()
    second.initialize()
    snapshot = _factory(objective_id="objective-race").snapshot()
    barrier = Barrier(2)

    def write(repository: BusinessFactoryRepository) -> str:
        barrier.wait()
        try:
            repository.save(snapshot, expected_row_version=0)
        except StaleBusinessStateError:
            return "stale"
        return "saved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_result = executor.submit(write, first)
        second_result = executor.submit(write, second)
        outcomes = sorted((first_result.result(), second_result.result()))

    assert outcomes == ["saved", "stale"]
    restored = first.load(snapshot.objective.objective_id)
    assert restored == snapshot


def test_business_factory_optimistic_update_rejects_stale_writer(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "business-factory-update.sqlite")
    store.initialize()
    repository = BusinessFactoryRepository(store)
    repository.initialize()

    factory = _factory(objective_id="objective-update")
    base = factory.snapshot()
    repository.save(base, expected_row_version=0)

    factory.identify_opportunity(
        opportunity_id="opportunity-update",
        title="Durable opportunity",
        evidence_ids=("evidence-objective-update",),
    )
    winner = factory.snapshot()
    repository.save(winner, expected_row_version=base.row_version)

    stale = replace(
        base,
        audit=winner.audit,
        row_version=winner.row_version,
        opportunity=winner.opportunity,
    )
    with pytest.raises(StaleBusinessStateError, match="row version changed"):
        repository.save(stale, expected_row_version=base.row_version)


def test_business_repository_detects_storage_metadata_tamper(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "business-factory-tamper.sqlite")
    store.initialize()
    repository = BusinessFactoryRepository(store)
    repository.initialize()
    snapshot = _factory(objective_id="objective-tamper").snapshot()
    repository.save(snapshot, expected_row_version=0)

    with store.connection() as conn:
        conn.execute(
            "UPDATE business_factory_snapshots SET row_version = row_version + 1 "
            "WHERE objective_id = ?",
            ("objective-tamper",),
        )

    with pytest.raises(RuntimeError, match="row version does not match"):
        repository.load("objective-tamper")
