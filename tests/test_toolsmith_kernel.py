from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.data.schema import MIGRATIONS, SCHEMA_VERSION
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.toolsmith import (
    AcceptanceCommand,
    AllowedPathPolicy,
    CandidateState,
    CapabilityEscalationService,
    CapabilityGap,
    CapabilityManifestV1,
    ChangedFile,
    CodingJob,
    CodingResult,
    DeterministicCodingWorker,
    GapDisposition,
    GapKind,
    InvalidTransitionError,
    IsolationClass,
    NetworkPolicy,
    ProcessPolicy,
    RepositorySnapshot,
    ResourceBudget,
    ReuseCandidate,
    StaleTransitionError,
    TestEvidence,
    ToolsmithRepository,
    WorkspaceLease,
    classify_gap,
)


def _store(tmp_path: Path, task_id: str = "task-1") -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    now = datetime.now(UTC).isoformat()
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO tasks(task_id, workspace_id, agent_id, state, payload_json, created_at, updated_at) "
            "VALUES (?, 'software.factory', 'agent', 'paused', '{}', ?, ?)",
            (task_id, now, now),
        )
    return store


def _gap(kind: GapKind = GapKind.MISSING_CAPABILITY) -> CapabilityGap:
    return CapabilityGap(
        task_id="task-1",
        requested_capability="tool.example",
        kind=kind,
        reason="required capability is unavailable",
        attempted_methods=("tool-registry", "plugin-registry", "mcp-metadata", "workspace", "installed", "catalog"),
        permission_ceiling=frozenset({"fs.read", "workspace.write", "tests.run"}),
    )


def _job(tmp_path: Path) -> CodingJob:
    return CodingJob(
        job_id="job-1",
        task_id="task-1",
        goal="implement bounded example capability",
        repository=RepositorySnapshot(
            repository_id="Oleksii-debug/Nika-Core",
            base_sha="0" * 40,
            tree_digest="sha256:base",
        ),
        lease=WorkspaceLease(
            lease_id="lease-1",
            workspace_root=tmp_path / "worker",
            isolation_class=IsolationClass.POLICY_ONLY,
            expires_at="2026-08-20T00:00:00+00:00",
        ),
        allowed_paths=AllowedPathPolicy(("src/nika_core/toolsmith", "tests/test_toolsmith_kernel.py")),
        process_policy=ProcessPolicy(("python", "ruff")),
        network_policy=NetworkPolicy(),
        resource_budget=ResourceBudget(timeout_seconds=120, max_output_bytes=100_000, max_changed_files=8),
        acceptance_commands=(AcceptanceCommand(("python", "-m", "pytest", "tests/test_toolsmith_kernel.py")),),
        permission_ceiling=frozenset({"fs.read", "workspace.write", "tests.run"}),
    )


def _success(job: CodingJob) -> CodingResult:
    return CodingResult(
        job_id=job.job_id,
        changed_files=(ChangedFile("src/nika_core/toolsmith/example.py", "a" * 64, 12),),
        test_evidence=(
            TestEvidence(
                command=("python", "-m", "pytest", "tests/test_toolsmith_kernel.py"),
                exit_code=0,
                output_digest="sha256:test",
            ),
        ),
    )


@pytest.mark.parametrize(
    "kind",
    [
        GapKind.MISSING_INFORMATION,
        GapKind.AMBIGUOUS_GOAL,
        GapKind.TOOL_FAILED,
        GapKind.MODEL_FAILED,
        GapKind.PERMISSION_DENIED,
    ],
)
def test_classifier_blocks_non_capability_failures(kind: GapKind) -> None:
    assert classify_gap(_gap(kind)).disposition is GapDisposition.BLOCK


def test_classifier_reuses_existing_capability() -> None:
    assert classify_gap(_gap(GapKind.EXISTING_CAPABILITY_AVAILABLE)).disposition is GapDisposition.REUSE


def test_classifier_requires_search_evidence() -> None:
    gap = CapabilityGap(
        task_id="task-1",
        requested_capability="tool.example",
        kind=GapKind.MISSING_CAPABILITY,
        reason="missing",
        permission_ceiling=frozenset({"fs.read"}),
    )
    assert classify_gap(gap).disposition is GapDisposition.BLOCK


@pytest.mark.parametrize(
    "path",
    ["../escape.py", ".git/config", ".GIT/hooks/x", "C:\\temp\\x.py", "file.txt:stream", "/absolute/x.py"],
)
def test_allowed_path_policy_rejects_escape_and_git(path: str) -> None:
    policy = AllowedPathPolicy(("src",))
    with pytest.raises(ValueError):
        policy.allows(path)


def test_process_policy_rejects_generic_shell() -> None:
    with pytest.raises(ValueError):
        ProcessPolicy(("python",), shell_allowed=True)
    with pytest.raises(ValueError):
        AcceptanceCommand(("powershell.exe", "-Command", "pytest"))


def test_migration_8_is_ordered_and_creates_toolsmith_tables(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert 8 in MIGRATIONS
    assert store.schema_version() == SCHEMA_VERSION
    with store.connection() as conn:
        names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'capability_%'"
            ).fetchall()
        }
    assert {
        "capability_escalations",
        "capability_search_candidates",
        "capability_registry",
        "capability_resume_bindings",
    }.issubset(names)


def test_begin_is_duplicate_safe(tmp_path: Path) -> None:
    repo = ToolsmithRepository(_store(tmp_path))
    gap = _gap()
    assert repo.create_escalation(gap) == (0, CandidateState.PROPOSED)
    assert repo.create_escalation(gap) == (0, CandidateState.PROPOSED)


def test_row_version_transition_fails_closed_on_stale_writer(tmp_path: Path) -> None:
    repo = ToolsmithRepository(_store(tmp_path))
    gap = _gap()
    repo.create_escalation(gap)
    version = repo.transition(
        task_id=gap.task_id,
        capability_id=gap.requested_capability,
        expected_version=0,
        target=CandidateState.BUILD_REQUIRED,
    )
    assert version == 1
    with pytest.raises(StaleTransitionError):
        repo.transition(
            task_id=gap.task_id,
            capability_id=gap.requested_capability,
            expected_version=0,
            target=CandidateState.BUILDING,
        )


def test_invalid_state_transition_is_rejected(tmp_path: Path) -> None:
    repo = ToolsmithRepository(_store(tmp_path))
    gap = _gap()
    repo.create_escalation(gap)
    with pytest.raises(InvalidTransitionError):
        repo.transition(
            task_id=gap.task_id,
            capability_id=gap.requested_capability,
            expected_version=0,
            target=CandidateState.REGISTERED,
        )


def test_search_candidate_cannot_widen_permissions(tmp_path: Path) -> None:
    repo = ToolsmithRepository(_store(tmp_path))
    gap = _gap()
    repo.create_escalation(gap)
    candidate = ReuseCandidate(
        capability_id=gap.requested_capability,
        version="1.0.0",
        source="approved-catalog",
        digest="sha256:candidate",
        permissions=frozenset({"fs.read", "network.any"}),
    )
    with pytest.raises(PermissionError):
        repo.record_search_candidate(task_id=gap.task_id, candidate=candidate)


def test_checkpoint_first_block_protocol(tmp_path: Path) -> None:
    store = _store(tmp_path)
    worker = DeterministicCodingWorker(_success)
    service = CapabilityEscalationService(
        repository=ToolsmithRepository(store), checkpoints=CheckpointService(store), worker=worker
    )
    gap = _gap(GapKind.PERMISSION_DENIED)
    version, state = service.begin(gap)
    assert version == 1
    assert state is CandidateState.BLOCKED
    checkpoint = CheckpointService(store).latest(gap.task_id)
    assert checkpoint is not None
    assert checkpoint.stage == "capability_escalation_blocked"
    assert checkpoint.payload["capability_id"] == gap.requested_capability


def test_build_preserves_same_task_and_deduplicates_recovery(tmp_path: Path) -> None:
    store = _store(tmp_path)
    repo = ToolsmithRepository(store)
    gap = _gap()
    repo.create_escalation(gap)
    version = repo.transition(
        task_id=gap.task_id,
        capability_id=gap.requested_capability,
        expected_version=0,
        target=CandidateState.BUILD_REQUIRED,
    )
    worker = DeterministicCodingWorker(_success)
    service = CapabilityEscalationService(repository=repo, checkpoints=CheckpointService(store), worker=worker)
    job = _job(tmp_path)
    version, result = asyncio.run(service.build(gap=gap, job=job, expected_version=version))
    assert result.succeeded
    assert version == 3
    assert worker.executions == [job.job_id]


def test_worker_result_outside_allowed_scope_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    repo = ToolsmithRepository(store)
    gap = _gap()
    repo.create_escalation(gap)
    version = repo.transition(
        task_id=gap.task_id,
        capability_id=gap.requested_capability,
        expected_version=0,
        target=CandidateState.BUILD_REQUIRED,
    )

    def bad(job: CodingJob) -> CodingResult:
        return CodingResult(
            job_id=job.job_id,
            changed_files=(ChangedFile("src/nika_core/ui/desktop_backend.py", "b" * 64, 2),),
            test_evidence=(
                TestEvidence(
                    command=("python", "-m", "pytest", "tests/test_toolsmith_kernel.py"),
                    exit_code=0,
                    output_digest="sha256:test",
                ),
            ),
        )

    service = CapabilityEscalationService(
        repository=repo,
        checkpoints=CheckpointService(store),
        worker=DeterministicCodingWorker(bad),
    )
    with pytest.raises(ValueError, match="outside allowed scope"):
        asyncio.run(service.build(gap=gap, job=_job(tmp_path), expected_version=version))


def test_exact_registration_and_same_task_resume_binding(tmp_path: Path) -> None:
    store = _store(tmp_path)
    repo = ToolsmithRepository(store)
    gap = _gap()
    repo.create_escalation(gap)
    service = CapabilityEscalationService(
        repository=repo,
        checkpoints=CheckpointService(store),
        worker=DeterministicCodingWorker(_success),
    )
    version = repo.transition(
        task_id=gap.task_id,
        capability_id=gap.requested_capability,
        expected_version=0,
        target=CandidateState.BUILD_REQUIRED,
    )
    version = repo.transition(
        task_id=gap.task_id,
        capability_id=gap.requested_capability,
        expected_version=version,
        target=CandidateState.BUILDING,
    )
    version = repo.transition(
        task_id=gap.task_id,
        capability_id=gap.requested_capability,
        expected_version=version,
        target=CandidateState.BUILT,
    )
    version = service.start_verification(gap=gap, expected_version=version)
    version = service.accept_verification(
        gap=gap,
        expected_version=version,
        candidate_digest="sha256:verified-tree",
        verifier_evidence={"verifier": "test-independent"},
    )
    manifest = CapabilityManifestV1(
        capability_id=gap.requested_capability,
        version="1.2.3",
        digest="sha256:verified-tree",
        entrypoint="nika_core.toolsmith.example:run",
        permissions=frozenset({"fs.read", "tests.run"}),
        source="nika-verified-candidate",
    )
    version = service.register(gap=gap, expected_version=version, manifest=manifest)
    assert version == 7
    binding = service.reconcile_resume(task_id=gap.task_id, capability_id=gap.requested_capability)
    assert binding == {
        "task_id": gap.task_id,
        "capability_id": gap.requested_capability,
        "version": "1.2.3",
        "digest": "sha256:verified-tree",
    }
    with store.connection() as conn:
        row = conn.execute(
            "SELECT task_id, version, digest, status FROM capability_resume_bindings WHERE task_id = ?",
            (gap.task_id,),
        ).fetchone()
    assert row is not None
    assert row["task_id"] == gap.task_id
    assert row["status"] == "ready"


def test_exact_version_collision_with_new_digest_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    repo = ToolsmithRepository(store)
    gap = _gap()
    repo.create_escalation(gap)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO capability_registry(capability_id, version, digest, manifest_json, registered_at, active) "
            "VALUES (?, '1.0.0', 'sha256:old', '{}', ?, 1)",
            (gap.requested_capability, datetime.now(UTC).isoformat()),
        )
        conn.execute(
            "UPDATE capability_escalations SET state='registering', row_version=1 WHERE task_id=? AND requested_capability=?",
            (gap.task_id, gap.requested_capability),
        )
    manifest = CapabilityManifestV1(
        capability_id=gap.requested_capability,
        version="1.0.0",
        digest="sha256:new",
        entrypoint="nika_core.toolsmith.example:run",
        permissions=frozenset({"fs.read"}),
        source="nika-verified-candidate",
    )
    with pytest.raises(RuntimeError, match="version collision"):
        repo.register_exact(task_id=gap.task_id, manifest=manifest)
