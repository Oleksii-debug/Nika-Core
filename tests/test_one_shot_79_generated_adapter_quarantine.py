from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
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
    GapKind,
    IsolationClass,
    NetworkPolicy,
    ProcessPolicy,
    RepositorySnapshot,
    ResourceBudget,
    TestEvidence,
    ToolsmithRepository,
    WorkspaceLease,
)
from nika_core.toolsmith.workspace_security import sterile_git_environment

CAPABILITY_ID = "provider.generated.quarantine-probe"
VERIFIED_DIGEST = "a" * 64
SUBSTITUTED_DIGEST = "b" * 64
PERMISSION_CEILING = frozenset({"fs.read", "workspace.write", "tests.run"})


def _setup(
    tmp_path: Path,
) -> tuple[str, SQLiteStore, ToolsmithRepository, CapabilityEscalationService]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="toolsmith.quarantine",
        agent_id="one-shot-79",
        payload={"capability_id": CAPABILITY_ID},
    )
    repository = ToolsmithRepository(store)

    def generated(job: CodingJob) -> CodingResult:
        return CodingResult(
            job_id=job.job_id,
            changed_files=(
                ChangedFile(
                    "src/nika_core/generated/provider_adapter.py",
                    VERIFIED_DIGEST,
                    128,
                ),
            ),
            test_evidence=(
                TestEvidence(
                    command=job.acceptance_commands[0].argv,
                    exit_code=0,
                    output_digest="sha256:generated-adapter-tests",
                ),
            ),
        )

    service = CapabilityEscalationService(
        repository=repository,
        checkpoints=CheckpointService(store),
        worker=DeterministicCodingWorker(generated),
    )
    return task.task_id, store, repository, service


def _gap(task_id: str) -> CapabilityGap:
    return CapabilityGap(
        task_id=task_id,
        requested_capability=CAPABILITY_ID,
        kind=GapKind.MISSING_CAPABILITY,
        reason="provider adapter is genuinely missing after canonical search",
        attempted_methods=(
            "tool-registry",
            "plugin-registry",
            "mcp-metadata",
            "workspace",
            "installed",
            "catalog",
        ),
        permission_ceiling=PERMISSION_CEILING,
    )


def _job(tmp_path: Path, task_id: str) -> CodingJob:
    return CodingJob(
        job_id="generated-adapter-job",
        task_id=task_id,
        goal="generate one bounded provider adapter candidate",
        repository=RepositorySnapshot(
            repository_id="Oleksii-debug/Nika-Core",
            base_sha="0" * 40,
            tree_digest="sha256:base",
        ),
        lease=WorkspaceLease(
            lease_id="generated-adapter-lease",
            workspace_root=tmp_path / "candidate-workspace",
            isolation_class=IsolationClass.POLICY_ONLY,
            expires_at="2026-09-11T00:00:00+00:00",
        ),
        allowed_paths=AllowedPathPolicy(("src/nika_core/generated",)),
        process_policy=ProcessPolicy(("python",)),
        network_policy=NetworkPolicy(),
        resource_budget=ResourceBudget(
            timeout_seconds=30,
            max_output_bytes=50_000,
            max_changed_files=2,
        ),
        acceptance_commands=(
            AcceptanceCommand(
                (
                    "python",
                    "-m",
                    "pytest",
                    "tests/test_one_shot_79_generated_adapter_quarantine.py",
                )
            ),
        ),
        permission_ceiling=PERMISSION_CEILING,
    )


def _build_candidate(
    tmp_path: Path,
) -> tuple[
    str,
    SQLiteStore,
    ToolsmithRepository,
    CapabilityEscalationService,
    CapabilityGap,
    int,
]:
    task_id, store, repository, service = _setup(tmp_path)
    gap = _gap(task_id)
    version, state = service.begin(gap)
    assert state is CandidateState.PROPOSED
    version, selected = service.choose_reuse(
        gap=gap,
        candidates=(),
        expected_version=version,
    )
    assert selected is None
    version, result = asyncio.run(
        service.build(
            gap=gap,
            job=_job(tmp_path, task_id),
            expected_version=version,
        )
    )
    assert result.succeeded
    assert repository.get_escalation(
        task_id=task_id,
        capability_id=CAPABILITY_ID,
    )["state"] == CandidateState.BUILT.value
    return task_id, store, repository, service, gap, version


def _active_registry_count(store: SQLiteStore) -> int:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM capability_registry WHERE capability_id = ? AND active = 1",
            (CAPABILITY_ID,),
        ).fetchone()
    assert row is not None
    return int(row["count"])


def _manifest(
    digest: str,
    *,
    permissions: frozenset[str] = PERMISSION_CEILING,
) -> CapabilityManifestV1:
    return CapabilityManifestV1(
        capability_id=CAPABILITY_ID,
        version="1.0.0",
        digest=digest,
        entrypoint="nika_generated_quarantine_probe:run",
        permissions=permissions,
        source="local://toolsmith/generated-candidate",
    )


def test_generation_success_never_publishes_or_resumes_candidate(tmp_path: Path) -> None:
    task_id, store, _, service, gap, version = _build_candidate(tmp_path)

    assert _active_registry_count(store) == 0
    assert service.reconcile_resume(task_id=task_id, capability_id=CAPABILITY_ID) is None

    version = service.start_verification(gap=gap, expected_version=version)
    assert _active_registry_count(store) == 0
    assert service.reconcile_resume(task_id=task_id, capability_id=CAPABILITY_ID) is None

    service.accept_verification(
        gap=gap,
        expected_version=version,
        candidate_digest=VERIFIED_DIGEST,
        verifier_evidence={"review_ref": "review://independent/generated-adapter"},
    )
    assert _active_registry_count(store) == 0
    assert service.reconcile_resume(task_id=task_id, capability_id=CAPABILITY_ID) is None


def test_post_review_adapter_substitution_cannot_publish_or_resume(tmp_path: Path) -> None:
    task_id, store, repository, service, gap, version = _build_candidate(tmp_path)
    version = service.start_verification(gap=gap, expected_version=version)
    version = service.accept_verification(
        gap=gap,
        expected_version=version,
        candidate_digest=VERIFIED_DIGEST,
        verifier_evidence={"review_ref": "review://independent/generated-adapter"},
    )

    with pytest.raises(ValueError, match="independently verified candidate"):
        service.register(
            gap=gap,
            expected_version=version,
            manifest=_manifest(SUBSTITUTED_DIGEST),
        )

    row = repository.get_escalation(task_id=task_id, capability_id=CAPABILITY_ID)
    assert row is not None
    assert row["state"] == CandidateState.VERIFIED.value
    assert row["pinned_digest"] == VERIFIED_DIGEST
    assert _active_registry_count(store) == 0
    assert service.reconcile_resume(task_id=task_id, capability_id=CAPABILITY_ID) is None


def test_generated_adapter_cannot_widen_original_permission_ceiling(tmp_path: Path) -> None:
    task_id, store, _, service, gap, version = _build_candidate(tmp_path)
    version = service.start_verification(gap=gap, expected_version=version)
    version = service.accept_verification(
        gap=gap,
        expected_version=version,
        candidate_digest=VERIFIED_DIGEST,
        verifier_evidence={"review_ref": "review://independent/generated-adapter"},
    )

    with pytest.raises(PermissionError, match="permissions exceed original task ceiling"):
        service.register(
            gap=gap,
            expected_version=version,
            manifest=_manifest(
                VERIFIED_DIGEST,
                permissions=PERMISSION_CEILING | {"credentials.read"},
            ),
        )

    assert _active_registry_count(store) == 0
    assert service.reconcile_resume(task_id=task_id, capability_id=CAPABILITY_ID) is None


def test_generated_adapter_workspace_has_no_ambient_credentials_by_default() -> None:
    environment = sterile_git_environment(
        {
            "PATH": "safe-path",
            "TEMP": "safe-temp",
            "GITHUB_TOKEN": "github-secret",
            "OPENAI_API_KEY": "openai-secret",
            "AWS_ACCESS_KEY_ID": "aws-id",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "AZURE_CLIENT_SECRET": "azure-secret",
            "GOOGLE_APPLICATION_CREDENTIALS": "credential-file.json",
            "SSH_AUTH_SOCK": "ssh-agent",
        }
    )

    assert environment["PATH"] == "safe-path"
    assert environment["TEMP"] == "safe-temp"
    for name in (
        "GITHUB_TOKEN",
        "OPENAI_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AZURE_CLIENT_SECRET",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "SSH_AUTH_SOCK",
    ):
        assert name not in environment
