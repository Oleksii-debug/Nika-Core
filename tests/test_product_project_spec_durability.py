from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    StaleProjectVersionError,
)
from nika_core.product_project_spec_durability import ProductProjectSpecDurabilityService
from nika_core.product_project_schema import PRODUCT_PROJECT_SCHEMA_VERSION


def _spec(goal: str) -> ProductProjectSpec:
    return ProductProjectSpec(
        goal=goal,
        desired_outcome="A restart-safe accepted product",
    )


def _store(tmp_path) -> tuple[SQLiteStore, ProductProjectRepository]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id="p1",
        name="Durable Product",
        spec=_spec("Initial durable scope"),
        idempotency_key="create:p1",
    )
    return store, projects


def test_spec_mutation_is_exactly_once_across_retry_and_restart(tmp_path) -> None:
    store, projects = _store(tmp_path)
    service = ProductProjectSpecDurabilityService(store)
    revised = _spec("Approved durable scope v2")

    first = service.update_spec(
        "p1",
        revised,
        expected_row_version=0,
        idempotency_key="spec:p1:approved-v2",
        change_reason="Owner approved scope v2",
    )
    replay = service.update_spec(
        "p1",
        revised,
        expected_row_version=0,
        idempotency_key="spec:p1:approved-v2",
        change_reason="Owner approved scope v2",
    )

    assert replay == first
    assert first.previous_spec_version == 1
    assert first.spec_version == 2
    assert first.previous_row_version == 0
    assert first.row_version == 1
    assert projects.get("p1").spec_version == 2
    assert projects.get("p1").row_version == 1
    assert len(projects.spec_history("p1")) == 2

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted = ProductProjectSpecDurabilityService(restarted_store)
    assert (
        restarted.update_spec(
            "p1",
            revised,
            expected_row_version=0,
            idempotency_key="spec:p1:approved-v2",
            change_reason="Owner approved scope v2",
        )
        == first
    )
    with restarted_store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM product_project_specs WHERE project_id='p1'"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM product_project_spec_idempotency WHERE project_id='p1'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type='product_project.spec_mutation_committed' AND entity_id='p1'"
        ).fetchone()[0] == 1


def test_same_idempotency_key_rejects_input_or_expected_version_drift(tmp_path) -> None:
    store, projects = _store(tmp_path)
    service = ProductProjectSpecDurabilityService(store)
    original = _spec("Approved scope")
    service.update_spec(
        "p1",
        original,
        expected_row_version=0,
        idempotency_key="spec:p1:once",
        change_reason="Approved once",
    )

    with pytest.raises(ProductProjectError, match="different specification mutation input"):
        service.update_spec(
            "p1",
            _spec("Different scope"),
            expected_row_version=0,
            idempotency_key="spec:p1:once",
            change_reason="Approved once",
        )
    with pytest.raises(ProductProjectError, match="different specification mutation input"):
        service.update_spec(
            "p1",
            original,
            expected_row_version=1,
            idempotency_key="spec:p1:once",
            change_reason="Approved once",
        )
    with pytest.raises(ProductProjectError, match="different specification mutation input"):
        service.update_spec(
            "p1",
            original,
            expected_row_version=0,
            idempotency_key="spec:p1:once",
            change_reason="Changed reason",
        )
    assert projects.get("p1").spec_version == 2
    assert projects.get("p1").row_version == 1


def test_stale_or_invalid_expected_row_version_never_mutates_history(tmp_path) -> None:
    store, projects = _store(tmp_path)
    service = ProductProjectSpecDurabilityService(store)

    for invalid in (True, -1, 1.0, "0"):
        with pytest.raises(ProductProjectError, match="non-negative integer"):
            service.update_spec(
                "p1",
                _spec("Rejected invalid version type"),
                expected_row_version=invalid,  # type: ignore[arg-type]
                idempotency_key=f"invalid:{invalid!r}",
            )
    with pytest.raises(StaleProjectVersionError):
        service.update_spec(
            "p1",
            _spec("Stale scope"),
            expected_row_version=1,
            idempotency_key="spec:p1:stale",
        )
    assert projects.get("p1").spec_version == 1
    assert projects.get("p1").row_version == 0
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM product_project_spec_idempotency"
        ).fetchone()[0] == 0


def test_replay_remains_stable_after_later_project_mutations(tmp_path) -> None:
    store, projects = _store(tmp_path)
    service = ProductProjectSpecDurabilityService(store)
    v2 = _spec("Durable scope v2")
    first = service.update_spec(
        "p1",
        v2,
        expected_row_version=0,
        idempotency_key="spec:p1:v2",
        change_reason="Approve v2",
    )
    projects.update_spec(
        "p1",
        _spec("Legacy writer scope v3"),
        expected_row_version=1,
        change_reason="Legacy compatible writer",
    )
    assert projects.get("p1").spec_version == 3
    assert projects.get("p1").row_version == 2

    replay = service.update_spec(
        "p1",
        v2,
        expected_row_version=0,
        idempotency_key="spec:p1:v2",
        change_reason="Approve v2",
    )
    assert replay == first
    assert projects.get("p1").spec_version == 3
    assert len(projects.spec_history("p1")) == 3


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            "UPDATE product_project_spec_idempotency SET spec_sha256='"
            + "0" * 64
            + "' WHERE operation_key='spec:p1:v2'",
            "digest does not match durable specification",
        ),
        (
            "UPDATE product_project_spec_idempotency SET result_row_version=9 "
            "WHERE operation_key='spec:p1:v2'",
            "row-version lineage",
        ),
        (
            "UPDATE product_project_spec_idempotency SET result_spec_version=9 "
            "WHERE operation_key='spec:p1:v2'",
            "version lineage",
        ),
    ],
)
def test_corrupt_spec_idempotency_ledger_fails_closed(tmp_path, mutation, message) -> None:
    store, _ = _store(tmp_path)
    service = ProductProjectSpecDurabilityService(store)
    revised = _spec("Durable scope v2")
    service.update_spec(
        "p1",
        revised,
        expected_row_version=0,
        idempotency_key="spec:p1:v2",
        change_reason="Approve v2",
    )
    with store.connection() as conn:
        conn.execute(mutation)

    with pytest.raises(ProductProjectError, match=message):
        service.update_spec(
            "p1",
            revised,
            expected_row_version=0,
            idempotency_key="spec:p1:v2",
            change_reason="Approve v2",
        )


def test_missing_or_forged_commit_evidence_fails_closed_on_replay(tmp_path) -> None:
    store, _ = _store(tmp_path)
    service = ProductProjectSpecDurabilityService(store)
    revised = _spec("Durable scope v2")
    service.update_spec(
        "p1",
        revised,
        expected_row_version=0,
        idempotency_key="spec:p1:v2",
        change_reason="Approve v2",
    )
    with store.connection() as conn:
        conn.execute(
            "DELETE FROM audit_events "
            "WHERE event_type='product_project.spec_mutation_committed' AND entity_id='p1'"
        )
    with pytest.raises(ProductProjectError, match="exactly one durable audit evidence"):
        service.update_spec(
            "p1",
            revised,
            expected_row_version=0,
            idempotency_key="spec:p1:v2",
            change_reason="Approve v2",
        )

    with store.connection() as conn:
        conn.execute(
            "INSERT INTO audit_events(event_type,entity_type,entity_id,payload_json,created_at) "
            "SELECT 'product_project.spec_mutation_committed','product_project','p1',"
            "'{\"operation_key\":\"spec:p1:v2\"}',created_at "
            "FROM product_project_spec_idempotency WHERE operation_key='spec:p1:v2'"
        )
    with pytest.raises(ProductProjectError, match="disagrees with durable audit evidence"):
        service.update_spec(
            "p1",
            revised,
            expected_row_version=0,
            idempotency_key="spec:p1:v2",
            change_reason="Approve v2",
        )


def test_rehashed_spec_corruption_is_detected_before_retry_credit(tmp_path) -> None:
    store, _ = _store(tmp_path)
    service = ProductProjectSpecDurabilityService(store)
    revised = _spec("Durable scope v2")
    service.update_spec(
        "p1",
        revised,
        expected_row_version=0,
        idempotency_key="spec:p1:v2",
        change_reason="Approve v2",
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_project_specs SET spec_json='{}' "
            "WHERE project_id='p1' AND spec_version=2"
        )
    with pytest.raises(ProductProjectError, match="digest does not match"):
        service.update_spec(
            "p1",
            revised,
            expected_row_version=0,
            idempotency_key="spec:p1:v2",
            change_reason="Approve v2",
        )


def test_same_key_concurrent_retry_commits_one_revision(tmp_path) -> None:
    store, projects = _store(tmp_path)
    revised = _spec("Concurrent durable scope v2")

    def mutate():
        service = ProductProjectSpecDurabilityService(SQLiteStore(store.path))
        return service.update_spec(
            "p1",
            revised,
            expected_row_version=0,
            idempotency_key="spec:p1:concurrent-v2",
            change_reason="Concurrent retry proof",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: mutate(), range(2)))
    assert receipts[0] == receipts[1]
    assert projects.get("p1").spec_version == 2
    assert projects.get("p1").row_version == 1
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM product_project_specs WHERE project_id='p1'"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM product_project_spec_idempotency WHERE project_id='p1'"
        ).fetchone()[0] == 1


def test_schema_v3_and_many_revisions_survive_repeated_restarts(tmp_path) -> None:
    store, _ = _store(tmp_path)
    with store.connection() as conn:
        version = conn.execute(
            "SELECT MAX(version) FROM product_project_schema_migrations"
        ).fetchone()[0]
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(product_project_spec_idempotency)")
        }
    assert version == PRODUCT_PROJECT_SCHEMA_VERSION == 3
    assert {
        "operation_key",
        "expected_row_version",
        "result_spec_version",
        "result_row_version",
        "spec_sha256",
    } <= columns

    for index in range(1, 61):
        if index % 10 == 1:
            store = SQLiteStore(store.path)
            store.initialize()
        service = ProductProjectSpecDurabilityService(store)
        service.update_spec(
            "p1",
            _spec(f"Long-horizon durable scope {index}"),
            expected_row_version=index - 1,
            idempotency_key=f"spec:p1:scale:{index}",
            change_reason=f"Approved long-horizon revision {index}",
        )

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_projects = ProductProjectRepository(restarted_store)
    project = restarted_projects.get("p1")
    assert project.spec_version == 61
    assert project.row_version == 60
    history = restarted_projects.spec_history("p1")
    assert len(history) == 61
    assert history[-1].supersedes_spec_version == 60
    assert history[-1].change_reason == "Approved long-horizon revision 60"

    first_replay = ProductProjectSpecDurabilityService(restarted_store).update_spec(
        "p1",
        _spec("Long-horizon durable scope 1"),
        expected_row_version=0,
        idempotency_key="spec:p1:scale:1",
        change_reason="Approved long-horizon revision 1",
    )
    assert first_replay.spec_version == 2
    assert first_replay.row_version == 1
    assert restarted_projects.get("p1").spec_version == 61
