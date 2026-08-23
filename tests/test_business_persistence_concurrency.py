from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from nika_core.business_communication import (
    BusinessCommunicationCoordinator,
    BusinessCommunicationRepository,
    StaleCommunicationStateError,
)
from nika_core.business_factory import (
    BusinessFactory,
    BusinessObjective,
    BusinessPolicy,
    CommunicationAuthority,
    StaleBusinessStateError,
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
                        source_ref="research:source:controlled:concurrency",
                        summary="Controlled concurrency evidence",
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


def test_business_factory_first_writer_race_has_one_typed_stale_loser(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "business-factory-race.sqlite")
    store.initialize()
    first = BusinessFactoryRepository(store)
    second = BusinessFactoryRepository(store)
    first.initialize()
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
        outcomes = sorted(
            (
                executor.submit(write, first).result(),
                executor.submit(write, second).result(),
            )
        )

    assert outcomes == ["saved", "stale"]
    restored = first.load(snapshot.objective.objective_id)
    assert restored == snapshot


def test_business_communication_first_writer_race_has_one_typed_stale_loser(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "business-communication-race.sqlite")
    store.initialize()
    first = BusinessCommunicationRepository(store)
    second = BusinessCommunicationRepository(store)
    first.initialize()

    factory = _factory(objective_id="objective-communication-race")
    evidence_id = factory.snapshot().objective.research_package.evidence[0].evidence_id
    factory.identify_opportunity(
        opportunity_id="opportunity-race",
        title="Controlled concurrency opportunity",
        evidence_ids=(evidence_id,),
    )
    factory.create_lead(
        lead_id="lead-race",
        channel_id="sandbox-email",
        counterparty_ref="counterparty:controlled:race",
    )
    record = BusinessCommunicationCoordinator.draft(
        factory.snapshot(),
        message_id="message-race",
        thread_ref="thread:controlled:race",
        payload_ref="payload:controlled:race",
    )
    barrier = Barrier(2)

    def write(repository: BusinessCommunicationRepository) -> str:
        barrier.wait()
        try:
            repository.save(record, expected_row_version=0)
        except StaleCommunicationStateError:
            return "stale"
        return "saved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(
            (
                executor.submit(write, first).result(),
                executor.submit(write, second).result(),
            )
        )

    assert outcomes == ["saved", "stale"]
    restored = first.load(record.message_id)
    assert restored == record
