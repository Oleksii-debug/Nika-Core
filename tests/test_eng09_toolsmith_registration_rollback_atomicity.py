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
    StaleTransitionError,
    ToolsmithRepository,
)

CAPABILITY_ID = "eng09.atomic-registration"
DIGEST = "a" * 64
PERMISSIONS = frozenset({"repo.read", "tests.run"})


def _verified_candidate(
    tmp_path: Path,
) -> tuple[
    SQLiteStore,
    ToolsmithRepository,
    CapabilityEscalationService,
    CapabilityGap,
    int,
]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="eng09.toolsmith",
        agent_id="eng09-qa",
        payload={"capability_id": CAPABILITY_ID},
    )
    repository = ToolsmithRepository(store)
    service = CapabilityEscalationService(
        repository=repository,
        checkpoints=CheckpointService(store),
        worker=DeterministicCodingWorker(lambda job: CodingResult(job_id=job.job_id)),
    )
    gap = CapabilityGap(
        task_id=task.task_id,
        requested_capability=CAPABILITY_ID,
        kind=GapKind.MISSING_CAPABILITY,
        reason="independent atomic-registration qualification",
        attempted_methods=("registry-search",),
        permission_ceiling=PERMISSIONS,
    )
    version, state = service.begin(gap)
    assert state is CandidateState.PROPOSED
    for target in (
        CandidateState.BUILD_REQUIRED,
        CandidateState.BUILDING,
        CandidateState.BUILT,
    ):
        version = repository.transition(
            task_id=gap.task_id,
            capability_id=gap.requested_capability,
            expected_version=version,
            target=target,
        )
    version = service.start_verification(gap=gap, expected_version=version)
    version = service.accept_verification(
        gap=gap,
        expected_version=version,
        candidate_digest=DIGEST,
        verifier_evidence={"oracle": "eng09-independent-rollback-atomicity"},
    )
    return store, repository, service, gap, version


def _manifest() -> CapabilityManifestV1:
    return CapabilityManifestV1(
        capability_id=CAPABILITY_ID,
        version="1.0.0",
        digest=DIGEST,
        entrypoint="toolsmith.generated.atomic_registration:run",
        permissions=PERMISSIONS,
        source="local://eng09/toolsmith-atomicity",
    )


def test_rollback_wins_before_registry_write_without_active_capability_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repository, service, gap, verified_version = _verified_candidate(tmp_path)
    original_get = repository.get_escalation
    rollback_injected = False

    def get_with_rollback(*, task_id: str, capability_id: str) -> dict[str, object] | None:
        nonlocal rollback_injected
        row = original_get(task_id=task_id, capability_id=capability_id)
        if (
            row is not None
            and not rollback_injected
            and row["state"] == CandidateState.REGISTERING.value
        ):
            rollback_injected = True
            registering_version = int(row["row_version"])
            repository.transition(
                task_id=task_id,
                capability_id=capability_id,
                expected_version=registering_version,
                target=CandidateState.ROLLED_BACK,
                evidence={"reason": "concurrent rollback before registry publication"},
            )
            repository.rollback_registration(task_id=task_id, capability_id=capability_id)
        return row

    monkeypatch.setattr(repository, "get_escalation", get_with_rollback)

    with pytest.raises(StaleTransitionError):
        service.register(
            gap=gap,
            expected_version=verified_version,
            manifest=_manifest(),
        )

    assert rollback_injected
    row = original_get(task_id=gap.task_id, capability_id=gap.requested_capability)
    assert row is not None
    assert row["state"] == CandidateState.ROLLED_BACK.value

    with store.connection() as conn:
        active = conn.execute(
            "SELECT digest FROM capability_registry "
            "WHERE capability_id = ? AND version = ? AND active = 1",
            (CAPABILITY_ID, "1.0.0"),
        ).fetchone()
        resume = conn.execute(
            "SELECT status FROM capability_resume_bindings "
            "WHERE task_id = ? AND capability_id = ?",
            (gap.task_id, CAPABILITY_ID),
        ).fetchone()

    assert active is None, "ROLLED_BACK escalation must not leave an active registry capability"
    assert resume is None


def test_exact_verified_registration_remains_healthy_without_rollback(tmp_path: Path) -> None:
    store, repository, service, gap, verified_version = _verified_candidate(tmp_path)

    final_version = service.register(
        gap=gap,
        expected_version=verified_version,
        manifest=_manifest(),
    )

    row = repository.get_escalation(
        task_id=gap.task_id,
        capability_id=gap.requested_capability,
    )
    assert row is not None
    assert row["state"] == CandidateState.REGISTERED.value
    assert int(row["row_version"]) == final_version
    assert row["pinned_digest"] == DIGEST

    with store.connection() as conn:
        active = conn.execute(
            "SELECT digest FROM capability_registry "
            "WHERE capability_id = ? AND version = ? AND active = 1",
            (CAPABILITY_ID, "1.0.0"),
        ).fetchone()

    assert active is not None
    assert active["digest"] == DIGEST
