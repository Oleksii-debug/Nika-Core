from __future__ import annotations

import json

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
)
from nika_core.product_project_lifecycle import (
    ProductProjectLifecycleService,
    ProductProjectState,
)


def _service(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id="p1",
        name="Project",
        spec=ProductProjectSpec(
            goal="Durable lifecycle",
            desired_outcome="Corruption fails closed",
        ),
        idempotency_key="create:p1",
    )
    return store, projects, ProductProjectLifecycleService(store)


def _pause(lifecycle: ProductProjectLifecycleService) -> None:
    lifecycle.transition(
        "p1",
        ProductProjectState.PAUSED,
        expected_row_version=0,
        idempotency_key="status:p1:pause",
        reason="Pause for review",
        changed_by_ref="user://owner",
    )


def _rewrite_only_status_audit(store: SQLiteStore, mutate) -> None:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT event_id,payload_json FROM audit_events "
            "WHERE event_type='product_project.status_changed' AND entity_id='p1'"
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        mutate(payload)
        conn.execute(
            "UPDATE audit_events SET payload_json=? WHERE event_id=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), row["event_id"]),
        )


@pytest.mark.parametrize("value", [True, False, -1, 1.5, "0"])
def test_transition_expected_row_version_requires_exact_nonnegative_int(tmp_path, value) -> None:
    _, projects, lifecycle = _service(tmp_path)

    with pytest.raises(ProductProjectError, match="expected_row_version"):
        lifecycle.transition(
            "p1",
            ProductProjectState.PAUSED,
            expected_row_version=value,  # type: ignore[arg-type]
            idempotency_key=f"status:p1:bad:{value!r}",
            reason="Typed precondition",
            changed_by_ref="user://owner",
        )

    assert projects.get("p1").row_version == 0
    assert projects.get("p1").status == "active"


def test_boolean_audit_row_version_is_rejected_after_restart(tmp_path) -> None:
    store, _, lifecycle = _service(tmp_path)
    _pause(lifecycle)
    _rewrite_only_status_audit(store, lambda payload: payload.__setitem__("row_version", True))

    restarted = ProductProjectLifecycleService(SQLiteStore(store.path))
    with pytest.raises(ProductProjectError, match="audit row_version"):
        restarted.history("p1")


def test_non_string_audit_actor_is_rejected_after_restart(tmp_path) -> None:
    store, _, lifecycle = _service(tmp_path)
    _pause(lifecycle)
    _rewrite_only_status_audit(
        store,
        lambda payload: payload.__setitem__("changed_by_ref", ["user://owner"]),
    )

    restarted = ProductProjectLifecycleService(SQLiteStore(store.path))
    with pytest.raises(ProductProjectError, match="changed_by_ref"):
        restarted.history("p1")


def test_idempotent_replay_rejects_tampered_audit_payload(tmp_path) -> None:
    store, projects, lifecycle = _service(tmp_path)
    _pause(lifecycle)
    _rewrite_only_status_audit(
        store,
        lambda payload: payload.__setitem__("reason", "Tampered reason"),
    )

    restarted = ProductProjectLifecycleService(SQLiteStore(store.path))
    with pytest.raises(ProductProjectError, match="does not match durable audit evidence"):
        restarted.transition(
            "p1",
            ProductProjectState.PAUSED,
            expected_row_version=0,
            idempotency_key="status:p1:pause",
            reason="Pause for review",
            changed_by_ref="user://owner",
        )

    assert projects.get("p1").status == "paused"


def test_history_rejects_state_chain_tamper(tmp_path) -> None:
    store, _, lifecycle = _service(tmp_path)
    _pause(lifecycle)
    lifecycle.transition(
        "p1",
        ProductProjectState.ACTIVE,
        expected_row_version=1,
        idempotency_key="status:p1:resume",
        reason="Resume after review",
        changed_by_ref="user://owner",
    )

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT event_id,payload_json FROM audit_events "
            "WHERE event_type='product_project.status_changed' AND entity_id='p1' "
            "ORDER BY event_id"
        ).fetchall()
        assert len(rows) == 2
        payload = json.loads(rows[1]["payload_json"])
        payload["previous_state"] = ProductProjectState.BLOCKED.value
        conn.execute(
            "UPDATE audit_events SET payload_json=? WHERE event_id=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), rows[1]["event_id"]),
        )

    with pytest.raises(ProductProjectError, match="state chain is inconsistent"):
        ProductProjectLifecycleService(SQLiteStore(store.path)).history("p1")


def test_history_rejects_non_monotonic_status_versions(tmp_path) -> None:
    store, _, lifecycle = _service(tmp_path)
    _pause(lifecycle)
    lifecycle.transition(
        "p1",
        ProductProjectState.ACTIVE,
        expected_row_version=1,
        idempotency_key="status:p1:resume",
        reason="Resume after review",
        changed_by_ref="user://owner",
    )

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT event_id,payload_json FROM audit_events "
            "WHERE event_type='product_project.status_changed' AND entity_id='p1' "
            "ORDER BY event_id"
        ).fetchall()
        payload = json.loads(rows[1]["payload_json"])
        payload["row_version"] = 1
        conn.execute(
            "UPDATE audit_events SET payload_json=? WHERE event_id=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), rows[1]["event_id"]),
        )

    with pytest.raises(ProductProjectError, match="increase monotonically"):
        ProductProjectLifecycleService(SQLiteStore(store.path)).history("p1")


def test_history_rejects_missing_audit_for_durable_nonactive_state(tmp_path) -> None:
    store, _, lifecycle = _service(tmp_path)
    _pause(lifecycle)
    with store.connection() as conn:
        conn.execute(
            "DELETE FROM audit_events WHERE event_type='product_project.status_changed' "
            "AND entity_id='p1'"
        )

    with pytest.raises(ProductProjectError, match="not backed by matching lifecycle audit"):
        ProductProjectLifecycleService(SQLiteStore(store.path)).history("p1")


def test_token_shaped_tamper_is_rejected_on_read(tmp_path) -> None:
    store, _, lifecycle = _service(tmp_path)
    _pause(lifecycle)
    _rewrite_only_status_audit(
        store,
        lambda payload: payload.__setitem__(
            "reason",
            "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
        ),
    )

    with pytest.raises(ProductProjectError, match="token-shaped"):
        ProductProjectLifecycleService(SQLiteStore(store.path)).history("p1")
