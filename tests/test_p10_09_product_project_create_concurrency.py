from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
)


def _spec() -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Serialize create authority",
        desired_outcome="Concurrent create calls converge without duplicate durable effects",
    )


def test_same_key_concurrent_create_converges_and_replays_after_restart(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    barrier = Barrier(2)

    def create_once():
        repo = ProductProjectRepository(SQLiteStore(store.path))
        barrier.wait()
        return repo.create(
            project_id="p1",
            name="Concurrent product",
            spec=_spec(),
            idempotency_key="create:p1",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create_once) for _ in range(2)]
        first = futures[0].result(timeout=10)
        second = futures[1].result(timeout=10)

    assert first == second

    with store.connection() as conn:
        project_count = conn.execute(
            "SELECT COUNT(*) FROM product_projects WHERE project_id = ?",
            ("p1",),
        ).fetchone()[0]
        spec_count = conn.execute(
            "SELECT COUNT(*) FROM product_project_specs WHERE project_id = ?",
            ("p1",),
        ).fetchone()[0]
        idempotency_count = conn.execute(
            "SELECT COUNT(*) FROM product_project_idempotency "
            "WHERE operation_key = ?",
            ("create:p1",),
        ).fetchone()[0]

    assert project_count == 1
    assert spec_count == 1
    assert idempotency_count == 1

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_repo = ProductProjectRepository(restarted_store)
    replay = restarted_repo.create(
        project_id="p1",
        name="Concurrent product",
        spec=_spec(),
        idempotency_key="create:p1",
    )

    assert replay == first
    history = restarted_repo.spec_history("p1")
    assert len(history) == 1
    assert history[0].spec_version == 1


def test_distinct_writer_conflict_semantics_remain_fail_closed(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    barrier = Barrier(2)

    def create_once(operation_key: str):
        repo = ProductProjectRepository(SQLiteStore(store.path))
        barrier.wait()
        try:
            return (
                "ok",
                repo.create(
                    project_id="p1",
                    name="Distinct writer product",
                    spec=_spec(),
                    idempotency_key=operation_key,
                ),
            )
        except ProductProjectError as exc:
            return ("error", str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(create_once, "create:p1:a"),
            pool.submit(create_once, "create:p1:b"),
        ]
        outcomes = [future.result(timeout=10) for future in futures]

    statuses = sorted(outcome[0] for outcome in outcomes)
    assert statuses == ["error", "ok"]
    error_text = next(outcome[1] for outcome in outcomes if outcome[0] == "error")
    assert error_text == "product project already exists: p1"

    with store.connection() as conn:
        project_count = conn.execute(
            "SELECT COUNT(*) FROM product_projects WHERE project_id = ?",
            ("p1",),
        ).fetchone()[0]
        spec_count = conn.execute(
            "SELECT COUNT(*) FROM product_project_specs WHERE project_id = ?",
            ("p1",),
        ).fetchone()[0]
        idempotency_count = conn.execute(
            "SELECT COUNT(*) FROM product_project_idempotency WHERE project_id = ?",
            ("p1",),
        ).fetchone()[0]

    assert project_count == 1
    assert spec_count == 1
    assert idempotency_count == 1
