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


def _verified(
    *,
    task_id: str,
    repository: ToolsmithRepository,
    service: CapabilityEscalationService,
) -> tuple[CapabilityGap, int]:
    gap = CapabilityGap(
        task_id=task_id,
        requested_capability=CAPABILITY_ID,
        kind=GapKind.MISSING_CAPABILITY,
        reason="bounded capability is required",
        attempted_methods=("registry-search",),
        permission_ceiling=PERMISSIONS,
    )
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


def _manifest(digest: str) -> CapabilityManifestV1:
    return CapabilityManifestV1(
        capability_id=CAPABILITY_ID,
        version="1.0.0",
        digest=digest,
        entrypoint="toolsmith.generated.safe_config_repair:run",
        permissions=PERMISSIONS,
        source="local://toolsmith/acceptance",
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


def test_legacy_verified_row_without_digest_fails_closed(tmp_path: Path) -> None:
    task_id, _, repository, service = _service(tmp_path)
    gap = CapabilityGap(
        task_id=task_id,
        requested_capability=CAPABILITY_ID,
        kind=GapKind.MISSING_CAPABILITY,
        reason="bounded capability is required",
        attempted_methods=("registry-search",),
        permission_ceiling=PERMISSIONS,
    )
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
