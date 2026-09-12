from __future__ import annotations

import pytest

from nika_core.business_factory import (
    BusinessFactory,
    BusinessObjective,
    BusinessPolicy,
    CommunicationAuthority,
)
from nika_core.business_factory_persistence import BusinessFactoryRepository
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import EvidenceRef, ResearchEvidencePackage


def _snapshot(objective_id: str):
    return BusinessFactory.start(
        objective=BusinessObjective(
            objective_id=objective_id,
            goal="Exercise PF9 fail-closed persistence integrity",
            research_package=ResearchEvidencePackage(
                package_id=f"research-{objective_id}",
                evidence=(
                    EvidenceRef(
                        evidence_id=f"evidence-{objective_id}",
                        provenance_ref="research:source:controlled:pf9-integrity",
                        claim="Controlled PF9 persistence integrity evidence",
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
    ).snapshot()


def _repository(tmp_path, name: str):
    store = SQLiteStore(tmp_path / name)
    store.initialize()
    repository = BusinessFactoryRepository(store)
    repository.initialize()
    return store, repository


def test_load_rejects_fractional_storage_row_version(tmp_path) -> None:
    store, repository = _repository(tmp_path, "fractional-row.sqlite")
    snapshot = _snapshot("objective-fractional")
    repository.save(snapshot, expected_row_version=0)

    with store.connection() as conn:
        conn.execute(
            "UPDATE business_factory_snapshots SET row_version = row_version + 0.5 "
            "WHERE objective_id = ?",
            (snapshot.objective.objective_id,),
        )

    with pytest.raises(TypeError, match="SQLite INTEGER"):
        repository.load(snapshot.objective.objective_id)


def test_initialize_rejects_fractional_migration_marker(tmp_path) -> None:
    store, repository = _repository(tmp_path, "fractional-migration.sqlite")
    with store.connection() as conn:
        conn.execute("DROP TABLE business_factory_schema_migrations")
        conn.execute(
            "CREATE TABLE business_factory_schema_migrations ("
            "version REAL PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO business_factory_schema_migrations(version, applied_at) "
            "VALUES (1.5, 'tampered')"
        )

    with pytest.raises(RuntimeError, match="schema mismatch"):
        repository.initialize()


def test_initialize_rejects_missing_owned_snapshot_table(tmp_path) -> None:
    store, repository = _repository(tmp_path, "missing-owned-table.sqlite")
    with store.connection() as conn:
        conn.execute("DROP TABLE business_factory_snapshots")

    with pytest.raises(RuntimeError, match="schema mismatch"):
        repository.initialize()


def test_initialize_rejects_malformed_owned_snapshot_table(tmp_path) -> None:
    store, repository = _repository(tmp_path, "malformed-owned-table.sqlite")
    with store.connection() as conn:
        conn.execute("DROP TABLE business_factory_snapshots")
        conn.execute(
            "CREATE TABLE business_factory_snapshots ("
            "objective_id TEXT PRIMARY KEY, "
            "row_version TEXT NOT NULL, "
            "payload_json TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )

    with pytest.raises(RuntimeError, match="schema mismatch"):
        repository.initialize()
