from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.toolsmith import (
    AcceptanceCommand,
    AllowedPathPolicy,
    CandidateState,
    CapabilityEscalationService,
    CapabilityGap,
    ChangedFile,
    CodingJob,
    CodingResult,
    DeterministicCodingWorker,
    GapKind,
    IsolationClass,
    NetworkPolicy,
    ProcessPolicy,
    RecoveryState,
    RepositorySnapshot,
    ResourceBudget,
    ReuseCandidate,
    ReuseSearchPipeline,
    StaticReuseMetadataSource,
    TestEvidence,
    ToolsmithRepository,
    WorkspaceLease,
)


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    now = datetime.now(UTC).isoformat()
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO tasks(task_id, workspace_id, agent_id, state, payload_json, created_at, updated_at) "
            "VALUES ('task-1', 'software.factory', 'agent', 'paused', '{}', ?, ?)",
            (now, now),
        )
    return store


def _gap() -> CapabilityGap:
    return CapabilityGap(
        task_id="task-1",
        requested_capability="tool.example",
        kind=GapKind.MISSING_CAPABILITY,
        reason="missing",
        attempted_methods=("registry-search",),
        permission_ceiling=frozenset({"fs.read", "workspace.write", "tests.run"}),
    )


def _job(tmp_path: Path) -> CodingJob:
    return CodingJob(
        job_id="job-1",
        task_id="task-1",
        goal="bounded build",
        repository=RepositorySnapshot("repo", "0" * 40, "sha256:base"),
        lease=WorkspaceLease(
            "lease-1", tmp_path / "worker", IsolationClass.POLICY_ONLY, "2026-08-20T00:00:00+00:00"
        ),
        allowed_paths=AllowedPathPolicy(("src/nika_core/toolsmith",)),
        process_policy=ProcessPolicy(("python",)),
        network_policy=NetworkPolicy(),
        resource_budget=ResourceBudget(120, 100_000, 8),
        acceptance_commands=(AcceptanceCommand(("python", "-m", "pytest", "tests/test_toolsmith_kernel.py")),),
        permission_ceiling=frozenset({"fs.read", "workspace.write", "tests.run"}),
    )


def _result(job: CodingJob) -> CodingResult:
    return CodingResult(
        job_id=job.job_id,
        changed_files=(ChangedFile("src/nika_core/toolsmith/example.py", "a" * 64, 1),),
        test_evidence=(
            TestEvidence(
                ("python", "-m", "pytest", "tests/test_toolsmith_kernel.py"),
                0,
                "sha256:test",
            ),
        ),
    )


def test_reuse_search_uses_binding_order_even_if_sources_are_supplied_out_of_order() -> None:
    calls: list[str] = []

    class Source(StaticReuseMetadataSource):
        def search(self, capability_id: str) -> tuple[ReuseCandidate, ...]:
            calls.append(self.source_id)
            return super().search(capability_id)

    def candidate(source: str) -> ReuseCandidate:
        return ReuseCandidate(
            capability_id="tool.example",
            version="1.0.0",
            source=source,
            digest=f"sha256:{source}",
            permissions=frozenset({"fs.read"}),
        )

    pipeline = ReuseSearchPipeline(
        (
            Source("approved_catalog", (candidate("approved_catalog"),)),
            Source("mcp_metadata", (candidate("mcp_metadata"),)),
            Source("tool_registry", (candidate("tool_registry"),)),
            Source("installed_distributions", (candidate("installed_distributions"),)),
            Source("plugin_registry", (candidate("plugin_registry"),)),
            Source("workspace_capabilities", (candidate("workspace_capabilities"),)),
        )
    )
    result = pipeline.search(_gap())
    assert calls == [
        "tool_registry",
        "plugin_registry",
        "mcp_metadata",
        "workspace_capabilities",
        "installed_distributions",
        "approved_catalog",
    ]
    assert result.attempted_sources == tuple(calls)
    assert tuple(item.source for item in result.candidates) == tuple(calls)


def test_reuse_search_filters_permission_widening_before_selection() -> None:
    widened = ReuseCandidate(
        capability_id="tool.example",
        version="9.9.9",
        source="tool_registry",
        digest="sha256:widened",
        permissions=frozenset({"fs.read", "network.any"}),
    )
    pipeline = ReuseSearchPipeline((StaticReuseMetadataSource("tool_registry", (widened,)),))
    assert pipeline.search(_gap()).candidates == ()


def test_building_restart_uses_worker_recovery_without_second_build_transition(tmp_path: Path) -> None:
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
    version = repo.transition(
        task_id=gap.task_id,
        capability_id=gap.requested_capability,
        expected_version=version,
        target=CandidateState.BUILDING,
    )
    worker = DeterministicCodingWorker(_result)
    worker.recovery["job-1"] = RecoveryState("running", "opaque-worker-token")
    service = CapabilityEscalationService(
        repository=repo,
        checkpoints=CheckpointService(store),
        worker=worker,
    )
    next_version, result = asyncio.run(
        service.recover_build(gap=gap, job=_job(tmp_path), expected_version=version)
    )
    assert result.succeeded
    assert next_version == 3
    row = repo.get_escalation(task_id=gap.task_id, capability_id=gap.requested_capability)
    assert row is not None
    assert row["state"] == CandidateState.BUILT.value
    assert row["row_version"] == 3
    assert worker.executions == ["job-1"]


def test_building_restart_without_recovery_checkpoint_blocks_instead_of_replaying(tmp_path: Path) -> None:
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
    version = repo.transition(
        task_id=gap.task_id,
        capability_id=gap.requested_capability,
        expected_version=version,
        target=CandidateState.BUILDING,
    )
    worker = DeterministicCodingWorker(_result)
    service = CapabilityEscalationService(
        repository=repo,
        checkpoints=CheckpointService(store),
        worker=worker,
    )
    next_version, _ = asyncio.run(
        service.recover_build(gap=gap, job=_job(tmp_path), expected_version=version)
    )
    assert next_version == 3
    assert worker.executions == []
    row = repo.get_escalation(task_id=gap.task_id, capability_id=gap.requested_capability)
    assert row is not None
    assert row["state"] == CandidateState.BLOCKED.value
    checkpoint = CheckpointService(store).latest(gap.task_id)
    assert checkpoint is not None
    assert checkpoint.stage == "capability_escalation_blocked"
