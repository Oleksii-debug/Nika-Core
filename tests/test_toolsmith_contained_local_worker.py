from __future__ import annotations

import asyncio
import dataclasses
import pathlib
import shutil
import subprocess
import sys

import pytest

from nika_core.toolsmith.contracts import (
    AcceptanceCommand,
    AllowedPathPolicy,
    CodingJob,
    IsolationClass,
    NetworkPolicy,
    ProcessPolicy,
    RepositorySnapshot,
    ResourceBudget,
    WorkerFailureKind,
    WorkspaceLease,
)
from nika_core.toolsmith.local_worker import (
    ContainedLocalCodingWorker,
    ContainedLocalWorkerError,
    LocalCodingPlan,
    LocalFileEdit,
)

PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _git(root: pathlib.Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def _repository(tmp_path: pathlib.Path) -> tuple[pathlib.Path, str]:
    if shutil.which("git") is None:
        pytest.skip("Git CLI unavailable")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Nika Test")
    _git(root, "config", "user.email", "nika@example.invalid")
    source = root / "src"
    source.mkdir()
    (source / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "src/value.py")
    _git(root, "commit", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


class _Planner:
    def __init__(self, *edits: LocalFileEdit) -> None:
        self.edits = edits
        self.calls = 0

    async def plan(self, _job: CodingJob) -> LocalCodingPlan:
        self.calls += 1
        return LocalCodingPlan(tuple(self.edits))


class _MustNotPlan:
    async def plan(self, _job: CodingJob) -> LocalCodingPlan:
        raise AssertionError("terminal recovery must not call the planner")


def _worker(
    tmp_path: pathlib.Path,
    repository: pathlib.Path,
    planner,
) -> ContainedLocalCodingWorker:
    workspace_parent = tmp_path / "jobs"
    workspace_parent.mkdir(exist_ok=True)
    return ContainedLocalCodingWorker(
        workspace_parent=workspace_parent,
        repositories={"repo-1": repository},
        planner=planner,
    )


def _job(
    worker: ContainedLocalCodingWorker,
    base_sha: str,
    *,
    acceptance: tuple[AcceptanceCommand, ...] | None = None,
    tree_digest: str | None = None,
    job_id: str = "work-1",
) -> CodingJob:
    python = str(pathlib.Path(sys.executable).resolve(strict=True))
    return CodingJob(
        job_id=job_id,
        task_id="product:p1:component:core",
        goal="Update the implementation and prove it with tests.",
        repository=RepositorySnapshot(
            repository_id="repo-1",
            base_sha=base_sha,
            tree_digest=(
                tree_digest
                if tree_digest is not None
                else worker.repository_tree_digest("repo-1", base_sha)
            ),
        ),
        lease=WorkspaceLease(
            lease_id=f"lease:{job_id}",
            workspace_root=worker.workspace_root_for(job_id),
            isolation_class=(
                IsolationClass.PROCESS_CONTAINED
                if sys.platform == "win32"
                else IsolationClass.POLICY_ONLY
            ),
            expires_at="2099-01-01T00:00:00+00:00",
        ),
        allowed_paths=AllowedPathPolicy(("src",)),
        process_policy=ProcessPolicy((python,)),
        network_policy=NetworkPolicy(),
        resource_budget=ResourceBudget(
            timeout_seconds=20,
            max_output_bytes=1024 * 1024,
            max_changed_files=10,
        ),
        acceptance_commands=(
            acceptance
            if acceptance is not None
            else (
                AcceptanceCommand(
                    (
                        python,
                        "-c",
                        "from pathlib import Path; "
                        "assert Path('src/value.py').read_text() == 'VALUE = 2\\n'",
                    )
                ),
            )
        ),
        permission_ceiling=PERMISSIONS,
    )


def _run(coroutine):
    return asyncio.run(coroutine)


def test_worker_creates_private_candidate_commit_and_preserves_production_repo(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    planner = _Planner(LocalFileEdit("src/value.py", b"VALUE = 2\n"))
    worker = _worker(tmp_path, repository, planner)
    job = _job(worker, base_sha)

    result = _run(worker.execute(job))

    assert result.failure is None
    assert [item.path for item in result.changed_files] == ["src/value.py"]
    assert result.test_evidence[0].exit_code == 0
    assert (repository / "src" / "value.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    candidate = worker.candidate_worktree(job.job_id)
    assert (candidate / "src" / "value.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    evidence = worker.execution_evidence(job.job_id)
    assert evidence.base_sha == base_sha
    assert evidence.result_sha != base_sha
    assert len(evidence.diff_digest) == 64
    assert planner.calls == 1


def test_acceptance_side_effects_are_isolated_from_preserved_candidate(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    planner = _Planner(LocalFileEdit("src/value.py", b"VALUE = 2\n"))
    worker = _worker(tmp_path, repository, planner)
    python = str(pathlib.Path(sys.executable).resolve(strict=True))
    command = AcceptanceCommand(
        (
            python,
            "-c",
            "from pathlib import Path; Path('src/acceptance-only.txt').write_text('x')",
        )
    )
    job = _job(worker, base_sha, acceptance=(command,))

    result = _run(worker.execute(job))

    assert result.failure is None
    candidate = worker.candidate_worktree(job.job_id)
    assert not (candidate / "src" / "acceptance-only.txt").exists()
    assert [item.path for item in result.changed_files] == ["src/value.py"]


def test_failed_acceptance_preserves_exact_candidate_for_repair(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    planner = _Planner(LocalFileEdit("src/value.py", b"VALUE = 9\n"))
    worker = _worker(tmp_path, repository, planner)
    python = str(pathlib.Path(sys.executable).resolve(strict=True))
    job = _job(
        worker,
        base_sha,
        acceptance=(AcceptanceCommand((python, "-c", "raise SystemExit(7)")),),
    )

    result = _run(worker.execute(job))

    assert result.failure is not None
    assert result.failure.kind is WorkerFailureKind.PROCESS_FAILED
    assert result.failure.retryable is True
    evidence = worker.execution_evidence(job.job_id)
    assert evidence.result_sha != base_sha
    assert worker.candidate_worktree(job.job_id).exists()
    assert result.recovery_state is not None
    assert result.recovery_state.phase == "candidate_preserved"


def test_out_of_scope_plan_fails_before_private_candidate_mutation(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    planner = _Planner(LocalFileEdit("docs/outside.md", b"no\n"))
    worker = _worker(tmp_path, repository, planner)
    job = _job(worker, base_sha)

    result = _run(worker.execute(job))

    assert result.failure is not None
    assert result.failure.kind is WorkerFailureKind.POLICY_VIOLATION
    evidence = worker.execution_evidence(job.job_id)
    assert evidence.result_sha == base_sha
    assert (repository / "src" / "value.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    with pytest.raises(ContainedLocalWorkerError, match="no candidate worktree"):
        worker.candidate_worktree(job.job_id)


def test_stale_tree_identity_returns_invalid_request_and_base_evidence(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    planner = _Planner(LocalFileEdit("src/value.py", b"VALUE = 2\n"))
    worker = _worker(tmp_path, repository, planner)
    job = _job(worker, base_sha, tree_digest="f" * 40)

    result = _run(worker.execute(job))

    assert result.failure is not None
    assert result.failure.kind is WorkerFailureKind.INVALID_REQUEST
    assert planner.calls == 0
    evidence = worker.execution_evidence(job.job_id)
    assert evidence.result_sha == base_sha


def test_terminal_result_survives_reconstruction_without_replanning(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    planner = _Planner(LocalFileEdit("src/value.py", b"VALUE = 2\n"))
    worker = _worker(tmp_path, repository, planner)
    job = _job(worker, base_sha)

    first = _run(worker.execute(job))
    assert first.failure is None

    reconstructed = ContainedLocalCodingWorker(
        workspace_parent=worker.workspace_parent,
        repositories={"repo-1": repository},
        planner=_MustNotPlan(),
    )
    state = _run(reconstructed.inspect(job.job_id))
    assert state is not None
    assert state.phase == "terminal"

    recovered = _run(reconstructed.recover(job, state))
    replayed = _run(reconstructed.execute(job))

    assert recovered == first
    assert replayed == first
    assert reconstructed.execution_evidence(job.job_id) == worker.execution_evidence(
        job.job_id
    )


def test_terminal_result_survives_lease_expiry_renewal_without_replay(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    planner = _Planner(LocalFileEdit("src/value.py", b"VALUE = 2\n"))
    worker = _worker(tmp_path, repository, planner)
    job = _job(worker, base_sha)

    first = _run(worker.execute(job))
    assert first.failure is None
    renewed = dataclasses.replace(
        job,
        lease=WorkspaceLease(
            lease_id=job.lease.lease_id,
            workspace_root=job.lease.workspace_root,
            isolation_class=job.lease.isolation_class,
            expires_at="2100-01-01T00:00:00+00:00",
        ),
    )
    reconstructed = ContainedLocalCodingWorker(
        workspace_parent=worker.workspace_parent,
        repositories={"repo-1": repository},
        planner=_MustNotPlan(),
    )

    replayed = _run(reconstructed.execute(renewed))

    assert replayed == first
    assert reconstructed.execution_evidence(job.job_id).result_sha != base_sha


def test_cancel_during_execution_produces_nonretryable_cancelled_result(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    planner = _Planner(LocalFileEdit("src/value.py", b"VALUE = 2\n"))
    worker = _worker(tmp_path, repository, planner)
    python = str(pathlib.Path(sys.executable).resolve(strict=True))
    job = _job(
        worker,
        base_sha,
        acceptance=(
            AcceptanceCommand(
                (python, "-c", "import time; time.sleep(30)"),
                timeout_seconds=15,
            ),
        ),
    )

    async def scenario():
        task = asyncio.create_task(worker.execute(job))
        for _ in range(100):
            if worker.is_active(job.job_id):
                break
            await asyncio.sleep(0.01)
        await worker.cancel(job.job_id)
        return await task

    result = _run(scenario())

    assert result.failure is not None
    assert result.failure.kind is WorkerFailureKind.CANCELLED
    assert result.failure.retryable is False


def test_corrupt_durable_state_fails_closed_to_manual_reconciliation(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    worker = _worker(
        tmp_path,
        repository,
        _Planner(LocalFileEdit("src/value.py", b"VALUE = 2\n")),
    )
    job = _job(worker, base_sha)
    result = _run(worker.execute(job))
    assert result.failure is None

    state_path = worker.workspace_root_for(job.job_id) / "_nika_local_worker_state.json"
    state_path.write_text('{"schema":"x","schema":"y"}', encoding="utf-8")

    state = _run(worker.inspect(job.job_id))
    assert state is not None
    assert state.phase == "manual_reconcile_required"
    recovered = _run(worker.recover(job, state))
    assert recovered.failure is not None
    assert recovered.failure.kind is WorkerFailureKind.INTERNAL_ERROR
    assert recovered.failure.retryable is False


@pytest.mark.parametrize(
    "corrupt_state",
    [
        b'{"nested":' + b"[" * 65 + b"0" + b"]" * 65 + b"}",
        b'{"value":1e400}',
        b'{"value":' + b"9" * 1235 + b"}",
    ],
)
def test_invalid_resource_bound_state_requires_manual_reconciliation(
    tmp_path: pathlib.Path,
    corrupt_state: bytes,
) -> None:
    repository, base_sha = _repository(tmp_path)
    worker = _worker(
        tmp_path,
        repository,
        _Planner(LocalFileEdit("src/value.py", b"VALUE = 2\n")),
    )
    job = _job(worker, base_sha)
    first = _run(worker.execute(job))
    assert first.failure is None

    state_path = worker.workspace_root_for(job.job_id) / "_nika_local_worker_state.json"
    state_path.write_bytes(corrupt_state)

    reconstructed = ContainedLocalCodingWorker(
        workspace_parent=worker.workspace_parent,
        repositories={"repo-1": repository},
        planner=_MustNotPlan(),
    )
    inspected = _run(reconstructed.inspect(job.job_id))
    replayed = _run(reconstructed.execute(job))

    assert inspected is not None
    assert inspected.phase == "manual_reconcile_required"
    assert replayed.failure is not None
    assert replayed.failure.kind is WorkerFailureKind.INTERNAL_ERROR
    assert replayed.failure.retryable is False
    assert replayed.recovery_state is not None
    assert replayed.recovery_state.phase == "manual_reconcile_required"


def test_worker_configuration_mappings_are_detached_and_read_only(
    tmp_path: pathlib.Path,
) -> None:
    repository, _base_sha = _repository(tmp_path)
    repositories = {"repo-1": repository}
    environment = {"SYSTEMROOT": "trusted-root"}
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    worker = ContainedLocalCodingWorker(
        workspace_parent=jobs,
        repositories=repositories,
        planner=_MustNotPlan(),
        source_environment=environment,
    )

    repositories["repo-1"] = tmp_path / "other"
    environment["SYSTEMROOT"] = "changed"

    assert worker.repositories["repo-1"] == repository.resolve(strict=True)
    assert worker.source_environment["SYSTEMROOT"] == "trusted-root"
    with pytest.raises(TypeError):
        worker.repositories["repo-2"] = repository  # type: ignore[index]
    with pytest.raises(TypeError):
        worker.source_environment["NEW_SECRET"] = "x"  # type: ignore[index]


def test_same_job_single_flight_precedes_planner_side_effect(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    entered: asyncio.Event
    release: asyncio.Event

    class BlockingPlanner:
        def __init__(self) -> None:
            self.calls = 0

        async def plan(self, _job: CodingJob) -> LocalCodingPlan:
            self.calls += 1
            entered.set()
            await release.wait()
            return LocalCodingPlan((LocalFileEdit("src/value.py", b"VALUE = 2\n"),))

    async def scenario():
        nonlocal entered, release
        entered = asyncio.Event()
        release = asyncio.Event()
        planner = BlockingPlanner()
        worker = _worker(tmp_path, repository, planner)
        job = _job(worker, base_sha)
        first_task = asyncio.create_task(worker.execute(job))
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert worker.is_active(job.job_id)
        second = await worker.execute(job)
        release.set()
        first = await first_task
        return planner, first, second

    planner, first, second = _run(scenario())

    assert planner.calls == 1
    assert first.failure is None
    assert second.failure is not None
    assert second.failure.kind is WorkerFailureKind.INVALID_REQUEST
    assert second.failure.retryable is False


def test_same_job_single_flight_spans_distinct_worker_instances(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    entered: asyncio.Event
    release: asyncio.Event

    class BlockingPlanner:
        async def plan(self, _job: CodingJob) -> LocalCodingPlan:
            entered.set()
            await release.wait()
            return LocalCodingPlan((LocalFileEdit("src/value.py", b"VALUE = 2\n"),))

    async def scenario():
        nonlocal entered, release
        entered = asyncio.Event()
        release = asyncio.Event()
        first_worker = _worker(tmp_path, repository, BlockingPlanner())
        second_worker = _worker(tmp_path, repository, _MustNotPlan())
        first_job = _job(first_worker, base_sha)
        second_job = _job(second_worker, base_sha)
        first_task = asyncio.create_task(first_worker.execute(first_job))
        await asyncio.wait_for(entered.wait(), timeout=2)
        second = await second_worker.execute(second_job)
        release.set()
        first = await first_task
        return first, second

    first, second = _run(scenario())

    assert first.failure is None
    assert second.failure is not None
    assert second.failure.kind is WorkerFailureKind.INVALID_REQUEST
    assert second.failure.retryable is False


def test_planner_mutation_cannot_rewrite_retained_job_authority(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)

    class MutatingPlanner:
        async def plan(self, planning_job: CodingJob) -> LocalCodingPlan:
            object.__setattr__(planning_job.resource_budget, "max_changed_files", 0)
            object.__setattr__(planning_job.allowed_paths, "roots", ("docs",))
            object.__setattr__(planning_job.repository, "tree_digest", "0" * 40)
            return LocalCodingPlan((LocalFileEdit("src/value.py", b"VALUE = 2\n"),))

    worker = _worker(tmp_path, repository, MutatingPlanner())
    job = _job(worker, base_sha)

    result = _run(worker.execute(job))

    assert result.failure is None
    assert [item.path for item in result.changed_files] == ["src/value.py"]
    evidence = worker.execution_evidence(job.job_id)
    assert evidence.base_sha == base_sha
    assert evidence.result_sha != base_sha


def test_terminal_replay_rejects_candidate_worktree_tamper(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    worker = _worker(
        tmp_path,
        repository,
        _Planner(LocalFileEdit("src/value.py", b"VALUE = 2\n")),
    )
    job = _job(worker, base_sha)
    first = _run(worker.execute(job))
    assert first.failure is None
    candidate = worker.candidate_worktree(job.job_id)
    (candidate / "src" / "value.py").write_text("VALUE = 99\n", encoding="utf-8")

    reconstructed = ContainedLocalCodingWorker(
        workspace_parent=worker.workspace_parent,
        repositories={"repo-1": repository},
        planner=_MustNotPlan(),
    )
    inspected = _run(reconstructed.inspect(job.job_id))
    replayed = _run(reconstructed.execute(job))

    assert inspected is not None
    assert inspected.phase == "manual_reconcile_required"
    assert replayed.failure is not None
    assert replayed.failure.kind is WorkerFailureKind.INTERNAL_ERROR
    assert replayed.failure.retryable is False
    assert replayed.recovery_state is not None
    assert replayed.recovery_state.phase == "manual_reconcile_required"
    with pytest.raises(ContainedLocalWorkerError, match="terminal execution evidence"):
        reconstructed.execution_evidence(job.job_id)


def test_terminal_recovery_rejects_missing_private_git_metadata(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    worker = _worker(
        tmp_path,
        repository,
        _Planner(LocalFileEdit("src/value.py", b"VALUE = 2\n")),
    )
    job = _job(worker, base_sha)
    first = _run(worker.execute(job))
    assert first.failure is None
    state = _run(worker.inspect(job.job_id))
    assert state is not None
    private_git = worker.workspace_root_for(job.job_id) / "_nika_private_git"
    shutil.rmtree(private_git)

    reconstructed = ContainedLocalCodingWorker(
        workspace_parent=worker.workspace_parent,
        repositories={"repo-1": repository},
        planner=_MustNotPlan(),
    )
    recovered = _run(reconstructed.recover(job, state))

    assert recovered.failure is not None
    assert recovered.failure.kind is WorkerFailureKind.INTERNAL_ERROR
    assert recovered.failure.retryable is False
    assert recovered.recovery_state is not None
    assert recovered.recovery_state.phase == "manual_reconcile_required"


def test_terminal_replay_rejects_result_identity_tamper(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    worker = _worker(
        tmp_path,
        repository,
        _Planner(LocalFileEdit("src/value.py", b"VALUE = 2\n")),
    )
    job = _job(worker, base_sha)
    first = _run(worker.execute(job))
    assert first.failure is None
    state_path = worker.workspace_root_for(job.job_id) / "_nika_local_worker_state.json"
    payload = __import__("json").loads(state_path.read_text(encoding="utf-8"))
    payload["result"]["job_id"] = "other-job"
    state_path.write_text(
        __import__("json").dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    reconstructed = ContainedLocalCodingWorker(
        workspace_parent=worker.workspace_parent,
        repositories={"repo-1": repository},
        planner=_MustNotPlan(),
    )
    replayed = _run(reconstructed.execute(job))

    assert replayed.failure is not None
    assert replayed.failure.kind is WorkerFailureKind.INTERNAL_ERROR
    assert replayed.failure.retryable is False
    assert replayed.recovery_state is not None
    assert replayed.recovery_state.phase == "manual_reconcile_required"


def test_post_init_tampered_resource_budget_is_rejected_before_planner(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    planner = _Planner(LocalFileEdit("src/value.py", b"VALUE = 2\n"))
    worker = _worker(tmp_path, repository, planner)
    job = _job(worker, base_sha)
    object.__setattr__(job.resource_budget, "timeout_seconds", True)

    result = _run(worker.execute(job))

    assert result.failure is not None
    assert result.failure.kind is WorkerFailureKind.INVALID_REQUEST
    assert planner.calls == 0


def test_terminal_replay_rejects_diff_digest_tamper(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    worker = _worker(
        tmp_path,
        repository,
        _Planner(LocalFileEdit("src/value.py", b"VALUE = 2\n")),
    )
    job = _job(worker, base_sha)
    first = _run(worker.execute(job))
    assert first.failure is None

    state_path = worker.workspace_root_for(job.job_id) / "_nika_local_worker_state.json"
    payload = __import__("json").loads(state_path.read_text(encoding="utf-8"))
    original = payload["evidence"]["diff_digest"]
    payload["evidence"]["diff_digest"] = "0" * 64 if original != "0" * 64 else "1" * 64
    state_path.write_text(
        __import__("json").dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    reconstructed = ContainedLocalCodingWorker(
        workspace_parent=worker.workspace_parent,
        repositories={"repo-1": repository},
        planner=_MustNotPlan(),
    )
    inspected = _run(reconstructed.inspect(job.job_id))
    replayed = _run(reconstructed.execute(job))

    assert inspected is not None
    assert inspected.phase == "manual_reconcile_required"
    assert replayed.failure is not None
    assert replayed.failure.kind is WorkerFailureKind.INTERNAL_ERROR
    assert replayed.failure.retryable is False
    assert replayed.recovery_state is not None
    assert replayed.recovery_state.phase == "manual_reconcile_required"
    with pytest.raises(ContainedLocalWorkerError, match="terminal execution evidence"):
        reconstructed.execution_evidence(job.job_id)


def test_no_candidate_replay_rejects_diff_digest_tamper(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    worker = _worker(
        tmp_path,
        repository,
        _Planner(LocalFileEdit("docs/outside.md", b"no\n")),
    )
    job = _job(worker, base_sha)
    first = _run(worker.execute(job))
    assert first.failure is not None
    assert first.failure.kind is WorkerFailureKind.POLICY_VIOLATION

    state_path = worker.workspace_root_for(job.job_id) / "_nika_local_worker_state.json"
    payload = __import__("json").loads(state_path.read_text(encoding="utf-8"))
    original = payload["evidence"]["diff_digest"]
    payload["evidence"]["diff_digest"] = "0" * 64 if original != "0" * 64 else "1" * 64
    state_path.write_text(
        __import__("json").dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    reconstructed = ContainedLocalCodingWorker(
        workspace_parent=worker.workspace_parent,
        repositories={"repo-1": repository},
        planner=_MustNotPlan(),
    )
    replayed = _run(reconstructed.execute(job))

    assert replayed.failure is not None
    assert replayed.failure.kind is WorkerFailureKind.INTERNAL_ERROR
    assert replayed.failure.retryable is False
    assert replayed.recovery_state is not None
    assert replayed.recovery_state.phase == "manual_reconcile_required"

