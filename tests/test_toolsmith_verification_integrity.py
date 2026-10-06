from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.toolsmith import (
    CandidateState,
    CapabilityEscalationService,
    CapabilityGap,
    CapabilityManifestV1,
    CodingResult,
    DeterministicCodingWorker,
    GapKind,
    ReuseCandidate,
    ToolsmithRepository,
)

CAPABILITY_ID = "safe-config-repair"
VERIFIED_DIGEST = "a" * 64
SUBSTITUTED_DIGEST = "b" * 64
PERMISSIONS = frozenset({"repo.read", "repo.write", "tests.run"})


def _service(
    tmp_path: Path,
) -> tuple[str, SQLiteStore, ToolsmithRepository, CapabilityEscalationService]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="toolsmith.verification",
        agent_id="eng02-test",
        payload={"capability_id": CAPABILITY_ID},
    )
    repository = ToolsmithRepository(store)
    worker = DeterministicCodingWorker(lambda job: CodingResult(job_id=job.job_id))
    service = CapabilityEscalationService(
        repository=repository,
        checkpoints=CheckpointService(store),
        worker=worker,
    )
    return task.task_id, store, repository, service


def _gap(task_id: str) -> CapabilityGap:
    return CapabilityGap(
        task_id=task_id,
        requested_capability=CAPABILITY_ID,
        kind=GapKind.MISSING_CAPABILITY,
        reason="bounded capability is required",
        attempted_methods=("registry-search",),
        permission_ceiling=PERMISSIONS,
    )


def _candidate(
    *,
    digest: str = VERIFIED_DIGEST,
    permissions: frozenset[str] = frozenset({"repo.read"}),
    metadata: dict[str, str] | None = None,
    source: str = "catalog://trusted",
    version: str = "1.0.0",
) -> ReuseCandidate:
    return ReuseCandidate(
        capability_id=CAPABILITY_ID,
        version=version,
        source=source,
        digest=digest,
        permissions=permissions,
        metadata=metadata or {"license": "MIT", "origin": "catalog"},
    )


def _verified(
    *,
    task_id: str,
    repository: ToolsmithRepository,
    service: CapabilityEscalationService,
) -> tuple[CapabilityGap, int]:
    gap = _gap(task_id)
    version, state = service.begin(gap)
    assert state is CandidateState.PROPOSED
    version = repository.transition(
        task_id=task_id,
        capability_id=CAPABILITY_ID,
        expected_version=version,
        target=CandidateState.BUILD_REQUIRED,
    )
    version = repository.transition(
        task_id=task_id,
        capability_id=CAPABILITY_ID,
        expected_version=version,
        target=CandidateState.BUILDING,
    )
    version = repository.transition(
        task_id=task_id,
        capability_id=CAPABILITY_ID,
        expected_version=version,
        target=CandidateState.BUILT,
    )
    version = service.start_verification(gap=gap, expected_version=version)
    version = service.accept_verification(
        gap=gap,
        expected_version=version,
        candidate_digest=VERIFIED_DIGEST,
        verifier_evidence={"review_ref": "review://independent/exact"},
    )
    return gap, version


def _manifest(
    digest: str,
    *,
    entrypoint: str = "toolsmith.generated.safe_config_repair:run",
    source: str = "local://toolsmith/acceptance",
) -> CapabilityManifestV1:
    return CapabilityManifestV1(
        capability_id=CAPABILITY_ID,
        version="1.0.0",
        digest=digest,
        entrypoint=entrypoint,
        permissions=PERMISSIONS,
        source=source,
    )


def test_registration_cannot_substitute_artifact_after_independent_verification(
    tmp_path: Path,
) -> None:
    task_id, _, repository, service = _service(tmp_path)
    gap, version = _verified(task_id=task_id, repository=repository, service=service)

    with pytest.raises(ValueError, match="independently verified candidate"):
        service.register(
            gap=gap,
            expected_version=version,
            manifest=_manifest(SUBSTITUTED_DIGEST),
        )

    row = repository.get_escalation(task_id=task_id, capability_id=CAPABILITY_ID)
    assert row is not None
    assert row["state"] == CandidateState.VERIFIED.value
    assert int(row["row_version"]) == version
    assert row["pinned_digest"] == VERIFIED_DIGEST

    registered_version = service.register(
        gap=gap,
        expected_version=version,
        manifest=_manifest(VERIFIED_DIGEST),
    )
    assert registered_version == version + 2
    assert service.reconcile_resume(task_id=task_id, capability_id=CAPABILITY_ID) == {
        "task_id": task_id,
        "capability_id": CAPABILITY_ID,
        "version": "1.0.0",
        "digest": VERIFIED_DIGEST,
    }


def test_registry_same_digest_cannot_rebind_manifest_identity(tmp_path: Path) -> None:
    task_id, store, repository, service = _service(tmp_path)
    gap, version = _verified(task_id=task_id, repository=repository, service=service)
    service.register(
        gap=gap,
        expected_version=version,
        manifest=_manifest(VERIFIED_DIGEST),
    )

    with store.connection() as conn:
        original_registry = conn.execute(
            "SELECT digest, manifest_json, active FROM capability_registry "
            "WHERE capability_id = ? AND version = ?",
            (CAPABILITY_ID, "1.0.0"),
        ).fetchone()
    assert original_registry is not None

    second_task = TaskQueue(store).create(
        workspace_id="toolsmith.verification.second",
        agent_id="eng02-test",
        payload={"capability_id": CAPABILITY_ID},
    )
    second_gap, second_version = _verified(
        task_id=second_task.task_id,
        repository=repository,
        service=service,
    )
    with pytest.raises(RuntimeError, match="different manifest identity"):
        service.register(
            gap=second_gap,
            expected_version=second_version,
            manifest=_manifest(
                VERIFIED_DIGEST,
                entrypoint="toolsmith.generated.substituted:run",
            ),
        )

    escalation = repository.get_escalation(
        task_id=second_task.task_id,
        capability_id=CAPABILITY_ID,
    )
    assert escalation is not None
    assert escalation["state"] == CandidateState.REGISTERING.value
    assert escalation["pinned_version"] is None

    with store.connection() as conn:
        registry = conn.execute(
            "SELECT digest, manifest_json, active FROM capability_registry "
            "WHERE capability_id = ? AND version = ?",
            (CAPABILITY_ID, "1.0.0"),
        ).fetchone()
        resume = conn.execute(
            "SELECT 1 FROM capability_resume_bindings WHERE task_id = ? AND capability_id = ?",
            (second_task.task_id, CAPABILITY_ID),
        ).fetchone()
    assert registry is not None
    assert tuple(registry) == tuple(original_registry)
    assert resume is None


def test_exact_registry_identity_reactivates_after_rollback(tmp_path: Path) -> None:
    task_id, store, repository, service = _service(tmp_path)
    gap, version = _verified(task_id=task_id, repository=repository, service=service)
    registered_version = service.register(
        gap=gap,
        expected_version=version,
        manifest=_manifest(VERIFIED_DIGEST),
    )
    rolled_back_version = repository.transition(
        task_id=task_id,
        capability_id=CAPABILITY_ID,
        expected_version=registered_version,
        target=CandidateState.ROLLED_BACK,
    )
    assert rolled_back_version == registered_version + 1
    repository.rollback_registration(task_id=task_id, capability_id=CAPABILITY_ID)

    with store.connection() as conn:
        inactive = conn.execute(
            "SELECT active FROM capability_registry "
            "WHERE capability_id = ? AND version = ?",
            (CAPABILITY_ID, "1.0.0"),
        ).fetchone()
    assert inactive is not None
    assert int(inactive["active"]) == 0

    second_task = TaskQueue(store).create(
        workspace_id="toolsmith.verification.reactivation",
        agent_id="eng02-test",
        payload={"capability_id": CAPABILITY_ID},
    )
    second_gap, second_version = _verified(
        task_id=second_task.task_id,
        repository=repository,
        service=service,
    )
    service.register(
        gap=second_gap,
        expected_version=second_version,
        manifest=_manifest(VERIFIED_DIGEST),
    )

    with store.connection() as conn:
        active = conn.execute(
            "SELECT active FROM capability_registry "
            "WHERE capability_id = ? AND version = ?",
            (CAPABILITY_ID, "1.0.0"),
        ).fetchone()
    assert active is not None
    assert int(active["active"]) == 1


def test_verified_digest_survives_restart_before_registration(tmp_path: Path) -> None:
    task_id, store, repository, service = _service(tmp_path)
    gap, version = _verified(task_id=task_id, repository=repository, service=service)

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_repository = ToolsmithRepository(restarted_store)
    restarted_service = CapabilityEscalationService(
        repository=restarted_repository,
        checkpoints=CheckpointService(restarted_store),
        worker=DeterministicCodingWorker(lambda job: CodingResult(job_id=job.job_id)),
    )

    row = restarted_repository.get_escalation(task_id=task_id, capability_id=CAPABILITY_ID)
    assert row is not None
    assert row["state"] == CandidateState.VERIFIED.value
    assert row["pinned_digest"] == VERIFIED_DIGEST

    with pytest.raises(ValueError, match="independently verified candidate"):
        restarted_service.register(
            gap=gap,
            expected_version=version,
            manifest=_manifest(SUBSTITUTED_DIGEST),
        )


def test_search_candidate_identity_survives_restart_and_rejects_digest_substitution(
    tmp_path: Path,
) -> None:
    task_id, store, repository, service = _service(tmp_path)
    service.begin(_gap(task_id))
    repository.record_search_candidate(task_id=task_id, candidate=_candidate())

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_repository = ToolsmithRepository(restarted_store)
    with pytest.raises(RuntimeError, match="prior durable search identity"):
        restarted_repository.record_search_candidate(
            task_id=task_id,
            candidate=_candidate(digest=SUBSTITUTED_DIGEST),
        )

    with restarted_store.connection() as conn:
        rows = conn.execute(
            "SELECT digest FROM capability_search_candidates "
            "WHERE task_id = ? AND capability_id = ? AND version = ? AND source = ?",
            (task_id, CAPABILITY_ID, "1.0.0", "catalog://trusted"),
        ).fetchall()
    assert [str(row["digest"]) for row in rows] == [VERIFIED_DIGEST]


def test_search_candidate_same_digest_cannot_change_permissions_or_provenance(
    tmp_path: Path,
) -> None:
    task_id, _, repository, service = _service(tmp_path)
    service.begin(_gap(task_id))
    repository.record_search_candidate(task_id=task_id, candidate=_candidate())

    with pytest.raises(RuntimeError, match="prior durable search identity"):
        repository.record_search_candidate(
            task_id=task_id,
            candidate=_candidate(permissions=frozenset({"repo.read", "tests.run"})),
        )
    with pytest.raises(RuntimeError, match="prior durable search identity"):
        repository.record_search_candidate(
            task_id=task_id,
            candidate=_candidate(metadata={"license": "Apache-2.0", "origin": "catalog"}),
        )


def test_search_candidate_exact_replay_is_idempotent_and_distinct_identity_is_allowed(
    tmp_path: Path,
) -> None:
    task_id, store, repository, service = _service(tmp_path)
    service.begin(_gap(task_id))
    candidate = _candidate()
    repository.record_search_candidate(task_id=task_id, candidate=candidate)
    repository.record_search_candidate(task_id=task_id, candidate=candidate)
    repository.record_search_candidate(
        task_id=task_id,
        candidate=_candidate(source="catalog://secondary"),
    )
    repository.record_search_candidate(
        task_id=task_id,
        candidate=_candidate(version="2.0.0"),
    )

    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM capability_search_candidates WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    assert count is not None
    assert int(count["count"]) == 3


def test_search_candidate_historical_equivocation_fails_closed(tmp_path: Path) -> None:
    task_id, store, repository, service = _service(tmp_path)
    service.begin(_gap(task_id))
    repository.record_search_candidate(task_id=task_id, candidate=_candidate())
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO capability_search_candidates("
            "task_id, capability_id, version, source, digest, permissions_json, metadata_json, "
            "created_at) SELECT task_id, capability_id, version, source, ?, permissions_json, "
            "metadata_json, created_at FROM capability_search_candidates WHERE task_id = ?",
            (SUBSTITUTED_DIGEST, task_id),
        )

    with pytest.raises(RuntimeError, match="prior durable search identity"):
        repository.record_search_candidate(task_id=task_id, candidate=_candidate())


def test_legacy_verified_row_without_digest_fails_closed(tmp_path: Path) -> None:
    task_id, _, repository, service = _service(tmp_path)
    gap = _gap(task_id)
    version, _ = service.begin(gap)
    for target in (
        CandidateState.BUILD_REQUIRED,
        CandidateState.BUILDING,
        CandidateState.BUILT,
        CandidateState.VERIFYING,
        CandidateState.VERIFIED,
    ):
        version = repository.transition(
            task_id=task_id,
            capability_id=CAPABILITY_ID,
            expected_version=version,
            target=target,
        )

    with pytest.raises(RuntimeError, match="missing exact candidate digest"):
        service.register(gap=gap, expected_version=version, manifest=_manifest(VERIFIED_DIGEST))

    row = repository.get_escalation(task_id=task_id, capability_id=CAPABILITY_ID)
    assert row is not None
    assert row["state"] == CandidateState.VERIFIED.value
    assert row["pinned_digest"] is None
