from __future__ import annotations

import json

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    ProductAcceptanceCriterion,
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
    StaleProjectVersionError,
)
from nika_core.product_project_history_archive import ProductProjectHistoryArchiveService
from nika_core.product_project_history_segments import (
    ProductProjectHistoryRetentionPolicy,
    ProductProjectHistorySegmentService,
)
from nika_core.product_project_lifecycle import (
    ProductProjectLifecycleService,
    ProductProjectState,
)


def _spec(index: int = 0, requirement_count: int = 1) -> ProductProjectSpec:
    requirements = tuple(
        ProductRequirement(
            requirement_id=f"requirement-{item}",
            text=f"Deterministic requirement {item}",
            acceptance=(f"Acceptance {item}",),
            acceptance_criteria=(
                ProductAcceptanceCriterion(
                    criterion_id=f"criterion-{item}",
                    text=f"Criterion {item} remains restart-safe",
                ),
            ),
        )
        for item in range(requirement_count)
    )
    return ProductProjectSpec(
        goal="Build a long-lived generic product",
        desired_outcome="Segmented verifiable durable history",
        requirements=requirements,
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
        name="Segment qualification",
        spec=_spec(),
        idempotency_key="create:project-1",
    )
    return store, projects


def _segment_bytes(bundle) -> tuple[bytes, ...]:
    return tuple(segment.bytes for segment in bundle.segments)


def test_segments_are_deterministic_and_reassemble_exact_archive(tmp_path) -> None:
    store, projects = _project(tmp_path)
    current = projects.get("project-1")
    for index in range(1, 16):
        current = projects.update_spec(
            "project-1",
            _spec(index),
            expected_row_version=current.row_version,
            change_reason=f"scope revision {index}",
        )

    archive = ProductProjectHistoryArchiveService(store).build("project-1")
    service = ProductProjectHistorySegmentService(store)
    first = service.build("project-1", target_entries_per_segment=7)
    restarted = SQLiteStore(store.path)
    restarted.initialize()
    second = ProductProjectHistorySegmentService(restarted).build(
        "project-1",
        target_entries_per_segment=7,
    )

    assert first.manifest_bytes == second.manifest_bytes
    assert _segment_bytes(first) == _segment_bytes(second)
    assert service.reassemble(first.manifest_bytes, _segment_bytes(first)) == archive.bytes
    assert (
        ProductProjectHistorySegmentService(restarted).verify_against_live(
            first.manifest_bytes,
            _segment_bytes(first),
        ).archive_digest_sha256
        == archive.summary.digest_sha256
    )


def test_segment_chain_rejects_missing_reordered_and_tampered_bytes(tmp_path) -> None:
    store, projects = _project(tmp_path)
    current = projects.get("project-1")
    for index in range(1, 12):
        current = projects.update_spec(
            "project-1",
            _spec(index),
            expected_row_version=current.row_version,
            change_reason=f"revision {index}",
        )
    service = ProductProjectHistorySegmentService(store)
    bundle = service.build("project-1", target_entries_per_segment=5)
    segments = list(_segment_bytes(bundle))

    with pytest.raises(ProductProjectError, match="count"):
        service.verify(bundle.manifest_bytes, segments[:-1])

    reordered = segments.copy()
    reordered[0], reordered[1] = reordered[1], reordered[0]
    with pytest.raises(ProductProjectError):
        service.verify(bundle.manifest_bytes, reordered)

    envelope = json.loads(segments[0])
    envelope["payload"]["records"][0]["value"]["name"] = "tampered"
    tampered = segments.copy()
    tampered[0] = json.dumps(envelope, sort_keys=True).encode("utf-8")
    with pytest.raises(ProductProjectError, match="digest"):
        service.verify(bundle.manifest_bytes, tampered)


def test_segments_become_stale_after_new_live_mutation(tmp_path) -> None:
    store, projects = _project(tmp_path)
    service = ProductProjectHistorySegmentService(store)
    bundle = service.build("project-1", target_entries_per_segment=3)
    current = projects.get("project-1")
    projects.update_spec(
        "project-1",
        _spec(2),
        expected_row_version=current.row_version,
        change_reason="new scope after checkpoint manifest",
    )

    with pytest.raises(StaleProjectVersionError):
        service.verify_against_live(bundle.manifest_bytes, _segment_bytes(bundle))


def test_retention_plan_never_authorizes_destructive_delete(tmp_path) -> None:
    store, projects = _project(tmp_path)
    current = projects.get("project-1")
    for index in range(1, 30):
        current = projects.update_spec(
            "project-1",
            _spec(index),
            expected_row_version=current.row_version,
            change_reason=f"retention revision {index}",
        )
    service = ProductProjectHistorySegmentService(store)
    bundle = service.build("project-1", target_entries_per_segment=4)
    plan = service.plan_retention(
        bundle.manifest_bytes,
        _segment_bytes(bundle),
        ProductProjectHistoryRetentionPolicy(hot_segment_count=3),
    )

    assert plan.hot_sequences == tuple(
        range(len(bundle.segments) - 2, len(bundle.segments) + 1)
    )
    assert plan.cold_sequences == tuple(range(1, len(bundle.segments) - 2))
    assert plan.destructive_delete_allowed is False
    with pytest.raises(ProductProjectError, match="destructive deletion"):
        ProductProjectHistoryRetentionPolicy(
            hot_segment_count=1,
            allow_destructive_delete=True,
        )


def test_retention_policy_allows_zero_or_more_hot_segments_without_history_cap(tmp_path) -> None:
    store, projects = _project(tmp_path)
    current = projects.get("project-1")
    for index in range(1, 101):
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

    service = ProductProjectHistorySegmentService(store)
    bundle = service.build("project-1", target_entries_per_segment=11)
    all_cold = service.plan_retention(
        bundle.manifest_bytes,
        _segment_bytes(bundle),
        ProductProjectHistoryRetentionPolicy(hot_segment_count=0),
    )
    all_hot = service.plan_retention(
        bundle.manifest_bytes,
        _segment_bytes(bundle),
        ProductProjectHistoryRetentionPolicy(hot_segment_count=10_000),
    )

    assert len(bundle.segments) > 10
    assert all_cold.hot_sequences == ()
    assert len(all_cold.cold_sequences) == len(bundle.segments)
    assert len(all_hot.hot_sequences) == len(bundle.segments)
    assert all_hot.cold_sequences == ()


def test_segment_snapshot_includes_lifecycle_history_and_survives_restart(tmp_path) -> None:
    store, projects = _project(tmp_path)
    current = projects.get("project-1")
    lifecycle = ProductProjectLifecycleService(store)
    paused = lifecycle.transition(
        "project-1",
        ProductProjectState.PAUSED,
        expected_row_version=current.row_version,
        idempotency_key="lifecycle:pause",
        reason="planned pause",
        changed_by_ref="user://owner",
    )
    lifecycle.transition(
        "project-1",
        ProductProjectState.ACTIVE,
        expected_row_version=paused.row_version,
        idempotency_key="lifecycle:resume",
        reason="resume",
        changed_by_ref="user://owner",
    )

    bundle = ProductProjectHistorySegmentService(store).build(
        "project-1",
        target_entries_per_segment=2,
    )
    restarted = SQLiteStore(store.path)
    restarted.initialize()
    summary = ProductProjectHistorySegmentService(restarted).verify_against_live(
        bundle.manifest_bytes,
        _segment_bytes(bundle),
    )

    counts = dict(summary.section_counts)
    assert summary.row_version == 2
    assert counts["audit_events"] >= 3
    assert counts["mutation_idempotency"] == 2


def test_invalid_segment_target_and_negative_retention_fail_closed(tmp_path) -> None:
    store, _ = _project(tmp_path)
    service = ProductProjectHistorySegmentService(store)
    with pytest.raises(ProductProjectError, match="positive"):
        service.build("project-1", target_entries_per_segment=0)
    with pytest.raises(ProductProjectError, match="non-negative"):
        ProductProjectHistoryRetentionPolicy(hot_segment_count=-1)
