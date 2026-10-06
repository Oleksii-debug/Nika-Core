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
    ReuseSearchResult,
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
    result = pipeline.search(_gap())
    assert result.candidates == ()
    assert result.permission_rejected_count == 1


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


def _service_for_reuse(tmp_path: Path) -> tuple[SQLiteStore, ToolsmithRepository, CapabilityEscalationService]:
    store = _store(tmp_path)
    repository = ToolsmithRepository(store)
    service = CapabilityEscalationService(
        repository=repository,
        checkpoints=CheckpointService(store),
        worker=DeterministicCodingWorker(_result),
    )
    return store, repository, service


def test_service_build_requires_canonical_search_provenance(tmp_path: Path) -> None:
    _, repository, service = _service_for_reuse(tmp_path)
    gap = CapabilityGap(
        task_id="task-1",
        requested_capability="tool.example",
        kind=GapKind.MISSING_CAPABILITY,
        reason="missing",
        attempted_methods=(),
        permission_ceiling=frozenset({"fs.read"}),
    )
    version, state = service.begin(gap)
    assert state is CandidateState.PROPOSED

    version, selected = service.choose_reuse(
        gap=gap,
        expected_version=version,
        search_result=ReuseSearchResult(
            candidates=(),
            attempted_sources=("tool_registry",),
        ),
    )
    assert selected is None
    row = repository.get_escalation(task_id=gap.task_id, capability_id=gap.requested_capability)
    assert row is not None
    assert row["state"] == CandidateState.BUILD_REQUIRED.value
    assert int(row["row_version"]) == version


def test_service_blocks_permission_filtered_canonical_match(tmp_path: Path) -> None:
    store, repository, service = _service_for_reuse(tmp_path)
    gap = _gap()
    version, state = service.begin(gap)
    assert state is CandidateState.PROPOSED
    widened = ReuseCandidate(
        capability_id=gap.requested_capability,
        version="9.9.9",
        source="tool_registry",
        digest="sha256:widened",
        permissions=frozenset({"fs.read", "network.any"}),
    )
    result = ReuseSearchPipeline(
        (StaticReuseMetadataSource("tool_registry", (widened,)),)
    ).search(gap)

    version, selected = service.choose_reuse(
        gap=gap,
        expected_version=version,
        search_result=result,
    )
    assert selected is None
    row = repository.get_escalation(task_id=gap.task_id, capability_id=gap.requested_capability)
    assert row is not None
    assert row["state"] == CandidateState.BLOCKED.value
    assert int(row["row_version"]) == version
    checkpoint = CheckpointService(store).latest(gap.task_id)
    assert checkpoint is not None
    assert checkpoint.stage == "capability_escalation_blocked"
    with store.connection() as conn:
        stored = conn.execute(
            "SELECT COUNT(*) AS count FROM capability_search_candidates WHERE task_id = ?",
            (gap.task_id,),
        ).fetchone()
    assert stored is not None
    assert int(stored["count"]) == 0


def test_service_selects_only_canonical_compatible_candidate(tmp_path: Path) -> None:
    _, repository, service = _service_for_reuse(tmp_path)
    gap = _gap()
    version, state = service.begin(gap)
    assert state is CandidateState.PROPOSED
    candidate = ReuseCandidate(
        capability_id=gap.requested_capability,
        version="1.0.0",
        source="tool_registry",
        digest="sha256:compatible",
        permissions=frozenset({"fs.read"}),
    )
    result = ReuseSearchPipeline(
        (StaticReuseMetadataSource("tool_registry", (candidate,)),)
    ).search(gap)

    version, selected = service.choose_reuse(
        gap=gap,
        expected_version=version,
        search_result=result,
    )
    assert selected == candidate
    row = repository.get_escalation(task_id=gap.task_id, capability_id=gap.requested_capability)
    assert row is not None
    assert row["state"] == CandidateState.REUSE_SELECTED.value
    assert int(row["row_version"]) == version


def test_legacy_candidate_iterable_cannot_authorize_build(tmp_path: Path) -> None:
    store, repository, service = _service_for_reuse(tmp_path)
    gap = _gap()
    version, state = service.begin(gap)
    assert state is CandidateState.PROPOSED

    version, selected = service.choose_reuse(
        gap=gap,
        expected_version=version,
        candidates=(),
    )
    assert selected is None
    row = repository.get_escalation(task_id=gap.task_id, capability_id=gap.requested_capability)
    assert row is not None
    assert row["state"] == CandidateState.BLOCKED.value
    assert int(row["row_version"]) == version
    checkpoint = CheckpointService(store).latest(gap.task_id)
    assert checkpoint is not None
    assert "canonical reuse search result" in str(checkpoint.payload["reason"])


def test_reuse_source_capability_identity_mismatch_fails_search() -> None:
    class MismatchedSource(StaticReuseMetadataSource):
        def search(self, capability_id: str) -> tuple[ReuseCandidate, ...]:
            return (
                ReuseCandidate(
                    capability_id="tool.other",
                    version="1.0.0",
                    source=self.source_id,
                    digest="sha256:foreign",
                    permissions=frozenset({"fs.read"}),
                ),
            )

    pipeline = ReuseSearchPipeline(
        (MismatchedSource("tool_registry", ()),)
    )
    try:
        pipeline.search(_gap())
    except ValueError as exc:
        assert "mismatched capability identity" in str(exc)
    else:
        raise AssertionError("mismatched capability identity must fail closed")
