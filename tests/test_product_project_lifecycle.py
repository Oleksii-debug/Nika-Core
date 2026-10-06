from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    StaleProjectVersionError,
)
from nika_core.product_project_lifecycle import (
    ProductProjectLifecycleService,
    ProductProjectState,
)


def _create_project(
    store: SQLiteStore,
    project_id: str = "p1",
) -> ProductProjectRepository:
    repo = ProductProjectRepository(store)
    repo.create(
        project_id=project_id,
        name=f"Project {project_id}",
        spec=ProductProjectSpec(
            goal=f"Build durable product {project_id}",
            desired_outcome="A restart-safe accepted product",
        ),
        idempotency_key=f"create:{project_id}",
    )
    return repo


def _repos(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = _create_project(store)
    return store, projects, ProductProjectLifecycleService(store)


def test_transition_is_durable_audited_idempotent_and_restart_safe(tmp_path) -> None:
    store, projects, lifecycle = _repos(tmp_path)
    first = lifecycle.transition(
        "p1",
        ProductProjectState.PAUSED,
        expected_row_version=0,
        idempotency_key="status:p1:pause:1",
        reason="Awaiting product-direction approval",
        changed_by_ref="user://owner",
    )
    replay = lifecycle.transition(
        "p1",
        ProductProjectState.PAUSED,
        expected_row_version=0,
        idempotency_key="status:p1:pause:1",
        reason="Awaiting product-direction approval",
        changed_by_ref="user://owner",
    )

    assert replay == first
    assert projects.get("p1").status == "paused"
    assert projects.get("p1").row_version == 1
    assert [item.new_state for item in lifecycle.history("p1")] == [
        ProductProjectState.ACTIVE,
        ProductProjectState.PAUSED,
    ]

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted = ProductProjectLifecycleService(restarted_store)
    assert restarted.current_state("p1") is ProductProjectState.PAUSED
    assert restarted.history("p1")[1] == first

    with restarted_store.connection() as conn:
        event = conn.execute(
            "SELECT payload_json FROM audit_events "
            "WHERE event_type='product_project.status_changed' AND entity_id='p1'"
        ).fetchone()
    assert event is not None
    assert "Awaiting product-direction approval" in event["payload_json"]


def test_lifecycle_and_spec_updates_share_one_optimistic_row_version(tmp_path) -> None:
    _, projects, lifecycle = _repos(tmp_path)
    lifecycle.transition(
        "p1",
        ProductProjectState.PAUSED,
        expected_row_version=0,
        idempotency_key="status:p1:pause",
        reason="Scope review",
        changed_by_ref="policy://product-review",
    )

    with pytest.raises(StaleProjectVersionError):
        projects.update_spec(
            "p1",
            ProductProjectSpec(goal="Stale", desired_outcome="Rejected"),
            expected_row_version=0,
        )

    updated = projects.update_spec(
        "p1",
        ProductProjectSpec(
            goal="Revised durable product",
            desired_outcome="Accepted revised scope",
        ),
        expected_row_version=1,
        change_reason="Approved scope revision",
    )
    assert updated.row_version == 2
    assert updated.status == "paused"

    with pytest.raises(StaleProjectVersionError):
        lifecycle.transition(
            "p1",
            ProductProjectState.ACTIVE,
            expected_row_version=1,
            idempotency_key="status:p1:stale-resume",
            reason="Stale resume",
            changed_by_ref="policy://product-review",
        )

    resumed = lifecycle.transition(
        "p1",
        ProductProjectState.ACTIVE,
        expected_row_version=2,
        idempotency_key="status:p1:resume",
        reason="Scope revision approved",
        changed_by_ref="user://owner",
    )
    assert resumed.row_version == 3
    assert projects.get("p1").spec_version == 2
    assert projects.get("p1").status == "active"


def test_stale_and_conflicting_idempotency_fail_closed(tmp_path) -> None:
    _, projects, lifecycle = _repos(tmp_path)
    lifecycle.transition(
        "p1",
        ProductProjectState.BLOCKED,
        expected_row_version=0,
        idempotency_key="status:p1:block",
        reason="External dependency unavailable",
        changed_by_ref="agent://coordinator",
    )

    with pytest.raises(StaleProjectVersionError):
        lifecycle.transition(
            "p1",
            ProductProjectState.ACTIVE,
            expected_row_version=0,
            idempotency_key="status:p1:stale",
            reason="Retry",
            changed_by_ref="agent://coordinator",
        )
    with pytest.raises(ProductProjectError, match="different mutation input"):
        lifecycle.transition(
            "p1",
            ProductProjectState.PAUSED,
            expected_row_version=1,
            idempotency_key="status:p1:block",
            reason="Different mutation",
            changed_by_ref="agent://coordinator",
        )
    assert projects.get("p1").status == "blocked"
    assert projects.get("p1").row_version == 1


def test_state_machine_and_unknown_durable_state_fail_closed(tmp_path) -> None:
    store, projects, lifecycle = _repos(tmp_path)
    completed = lifecycle.transition(
        "p1",
        ProductProjectState.COMPLETED,
        expected_row_version=0,
        idempotency_key="status:p1:complete",
        reason="Acceptance evidence complete",
        changed_by_ref="policy://product-gate",
    )
    assert completed.new_state is ProductProjectState.COMPLETED
    assert lifecycle.is_runnable(projects.get("p1")) is False

    with pytest.raises(ProductProjectError, match="must change state"):
        lifecycle.transition(
            "p1",
            ProductProjectState.COMPLETED,
            expected_row_version=1,
            idempotency_key="status:p1:complete-again",
            reason="Duplicate completion",
            changed_by_ref="policy://product-gate",
        )

    archived = lifecycle.transition(
        "p1",
        ProductProjectState.ARCHIVED,
        expected_row_version=1,
        idempotency_key="status:p1:archive",
        reason="Project retained for history",
        changed_by_ref="user://owner",
    )
    assert archived.previous_state is ProductProjectState.COMPLETED

    with store.connection() as conn:
        conn.execute("UPDATE product_projects SET status='mystery' WHERE project_id='p1'")
    with pytest.raises(ProductProjectError, match="unsupported durable"):
        lifecycle.current_state("p1")
    with pytest.raises(ProductProjectError, match="unsupported durable"):
        lifecycle.is_runnable(projects.get("p1"))


def test_token_shaped_audit_material_is_rejected_before_persistence(tmp_path) -> None:
    store, projects, lifecycle = _repos(tmp_path)
    with pytest.raises(ProductProjectError, match="token-shaped"):
        lifecycle.transition(
            "p1",
            ProductProjectState.PAUSED,
            expected_row_version=0,
            idempotency_key="status:p1:token",
            reason="ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
            changed_by_ref="user://owner",
        )
    assert projects.get("p1").status == "active"
    assert projects.get("p1").row_version == 0
    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type='product_project.status_changed' AND entity_id='p1'"
        ).fetchone()[0]
    assert count == 0


def test_large_portfolio_repeated_transitions_survive_restart(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    for index in range(100):
        _create_project(store, f"project-{index:03d}")

    lifecycle = ProductProjectLifecycleService(store)
    for index in range(100):
        project_id = f"project-{index:03d}"
        lifecycle.transition(
            project_id,
            ProductProjectState.PAUSED,
            expected_row_version=0,
            idempotency_key=f"status:{project_id}:pause",
            reason="Scheduled long-horizon checkpoint",
            changed_by_ref="policy://portfolio-maintenance",
        )
        lifecycle.transition(
            project_id,
            ProductProjectState.ACTIVE,
            expected_row_version=1,
            idempotency_key=f"status:{project_id}:resume",
            reason="Checkpoint maintenance complete",
            changed_by_ref="policy://portfolio-maintenance",
        )

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted = ProductProjectLifecycleService(restarted_store)
    assert all(
        restarted.current_state(f"project-{index:03d}") is ProductProjectState.ACTIVE
        for index in range(100)
    )
    assert len(restarted.history("project-099")) == 3
    assert restarted.projects.get("project-099").row_version == 2


def test_corrupt_idempotency_without_matching_audit_fails_closed(tmp_path) -> None:
    store, _, lifecycle = _repos(tmp_path)
    first = lifecycle.transition(
        "p1",
        ProductProjectState.PAUSED,
        expected_row_version=0,
        idempotency_key="status:p1:pause",
        reason="Durable pause",
        changed_by_ref="user://owner",
    )
    assert first.row_version == 1
    with store.connection() as conn:
        conn.execute(
            "DELETE FROM audit_events WHERE event_type='product_project.status_changed' "
            "AND entity_id='p1'"
        )

    with pytest.raises(ProductProjectError, match="no matching durable audit evidence"):
        lifecycle.transition(
            "p1",
            ProductProjectState.PAUSED,
            expected_row_version=0,
            idempotency_key="status:p1:pause",
            reason="Durable pause",
            changed_by_ref="user://owner",
        )


def test_missing_project_and_non_enum_state_are_rejected(tmp_path) -> None:
    _, _, lifecycle = _repos(tmp_path)
    with pytest.raises(KeyError):
        lifecycle.transition(
            "missing",
            ProductProjectState.PAUSED,
            expected_row_version=0,
            idempotency_key="status:missing:pause",
            reason="Missing project",
            changed_by_ref="user://owner",
        )
    with pytest.raises(ProductProjectError, match="ProductProjectState"):
        lifecycle.transition(
            "p1",
            "paused",  # type: ignore[arg-type]
            expected_row_version=0,
            idempotency_key="status:p1:string",
            reason="Typed contract required",
            changed_by_ref="user://owner",
        )
