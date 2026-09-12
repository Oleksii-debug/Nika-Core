from __future__ import annotations

import threading
from pathlib import Path

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
from nika_core.toolsmith.repository import StaleTransitionError

CAPABILITY_ID = "resume-atomicity"
DIGEST = "c" * 64
PERMISSIONS = frozenset({"repo.read"})


def _registered(
    tmp_path: Path,
) -> tuple[str, SQLiteStore, ToolsmithRepository, CapabilityEscalationService]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="toolsmith.resume.atomicity",
        agent_id="eng02-test",
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
        reason="resume publication must remain rollback-atomic",
        attempted_methods=("registry-search",),
        permission_ceiling=PERMISSIONS,
    )
    version, state = repository.create_escalation(gap)
    assert state is CandidateState.PROPOSED
    for target in (
        CandidateState.BUILD_REQUIRED,
        CandidateState.BUILDING,
        CandidateState.BUILT,
        CandidateState.VERIFYING,
    ):
        version = repository.transition(
            task_id=task.task_id,
            capability_id=CAPABILITY_ID,
            expected_version=version,
            target=target,
        )
    version = repository.accept_verification(
        task_id=task.task_id,
        capability_id=CAPABILITY_ID,
        expected_version=version,
        candidate_digest=DIGEST,
        verifier_evidence={"review_ref": "review://independent/resume-atomicity"},
    )
    version = repository.transition(
        task_id=task.task_id,
        capability_id=CAPABILITY_ID,
        expected_version=version,
        target=CandidateState.REGISTERING,
    )
    repository.register_exact(
        task_id=task.task_id,
        manifest=CapabilityManifestV1(
            capability_id=CAPABILITY_ID,
            version="1.0.0",
            digest=DIGEST,
            entrypoint="toolsmith.generated.resume_atomicity:run",
            permissions=PERMISSIONS,
            source="local://toolsmith/acceptance",
        ),
    )
    version = repository.transition(
        task_id=task.task_id,
        capability_id=CAPABILITY_ID,
        expected_version=version,
        target=CandidateState.REGISTERED,
    )
    repository.mark_resume_ready(task_id=task.task_id, capability_id=CAPABILITY_ID)
    return task.task_id, store, repository, service


def test_resume_binding_publication_fails_closed_if_rollback_wins_after_preflight(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task_id, store, repository, _ = _registered(tmp_path)
    original_get = repository.get_escalation
    preflight_seen = threading.Event()
    allow_writer = threading.Event()

    def gated_get_escalation(*, task_id: str, capability_id: str):
        row = original_get(task_id=task_id, capability_id=capability_id)
        if threading.current_thread().name == "resume-writer" and not preflight_seen.is_set():
            preflight_seen.set()
            assert allow_writer.wait(5), "rollback did not complete before resume writer continued"
        return row

    monkeypatch.setattr(repository, "get_escalation", gated_get_escalation)
    writer_errors: list[BaseException] = []

    def publish_resume() -> None:
        try:
            repository.mark_resume_ready(task_id=task_id, capability_id=CAPABILITY_ID)
        except BaseException as exc:  # noqa: BLE001 - exact exception asserted below
            writer_errors.append(exc)

    writer = threading.Thread(target=publish_resume, name="resume-writer")
    writer.start()
    assert preflight_seen.wait(5), "resume writer never reached preflight boundary"

    durable = original_get(task_id=task_id, capability_id=CAPABILITY_ID)
    assert durable is not None
    repository.transition(
        task_id=task_id,
        capability_id=CAPABILITY_ID,
        expected_version=int(durable["row_version"]),
        target=CandidateState.ROLLED_BACK,
    )
    repository.rollback_registration(task_id=task_id, capability_id=CAPABILITY_ID)
    allow_writer.set()
    writer.join(5)
    assert not writer.is_alive(), "resume writer did not terminate"

    assert len(writer_errors) == 1
    assert isinstance(writer_errors[0], StaleTransitionError)
    with store.connection() as conn:
        binding = conn.execute(
            "SELECT status FROM capability_resume_bindings "
            "WHERE task_id = ? AND capability_id = ?",
            (task_id, CAPABILITY_ID),
        ).fetchone()
        registry = conn.execute(
            "SELECT active FROM capability_registry "
            "WHERE capability_id = ? AND version = ? AND digest = ?",
            (CAPABILITY_ID, "1.0.0", DIGEST),
        ).fetchone()
    assert binding is None
    assert registry is not None
    assert int(registry["active"]) == 0


def test_reconcile_resume_returns_only_authoritative_publication_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task_id, _, repository, service = _registered(tmp_path)
    authoritative = {
        "task_id": task_id,
        "capability_id": CAPABILITY_ID,
        "version": "authoritative-version",
        "digest": "d" * 64,
    }

    def authoritative_publication(*, task_id: str, capability_id: str) -> dict[str, str]:
        assert task_id == authoritative["task_id"]
        assert capability_id == authoritative["capability_id"]
        return authoritative

    monkeypatch.setattr(repository, "mark_resume_ready", authoritative_publication)
    assert service.reconcile_resume(task_id=task_id, capability_id=CAPABILITY_ID) == authoritative
