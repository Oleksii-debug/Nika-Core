from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    EvidenceRef,
    ProductOption,
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
    ResearchEvidencePackage,
    StaleProjectVersionError,
)


def _spec(goal: str = "Build accessible expense app") -> ProductProjectSpec:
    return ProductProjectSpec(
        goal=goal,
        desired_outcome="A tested durable product",
        requirements=(
            ProductRequirement("req-1", "Keyboard operation", ("All primary actions keyboard reachable",)),
        ),
        credential_refs=("credential://github/project-writer",),
        risk={"accessibility": "high"},
    )


def test_create_is_idempotent_and_survives_restart(tmp_path) -> None:
    db = tmp_path / "nika.db"
    store = SQLiteStore(db)
    store.initialize()
    repo = ProductProjectRepository(store)
    first = repo.create(project_id="p1", name="Expense", spec=_spec(), idempotency_key="create:p1")
    again = repo.create(project_id="p1", name="Expense", spec=_spec(), idempotency_key="create:p1")
    assert again == first

    restarted = SQLiteStore(db)
    restarted.initialize()
    recovered = ProductProjectRepository(restarted).get("p1")
    assert recovered.spec.goal == first.spec.goal
    assert recovered.spec_version == 1


def test_spec_versions_are_immutable_and_stale_write_fails(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repo = ProductProjectRepository(store)
    created = repo.create(project_id="p1", name="Expense", spec=_spec(), idempotency_key="create:p1")
    updated = repo.update_spec("p1", _spec("Build accessible expense app v2"), expected_row_version=created.row_version)
    assert updated.spec_version == 2
    assert updated.row_version == 1
    with pytest.raises(StaleProjectVersionError):
        repo.update_spec("p1", _spec("stale"), expected_row_version=0)
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM product_project_specs WHERE project_id='p1'").fetchone()[0] == 2


def test_research_handoff_requires_provenance_and_links_options(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repo = ProductProjectRepository(store)
    repo.create(project_id="p1", name="Expense", spec=_spec(), idempotency_key="create:p1")
    package = ResearchEvidencePackage(
        "research-1",
        (EvidenceRef("ev-1", "research://workspace/report/claim/1", "Competitor gap"),),
        "artifact://research/report-1",
    )
    repo.record_research_handoff(
        "p1",
        package,
        (ProductOption("option-1", "Local-first", "Private by default", ("research-1",)),),
    )
    with store.connection() as conn:
        payload = conn.execute("SELECT payload_json FROM product_research_handoffs WHERE project_id='p1'").fetchone()[0]
    assert "research://workspace/report/claim/1" in payload

    with pytest.raises(ProductProjectError):
        ResearchEvidencePackage("bad", (EvidenceRef("ev", ""),))


def test_raw_credential_fields_are_rejected() -> None:
    with pytest.raises(ProductProjectError, match="credential"):
        ProductProjectSpec(
            goal="x",
            desired_outcome="y",
            compliance={"api_key": "plaintext-secret"},
        )


def test_idempotency_key_cannot_be_reused_for_different_input(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repo = ProductProjectRepository(store)
    repo.create(project_id="p1", name="Expense", spec=_spec(), idempotency_key="same")
    with pytest.raises(ProductProjectError, match="different input"):
        repo.create(project_id="p2", name="Other", spec=_spec("other"), idempotency_key="same")
