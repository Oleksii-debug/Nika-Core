from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.business_factory import (
    BusinessFactory,
    BusinessFactorySnapshot,
    BusinessObjective,
    BusinessPolicy,
    CommunicationAuthority,
)
from nika_core.business_factory_persistence import BusinessFactoryRepository
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import EvidenceRef, ResearchEvidencePackage


def _snapshot(objective_id: str) -> BusinessFactorySnapshot:
    return BusinessFactory.start(
        objective=BusinessObjective(
            objective_id=objective_id,
            goal="Qualify PF9 durable persistence authority",
            research_package=ResearchEvidencePackage(
                package_id=f"research-{objective_id}",
                evidence=(
                    EvidenceRef(
                        evidence_id=f"evidence-{objective_id}",
                        provenance_ref="research:source:qa:pf9-550",
                        claim="Independent PF9 persistence integrity evidence",
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


def _repository(path: Path) -> tuple[SQLiteStore, BusinessFactoryRepository]:
    store = SQLiteStore(path)
    store.initialize()
    repository = BusinessFactoryRepository(store)
    repository.initialize()
    return store, repository


def test_load_rejects_real_row_version_even_when_int_coercion_matches_payload(tmp_path) -> None:
    store, repository = _repository(tmp_path / "pf9 real row version.sqlite")
    snapshot = _snapshot("objective-real-row-version")
    repository.save(snapshot, expected_row_version=0)

    with store.connection() as conn:
        conn.execute(
            "UPDATE business_factory_snapshots SET row_version = row_version + 0.5 "
            "WHERE objective_id = ?",
            (snapshot.objective.objective_id,),
        )
        row = conn.execute(
            "SELECT row_version, typeof(row_version) AS storage_type "
            "FROM business_factory_snapshots WHERE objective_id = ?",
            (snapshot.objective.objective_id,),
        ).fetchone()

    assert row is not None
    assert row["storage_type"] == "real"
    assert int(row["row_version"]) == snapshot.row_version
    with pytest.raises(RuntimeError):
        repository.load(snapshot.objective.objective_id)


def test_initialize_rejects_non_integer_pf9_migration_marker(tmp_path) -> None:
    store, _repository_before_corruption = _repository(
        tmp_path / "pf9 malformed migration marker.sqlite"
    )

    with store.connection() as conn:
        conn.execute("DROP TABLE business_factory_schema_migrations")
        conn.execute(
            "CREATE TABLE business_factory_schema_migrations ("
            "version REAL PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO business_factory_schema_migrations(version, applied_at) "
            "VALUES (?, ?)",
            (1.5, "2026-08-26T20:00:00+00:00"),
        )
        marker = conn.execute(
            "SELECT version, typeof(version) AS storage_type "
            "FROM business_factory_schema_migrations"
        ).fetchone()

    assert marker is not None
    assert marker["storage_type"] == "real"
    with pytest.raises(RuntimeError):
        BusinessFactoryRepository(store).initialize()


def test_initialize_rejects_current_marker_with_missing_owned_table(tmp_path) -> None:
    store, _repository_before_corruption = _repository(
        tmp_path / "pf9 missing owned table.sqlite"
    )

    with store.connection() as conn:
        marker = conn.execute(
            "SELECT version FROM business_factory_schema_migrations"
        ).fetchone()
        assert marker is not None
        assert marker["version"] == 1
        conn.execute("DROP TABLE business_factory_snapshots")

    with pytest.raises(RuntimeError):
        BusinessFactoryRepository(store).initialize()


def test_initialize_rejects_current_marker_with_malformed_owned_table(tmp_path) -> None:
    store, _repository_before_corruption = _repository(
        tmp_path / "pf9 malformed owned table.sqlite"
    )

    with store.connection() as conn:
        conn.execute("DROP TABLE business_factory_snapshots")
        conn.execute(
            "CREATE TABLE business_factory_snapshots ("
            "objective_id TEXT PRIMARY KEY, "
            "row_version TEXT NOT NULL, "
            "payload_json TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        declared_type = conn.execute(
            "SELECT type FROM pragma_table_info('business_factory_snapshots') "
            "WHERE name = 'row_version'"
        ).fetchone()

    assert declared_type is not None
    assert declared_type["type"] == "TEXT"
    with pytest.raises(RuntimeError):
        BusinessFactoryRepository(store).initialize()
