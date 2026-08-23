from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    StaleProjectVersionError,
)
from nika_core.product_project_spec_durability import ProductProjectSpecDurabilityService


def _repo(tmp_path, *, name: str = "nika.db") -> tuple[SQLiteStore, ProductProjectRepository]:
    store = SQLiteStore(tmp_path / name)
    store.initialize()
    return store, ProductProjectRepository(store)


def _spec(hypothesis: str = "initial") -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Build a durable product",
        desired_outcome="Versioned restart-safe ProductProject",
        hypothesis=hypothesis,
    )


def _create(repo: ProductProjectRepository, project_id: str = "p1"):
    return repo.create(
        project_id=project_id,
        name="Durable",
        spec=_spec(),
        idempotency_key=f"create:{project_id}",
    )


def test_canonical_update_spec_is_restart_idempotent_without_explicit_key(tmp_path) -> None:
    store, repo = _repo(tmp_path)
    created = _create(repo)

    updated = repo.update_spec(
        "p1",
        _spec("revision-2"),
        expected_row_version=created.row_version,
        change_reason="scope revision",
    )
    restarted = ProductProjectRepository(SQLiteStore(store.path))
    replay = restarted.update_spec(
        "p1",
        _spec("revision-2"),
        expected_row_version=created.row_version,
        change_reason="scope revision",
    )

    assert (updated.spec_version, updated.row_version) == (2, 1)
    assert (replay.spec_version, replay.row_version) == (2, 1)
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM product_project_specs WHERE project_id='p1'"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM product_project_spec_idempotency WHERE project_id='p1'"
        ).fetchone()[0] == 1
        events = conn.execute(
            "SELECT event_type,payload_json FROM audit_events "
            "WHERE entity_type='product_project' AND entity_id='p1' ORDER BY event_id"
        ).fetchall()
    versioned = [row for row in events if row["event_type"] == "product_project.spec_versioned"]
    assert len(versioned) == 1
    assert all(row["event_type"] != "product_project.spec_mutation_committed" for row in events)
    payload = json.loads(versioned[0]["payload_json"])
    assert payload["spec_version"] == 2
    assert payload["supersedes_spec_version"] == 1
    assert payload["row_version"] == 1
    assert len(payload["operation_key_sha256"]) == 64


def test_compatibility_service_delegates_to_canonical_writer(tmp_path) -> None:
    store, repo = _repo(tmp_path)
    created = _create(repo)
    service = ProductProjectSpecDurabilityService(store)

    receipt = service.update_spec(
        "p1",
        _spec("compatibility"),
        expected_row_version=created.row_version,
        idempotency_key="spec:p1:compat",
        change_reason="compatibility facade",
    )
    replay = ProductProjectSpecDurabilityService(SQLiteStore(store.path)).update_spec(
        "p1",
        _spec("compatibility"),
        expected_row_version=created.row_version,
        idempotency_key="spec:p1:compat",
        change_reason="compatibility facade",
    )

    assert replay == receipt
    assert (receipt.previous_spec_version, receipt.spec_version) == (1, 2)
    assert (receipt.previous_row_version, receipt.row_version) == (0, 1)
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type='product_project.spec_versioned'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type='product_project.spec_mutation_committed'"
        ).fetchone()[0] == 0


def test_explicit_key_conflict_and_stale_distinct_write_fail_closed(tmp_path) -> None:
    _, repo = _repo(tmp_path)
    created = _create(repo)
    repo.update_spec(
        "p1",
        _spec("first"),
        expected_row_version=created.row_version,
        idempotency_key="spec:key",
        change_reason="first",
    )

    with pytest.raises(ProductProjectError, match="different specification mutation input"):
        repo.update_spec(
            "p1",
            _spec("different"),
            expected_row_version=created.row_version,
            idempotency_key="spec:key",
            change_reason="first",
        )
    with pytest.raises(StaleProjectVersionError):
        repo.update_spec(
            "p1",
            _spec("other operation"),
            expected_row_version=created.row_version,
            idempotency_key="spec:other",
        )


def test_replay_after_later_mutation_does_not_create_another_revision(tmp_path) -> None:
    store, repo = _repo(tmp_path)
    created = _create(repo)
    first = ProductProjectSpecDurabilityService(store).update_spec(
        "p1",
        _spec("first"),
        expected_row_version=created.row_version,
        idempotency_key="spec:first",
        change_reason="first",
    )
    second = repo.update_spec(
        "p1",
        _spec("second"),
        expected_row_version=first.row_version,
        idempotency_key="spec:second",
        change_reason="second",
    )
    replay = ProductProjectSpecDurabilityService(SQLiteStore(store.path)).update_spec(
        "p1",
        _spec("first"),
        expected_row_version=created.row_version,
        idempotency_key="spec:first",
        change_reason="first",
    )

    assert replay == first
    assert (second.spec_version, second.row_version) == (3, 2)
    assert repo.get("p1").spec_version == 3
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM product_project_specs WHERE project_id='p1'"
        ).fetchone()[0] == 3


def test_same_key_concurrent_writers_commit_one_revision(tmp_path) -> None:
    store, repo = _repo(tmp_path)
    created = _create(repo)
    barrier = threading.Barrier(3)

    def worker() -> tuple[int, int]:
        candidate = ProductProjectRepository(SQLiteStore(store.path))
        barrier.wait()
        result = candidate.update_spec(
            "p1",
            _spec("concurrent"),
            expected_row_version=created.row_version,
            idempotency_key="spec:concurrent",
            change_reason="concurrent",
        )
        return result.spec_version, result.row_version

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="spec-writer") as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        barrier.wait()
        results = sorted(future.result(timeout=10) for future in futures)

    assert results == [(2, 1), (2, 1)]
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM product_project_specs WHERE project_id='p1'"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM product_project_spec_idempotency "
            "WHERE operation_key='spec:concurrent'"
        ).fetchone()[0] == 1


@pytest.mark.parametrize("bad_version", [True, False, 0.0, 0.5, "0", None])
def test_update_spec_rejects_non_exact_expected_row_version(tmp_path, bad_version: object) -> None:
    _, repo = _repo(tmp_path)
    _create(repo)
    with pytest.raises(ProductProjectError, match="expected_row_version"):
        repo.update_spec("p1", _spec("bad"), expected_row_version=bad_version)


def test_update_spec_rejects_corrupt_durable_project_version_metadata(tmp_path) -> None:
    store, repo = _repo(tmp_path)
    _create(repo)
    with store.connection() as conn:
        conn.execute("UPDATE product_projects SET row_version=0.5 WHERE project_id='p1'")
    with pytest.raises(ProductProjectError, match="row_version"):
        repo.update_spec("p1", _spec("bad durable row"), expected_row_version=0)


def test_replay_fails_closed_on_ledger_spec_or_audit_tamper(tmp_path) -> None:
    store, repo = _repo(tmp_path)
    created = _create(repo)
    repo.update_spec(
        "p1",
        _spec("tamper target"),
        expected_row_version=created.row_version,
        idempotency_key="spec:tamper",
        change_reason="tamper target",
    )

    with store.connection() as conn:
        event_id = conn.execute(
            "SELECT MAX(event_id) FROM audit_events "
            "WHERE event_type='product_project.spec_versioned' AND entity_id='p1'"
        ).fetchone()[0]
        conn.execute("UPDATE audit_events SET payload_json='{}' WHERE event_id=?", (event_id,))
    with pytest.raises(ProductProjectError, match="audit"):
        repo.update_spec(
            "p1",
            _spec("tamper target"),
            expected_row_version=created.row_version,
            idempotency_key="spec:tamper",
            change_reason="tamper target",
        )


def test_get_distinguishes_missing_project_from_missing_current_spec(tmp_path) -> None:
    store, repo = _repo(tmp_path)
    _create(repo)
    with pytest.raises(KeyError):
        repo.get("missing")

    with store.connection() as conn:
        conn.execute("DELETE FROM product_project_specs WHERE project_id='p1' AND spec_version=1")
    with pytest.raises(
        ProductProjectError,
        match="current ProductProject specification is missing",
    ):
        repo.get("p1")


def test_sixty_spec_revisions_survive_repeated_store_reconstruction(tmp_path) -> None:
    store, repo = _repo(tmp_path)
    project = _create(repo)
    for revision in range(2, 62):
        repo = ProductProjectRepository(SQLiteStore(store.path))
        project = repo.update_spec(
            "p1",
            replace(project.spec, hypothesis=f"revision-{revision}"),
            expected_row_version=project.row_version,
            idempotency_key=f"spec:p1:{revision}",
            change_reason=f"revision {revision}",
        )

    restarted = ProductProjectRepository(SQLiteStore(store.path))
    recovered = restarted.get("p1")
    assert recovered.spec_version == 61
    assert recovered.row_version == 60
    assert recovered.spec.hypothesis == "revision-61"
    history = restarted.spec_history("p1")
    assert len(history) == 61
    assert history[-1].supersedes_spec_version == 60
