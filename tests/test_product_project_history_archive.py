from __future__ import annotations

import hashlib
import json

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    ProductAcceptanceCriterion,
    ProductMilestone,
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
    StaleProjectVersionError,
)
from nika_core.product_project_history_archive import ProductProjectHistoryArchiveService
from nika_core.product_project_lifecycle import (
    ProductProjectLifecycleService,
    ProductProjectState,
)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _spec(index: int = 0) -> ProductProjectSpec:
    criterion = ProductAcceptanceCriterion(
        criterion_id="criterion-1",
        text="Keyboard-only completion is deterministic",
    )
    requirement = ProductRequirement(
        requirement_id="requirement-1",
        text="Accessible project operation",
        acceptance=("Automated contract check",),
        acceptance_criteria=(criterion,),
    )
    milestone = ProductMilestone(
        milestone_id="milestone-1",
        title="Release candidate",
        acceptance_criterion_ids=(criterion.criterion_id,),
    )
    return ProductProjectSpec(
        goal="Build a durable generic product",
        desired_outcome="Restart-safe product history",
        requirements=(requirement,),
        milestones=(milestone,),
        repository_refs=("repo://primary",),
        build_refs=(f"build://{index}",),
        release_refs=(f"release://{index}",),
        deployment_refs=(f"deployment://{index}",),
        incident_refs=(f"incident://{index}",),
    )


def _project(tmp_path) -> tuple[SQLiteStore, ProductProjectRepository]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    projects.create(
        project_id="project-1",
        name="Archive qualification",
        spec=_spec(),
        idempotency_key="create:project-1",
    )
    return store, projects


def test_archive_is_deterministic_and_restart_safe(tmp_path) -> None:
    store, projects = _project(tmp_path)
    current = projects.get("project-1")
    for index in range(1, 9):
        current = projects.update_spec(
            "project-1",
            _spec(index),
            expected_row_version=current.row_version,
            change_reason=f"scope revision {index}",
        )

    lifecycle = ProductProjectLifecycleService(store)
    paused = lifecycle.transition(
        "project-1",
        ProductProjectState.PAUSED,
        expected_row_version=current.row_version,
        idempotency_key="status:pause",
        reason="planned maintenance window",
        changed_by_ref="user://owner",
    )
    lifecycle.transition(
        "project-1",
        ProductProjectState.ACTIVE,
        expected_row_version=paused.row_version,
        idempotency_key="status:resume",
        reason="maintenance complete",
        changed_by_ref="user://owner",
    )

    first = ProductProjectHistoryArchiveService(store).build("project-1")
    restarted = SQLiteStore(store.path)
    restarted.initialize()
    second = ProductProjectHistoryArchiveService(restarted).build("project-1")

    assert first.bytes == second.bytes
    assert first.summary.digest_sha256 == second.summary.digest_sha256
    assert first.summary.spec_count == 9
    assert first.summary.row_version == 10
    assert ProductProjectHistoryArchiveService(restarted).verify(first.bytes) == first.summary
    assert (
        ProductProjectHistoryArchiveService(restarted).verify_against_live(first.bytes)
        == first.summary
    )


def test_archive_preserves_superseded_release_and_operations_refs(tmp_path) -> None:
    store, projects = _project(tmp_path)
    created = projects.get("project-1")
    projects.update_spec(
        "project-1",
        _spec(1),
        expected_row_version=created.row_version,
        change_reason="new release line",
    )

    archive = ProductProjectHistoryArchiveService(store).build("project-1")
    envelope = json.loads(archive.bytes)
    specs = envelope["payload"]["history"]["specs"]

    assert specs[0]["spec"]["release_refs"] == ["release://0"]
    assert specs[0]["spec"]["deployment_refs"] == ["deployment://0"]
    assert specs[0]["spec"]["incident_refs"] == ["incident://0"]
    assert specs[1]["spec"]["release_refs"] == ["release://1"]
    assert specs[1]["spec"]["deployment_refs"] == ["deployment://1"]
    assert specs[1]["spec"]["incident_refs"] == ["incident://1"]


def test_archive_detects_payload_tamper(tmp_path) -> None:
    store, _ = _project(tmp_path)
    service = ProductProjectHistoryArchiveService(store)
    archive = service.build("project-1")
    envelope = json.loads(archive.bytes)
    envelope["payload"]["history"]["project"]["name"] = "tampered"
    tampered = _canonical(envelope).encode("utf-8")

    with pytest.raises(ProductProjectError, match="digest mismatch"):
        service.verify(tampered)


def test_rehashed_forgery_is_rejected_against_live_history(tmp_path) -> None:
    store, _ = _project(tmp_path)
    service = ProductProjectHistoryArchiveService(store)
    archive = service.build("project-1")
    envelope = json.loads(archive.bytes)
    envelope["payload"]["history"]["project"]["name"] = "forged"
    envelope["digest_sha256"] = hashlib.sha256(
        _canonical(envelope["payload"]).encode("utf-8")
    ).hexdigest()
    forged = _canonical(envelope).encode("utf-8")

    assert service.verify(forged).project_id == "project-1"
    with pytest.raises(ProductProjectError, match="differs from durable live history"):
        service.verify_against_live(forged)


def test_archive_becomes_stale_after_new_project_mutation(tmp_path) -> None:
    store, projects = _project(tmp_path)
    service = ProductProjectHistoryArchiveService(store)
    archive = service.build("project-1")
    current = projects.get("project-1")
    projects.update_spec(
        "project-1",
        _spec(2),
        expected_row_version=current.row_version,
        change_reason="post-archive scope change",
    )

    with pytest.raises(StaleProjectVersionError):
        service.verify_against_live(archive.bytes)


def test_archive_fails_closed_on_corrupt_durable_history(tmp_path) -> None:
    store, _ = _project(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_project_specs SET spec_json='not-json' "
            "WHERE project_id='project-1' AND spec_version=1"
        )

    with pytest.raises(ProductProjectError):
        ProductProjectHistoryArchiveService(store).build("project-1")


def test_archive_rejects_unknown_schema_even_with_matching_digest(tmp_path) -> None:
    store, _ = _project(tmp_path)
    service = ProductProjectHistoryArchiveService(store)
    archive = service.build("project-1")
    envelope = json.loads(archive.bytes)
    envelope["payload"]["schema"] = "nika-product-project-history-archive-v999"
    envelope["digest_sha256"] = hashlib.sha256(
        _canonical(envelope["payload"]).encode("utf-8")
    ).hexdigest()
    unsupported = _canonical(envelope).encode("utf-8")

    with pytest.raises(ProductProjectError, match="unsupported"):
        service.verify(unsupported)


def test_large_history_archive_survives_repeated_restarts(tmp_path) -> None:
    store, projects = _project(tmp_path)
    current = projects.get("project-1")
    for index in range(1, 121):
        current = projects.update_spec(
            "project-1",
            _spec(index),
            expected_row_version=current.row_version,
            change_reason=f"long horizon revision {index}",
        )
        if index % 20 == 0:
            store = SQLiteStore(store.path)
            store.initialize()
            projects = ProductProjectRepository(store)
            current = projects.get("project-1")

    service = ProductProjectHistoryArchiveService(store)
    archive = service.build("project-1")
    restarted = SQLiteStore(store.path)
    restarted.initialize()
    verified = ProductProjectHistoryArchiveService(restarted).verify_against_live(
        archive.bytes
    )

    assert verified.spec_count == 121
    assert verified.row_version == 120
    assert verified.digest_sha256 == archive.summary.digest_sha256
    assert len(archive.bytes) > 100_000
