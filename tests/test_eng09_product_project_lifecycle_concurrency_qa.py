from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

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
    ProductProjectStatusTransition,
)


def _setup_project(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id="p1",
        name="ENG09 PF0 concurrency QA",
        spec=ProductProjectSpec(
            goal="Prove serialized ProductProject lifecycle authority",
            desired_outcome="Concurrent retries have one durable outcome",
        ),
        idempotency_key="create:p1",
    )
    return store, projects, ProductProjectLifecycleService(store)


def _mutation_counts(store: SQLiteStore) -> tuple[int, int]:
    with store.connection() as conn:
        receipt_count = conn.execute(
            "SELECT COUNT(*) FROM product_project_mutation_idempotency "
            "WHERE project_id='p1' AND operation_kind='product_project.status_transition'"
        ).fetchone()[0]
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type='product_project.status_changed' "
            "AND entity_type='product_project' AND entity_id='p1'"
        ).fetchone()[0]
    return int(receipt_count), int(audit_count)


def test_same_key_concurrent_retry_commits_one_transition_and_replays_exactly(tmp_path) -> None:
    store, projects, lifecycle = _setup_project(tmp_path)
    barrier = Barrier(2)

    def run_retry() -> ProductProjectStatusTransition:
        barrier.wait(timeout=5)
        return lifecycle.transition(
            "p1",
            ProductProjectState.PAUSED,
            expected_row_version=0,
            idempotency_key="status:p1:pause:concurrent",
            reason="Concurrent exact retry",
            changed_by_ref="agent://eng09",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(run_retry)
        second_future = executor.submit(run_retry)
        first = first_future.result(timeout=10)
        second = second_future.result(timeout=10)

    assert first == second
    assert first.row_version == 1
    assert projects.get("p1").row_version == 1
    assert projects.get("p1").status == ProductProjectState.PAUSED.value
    assert _mutation_counts(store) == (1, 1)

    restarted = ProductProjectLifecycleService(SQLiteStore(store.path))
    replay = restarted.transition(
        "p1",
        ProductProjectState.PAUSED,
        expected_row_version=0,
        idempotency_key="status:p1:pause:concurrent",
        reason="Concurrent exact retry",
        changed_by_ref="agent://eng09",
    )
    assert replay == first
    assert restarted.history("p1") == (
        restarted.history("p1")[0],
        first,
    )


def test_different_keys_same_expected_version_have_one_winner_and_one_stale_writer(tmp_path) -> None:
    store, projects, lifecycle = _setup_project(tmp_path)
    barrier = Barrier(2)

    def run_transition(
        state: ProductProjectState,
        key: str,
        reason: str,
    ) -> ProductProjectStatusTransition | str:
        barrier.wait(timeout=5)
        try:
            return lifecycle.transition(
                "p1",
                state,
                expected_row_version=0,
                idempotency_key=key,
                reason=reason,
                changed_by_ref="agent://eng09",
            )
        except StaleProjectVersionError:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as executor:
        paused_future = executor.submit(
            run_transition,
            ProductProjectState.PAUSED,
            "status:p1:pause:writer-a",
            "Writer A pause",
        )
        blocked_future = executor.submit(
            run_transition,
            ProductProjectState.BLOCKED,
            "status:p1:block:writer-b",
            "Writer B block",
        )
        outcomes = (
            paused_future.result(timeout=10),
            blocked_future.result(timeout=10),
        )

    winners = [item for item in outcomes if isinstance(item, ProductProjectStatusTransition)]
    assert len(winners) == 1
    assert outcomes.count("stale") == 1
    winner = winners[0]
    assert winner.row_version == 1
    assert projects.get("p1").row_version == 1
    assert projects.get("p1").status == winner.new_state.value
    assert _mutation_counts(store) == (1, 1)

    restarted = ProductProjectLifecycleService(SQLiteStore(store.path))
    history = restarted.history("p1")
    assert len(history) == 2
    assert history[1] == winner
    assert restarted.current_state("p1") is winner.new_state


def test_same_key_conflicting_concurrent_input_is_rejected_and_lock_is_released(tmp_path) -> None:
    store, projects, lifecycle = _setup_project(tmp_path)
    barrier = Barrier(2)

    def run_transition(
        state: ProductProjectState,
        reason: str,
    ) -> tuple[str, ProductProjectStatusTransition | str]:
        barrier.wait(timeout=5)
        try:
            transition = lifecycle.transition(
                "p1",
                state,
                expected_row_version=0,
                idempotency_key="status:p1:shared-conflicting-key",
                reason=reason,
                changed_by_ref="agent://eng09",
            )
        except ProductProjectError as exc:
            return "error", str(exc)
        return "ok", transition

    with ThreadPoolExecutor(max_workers=2) as executor:
        paused_future = executor.submit(
            run_transition,
            ProductProjectState.PAUSED,
            "Conflicting pause",
        )
        blocked_future = executor.submit(
            run_transition,
            ProductProjectState.BLOCKED,
            "Conflicting block",
        )
        outcomes = (
            paused_future.result(timeout=10),
            blocked_future.result(timeout=10),
        )

    assert sorted(kind for kind, _ in outcomes) == ["error", "ok"]
    error = next(value for kind, value in outcomes if kind == "error")
    winner = next(value for kind, value in outcomes if kind == "ok")
    assert isinstance(error, str)
    assert "different mutation input" in error
    assert isinstance(winner, ProductProjectStatusTransition)
    assert winner.row_version == 1
    assert projects.get("p1").status == winner.new_state.value
    assert _mutation_counts(store) == (1, 1)

    resumed = lifecycle.transition(
        "p1",
        ProductProjectState.ACTIVE,
        expected_row_version=1,
        idempotency_key="status:p1:resume:after-conflict",
        reason="Prove failed contender released its transaction",
        changed_by_ref="agent://eng09",
    )
    assert resumed.row_version == 2
    assert projects.get("p1").status == ProductProjectState.ACTIVE.value
    assert _mutation_counts(store) == (2, 2)

    replay_state = winner.new_state
    replay_reason = "Conflicting pause" if replay_state is ProductProjectState.PAUSED else "Conflicting block"
    restarted = ProductProjectLifecycleService(SQLiteStore(store.path))
    historical_replay = restarted.transition(
        "p1",
        replay_state,
        expected_row_version=0,
        idempotency_key="status:p1:shared-conflicting-key",
        reason=replay_reason,
        changed_by_ref="agent://eng09",
    )
    assert historical_replay == winner
    assert restarted.current_state("p1") is ProductProjectState.ACTIVE


@pytest.mark.parametrize("state", [ProductProjectState.PAUSED, ProductProjectState.BLOCKED])
def test_post_concurrency_restart_history_remains_canonical(tmp_path, state) -> None:
    store, _, lifecycle = _setup_project(tmp_path)
    first = lifecycle.transition(
        "p1",
        state,
        expected_row_version=0,
        idempotency_key=f"status:p1:{state.value}:restart",
        reason=f"Persist {state.value} before restart",
        changed_by_ref="agent://eng09",
    )
    restarted = ProductProjectLifecycleService(SQLiteStore(store.path))
    assert restarted.history("p1")[1] == first
    assert restarted.current_state("p1") is state
