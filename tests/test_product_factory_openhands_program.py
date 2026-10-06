from __future__ import annotations

import asyncio
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_coding_worker_adapter import CodingWorkerComponentAdapter
from nika_core.product_factory_coordinator import ComponentWorkRequest
from nika_core.product_factory_openhands_program import (
    OpenHandsProductFactoryError,
    OpenHandsProductFactoryPolicy,
    build_openhands_product_factory_program,
)
from nika_core.toolsmith.contracts import (
    AcceptanceCommand,
    AllowedPathPolicy,
    ChangedFile,
    CodingJob,
    CodingResult,
    IsolationClass,
    NetworkMode,
    RepositorySnapshot,
    ResourceBudget,
    TestEvidence,
)
from nika_core.toolsmith.openhands_remote_worker import (
    OpenHandsRemoteCodingWorker,
    OpenHandsRunEvidence,
    OpenHandsSandboxEndpoint,
    RemoteFile,
    SandboxedAcceptanceEvidence,
)

PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _run(coroutine):
    return asyncio.run(coroutine)


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
    root = tmp_path / "trusted repository"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Nika Test")
    _git(root, "config", "user.email", "nika@example.invalid")
    (root / "src").mkdir()
    (root / "src" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "src/core.py")
    _git(root, "commit", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


class _SandboxProvider:
    async def acquire(self, job):
        raise AssertionError(f"composition test must not acquire a sandbox: {job.job_id}")

    async def release(self, job, endpoint, *, succeeded):
        raise AssertionError((job.job_id, endpoint.endpoint_id, succeeded))


class _AcceptanceRuntime:
    async def execute(self, job, candidate_files, candidate_evidence):
        raise AssertionError((job.job_id, candidate_files, candidate_evidence))

    async def cancel(self, job_id):
        raise AssertionError(job_id)


def _policy() -> OpenHandsProductFactoryPolicy:
    python = str(pathlib.Path(sys.executable).resolve(strict=True))
    return OpenHandsProductFactoryPolicy(
        allowed_executables=(python,),
        approved_hosts=("127.0.0.1", "localhost"),
        resource_budget=ResourceBudget(30, 1024 * 1024, 20),
        lease_seconds=300,
    )


def _request(base_sha: str, *, work_id: str = "work-openhands-1") -> ComponentWorkRequest:
    python = str(pathlib.Path(sys.executable).resolve(strict=True))
    return ComponentWorkRequest(
        work_id=work_id,
        project_id="project-1",
        component_id="core",
        repository_id="repo-1",
        goal="update core without publishing",
        base_sha=base_sha,
        allowed_paths=("src",),
        permission_ceiling=PERMISSIONS,
        acceptance_commands=((python, "-c", "print('ok')"),),
    )


def _job(request: ComponentWorkRequest, context) -> CodingJob:
    return CodingJob(
        job_id=request.work_id,
        task_id=f"product:{request.project_id}:component:{request.component_id}",
        goal=request.goal,
        repository=RepositorySnapshot(
            request.repository_id,
            request.base_sha,
            context.repository_tree_digest,
        ),
        lease=context.lease,
        allowed_paths=AllowedPathPolicy(request.allowed_paths),
        process_policy=context.process_policy,
        network_policy=context.network_policy,
        resource_budget=context.resource_budget,
        acceptance_commands=tuple(
            AcceptanceCommand(argv=command) for command in request.acceptance_commands
        ),
        permission_ceiling=request.permission_ceiling,
    )


def _program(
    tmp_path: pathlib.Path,
    repository: pathlib.Path,
):
    jobs = tmp_path / "OpenHands jobs"
    jobs.mkdir()
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()

    def _never_client(_endpoint):
        raise AssertionError("composition test must not redeem client authentication")

    program = build_openhands_product_factory_program(
        store,
        workspace_parent=jobs,
        repositories={"repo-1": repository},
        sandbox_provider=_SandboxProvider(),
        client_factory=_never_client,
        agent_profile_id_factory=lambda _job, _endpoint: (
            "11111111-1111-4111-8111-111111111111"
        ),
        acceptance_runtime=_AcceptanceRuntime(),
        policy=_policy(),
    )
    return store, program


def _private_git(program, work_id: str, *args: str) -> str:
    root = program.ports.workspace_root_for(work_id)
    return _git(root, "--git-dir", str(root / "_nika_private_git"), *args)


def test_builder_composes_one_worker_host_recovery_ledger_and_explicit_policy(
    tmp_path: pathlib.Path,
) -> None:
    repository, _base_sha = _repository(tmp_path)
    store, program = _program(tmp_path, repository)

    assert program.host.store is store
    assert isinstance(program.host.worker, CodingWorkerComponentAdapter)
    assert program.host.worker.worker is program.worker
    assert program.host.worker.contexts is program.ports
    assert program.host.worker.evidence is program.ports
    assert program.recovery.ledger is program.host.idempotency
    assert program.runtime._recovery_binding_store is program.recovery
    assert program.worker._recovery_probe is program.recovery
    assert program.worker._recovery_binding_store is program.recovery
    assert program.worker._acceptance_runtime is not None


def test_context_prepares_exact_private_base_without_git_remote_or_visible_metadata(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    _store, program = _program(tmp_path, repository)
    request = _request(base_sha)

    context = _run(program.ports.context_for(request))

    worktree = context.lease.workspace_root
    assert worktree == program.ports.workspace_root_for(request.work_id) / "worktree"
    assert not (worktree / ".git").exists()
    assert _private_git(program, request.work_id, "remote") == ""
    assert _private_git(program, request.work_id, "rev-parse", "HEAD") == base_sha
    assert context.network_policy.mode is NetworkMode.APPROVED_HOSTS
    assert context.network_policy.approved_hosts == ("127.0.0.1", "localhost")
    assert len(context.repository_tree_digest) == 64
    assert (repository / "src" / "core.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_context_rebuilds_stale_local_staging_from_exact_trusted_base(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    _store, program = _program(tmp_path, repository)
    request = _request(base_sha, work_id="work-restart-base")

    first = _run(program.ports.context_for(request))
    (first.lease.workspace_root / "src" / "core.py").write_text(
        "STALE = True\n",
        encoding="utf-8",
    )

    second = _run(program.ports.context_for(request))

    assert second.repository_tree_digest == first.repository_tree_digest
    assert (second.lease.workspace_root / "src" / "core.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"
    assert _private_git(program, request.work_id, "remote") == ""
    assert _private_git(program, request.work_id, "rev-parse", "HEAD") == base_sha


def test_collect_commits_exact_validated_candidate_only_in_private_git(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    _store, program = _program(tmp_path, repository)
    request = _request(base_sha, work_id="work-private-candidate")
    context = _run(program.ports.context_for(request))
    job = _job(request, context)

    candidate = context.lease.workspace_root / "src" / "core.py"
    candidate.write_text("VALUE = 2\n", encoding="utf-8")
    data = candidate.read_bytes()
    result = CodingResult(
        job_id=request.work_id,
        changed_files=(
            ChangedFile(
                "src/core.py",
                hashlib.sha256(data).hexdigest(),
                len(data),
            ),
        ),
    )

    evidence = _run(program.ports.collect(request, job, result))

    assert evidence.base_sha == base_sha
    assert evidence.result_sha != base_sha
    assert len(evidence.result_sha) == 40
    assert len(evidence.diff_digest) == 64
    assert _private_git(program, request.work_id, "rev-parse", "HEAD^1") == base_sha
    assert _private_git(program, request.work_id, "remote") == ""
    assert _private_git(
        program,
        request.work_id,
        "diff",
        "--name-only",
        "--no-renames",
        base_sha,
        evidence.result_sha,
    ) == "src/core.py"
    assert (repository / "src" / "core.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert program.ports.candidate_worktree(request.work_id).joinpath(
        "src", "core.py"
    ).read_text(encoding="utf-8") == "VALUE = 2\n"


def test_collect_rejects_unreported_private_tree_change_before_commit(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    _store, program = _program(tmp_path, repository)
    request = _request(base_sha, work_id="work-unreported-change")
    context = _run(program.ports.context_for(request))
    job = _job(request, context)

    core = context.lease.workspace_root / "src" / "core.py"
    core.write_text("VALUE = 2\n", encoding="utf-8")
    extra = context.lease.workspace_root / "src" / "extra.py"
    extra.write_text("EXTRA = True\n", encoding="utf-8")
    data = core.read_bytes()
    result = CodingResult(
        job_id=request.work_id,
        changed_files=(
            ChangedFile(
                "src/core.py",
                hashlib.sha256(data).hexdigest(),
                len(data),
            ),
        ),
    )

    with pytest.raises(
        OpenHandsProductFactoryError,
        match="changed-file evidence differs",
    ):
        _run(program.ports.collect(request, job, result))

    assert _private_git(program, request.work_id, "rev-parse", "HEAD") == base_sha
    assert (repository / "src" / "core.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_context_rejects_repository_not_explicitly_authorized(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    _store, program = _program(tmp_path, repository)
    request = ComponentWorkRequest(
        work_id="work-unauthorized-repo",
        project_id="project-1",
        component_id="core",
        repository_id="repo-2",
        goal="must fail closed",
        base_sha=base_sha,
        allowed_paths=("src",),
        permission_ceiling=PERMISSIONS,
        acceptance_commands=(),
    )

    with pytest.raises(
        OpenHandsProductFactoryError,
        match="not explicitly authorized",
    ):
        _run(program.ports.context_for(request))


def test_policy_rejects_url_hosts_noncanonical_executables_and_workspace_overlap(
    tmp_path: pathlib.Path,
) -> None:
    python = str(pathlib.Path(sys.executable).resolve(strict=True))
    budget = ResourceBudget(30, 1024 * 1024, 20)

    with pytest.raises(ValueError, match="bare host name"):
        OpenHandsProductFactoryPolicy(
            allowed_executables=(python,),
            approved_hosts=("https://agent.example",),
            resource_budget=budget,
        )
    remote_policy = OpenHandsProductFactoryPolicy(
        allowed_executables=("python",),
        approved_hosts=("localhost",),
        resource_budget=budget,
    )
    assert remote_policy.allowed_executables == ("python",)

    with pytest.raises(ValueError, match="canonical text"):
        OpenHandsProductFactoryPolicy(
            allowed_executables=(" python",),
            approved_hosts=("localhost",),
            resource_budget=budget,
        )

    repository, _base_sha = _repository(tmp_path)
    store = SQLiteStore(tmp_path / "overlap.db")
    store.initialize()
    with pytest.raises(OpenHandsProductFactoryError, match="must be disjoint"):
        build_openhands_product_factory_program(
            store,
            workspace_parent=repository,
            repositories={"repo-1": repository},
            sandbox_provider=_SandboxProvider(),
            client_factory=lambda _endpoint: None,
            agent_profile_id_factory=lambda _job, _endpoint: (
                "11111111-1111-4111-8111-111111111111"
            ),
            acceptance_runtime=_AcceptanceRuntime(),
            policy=_policy(),
        )


def test_source_environment_is_sanitized_before_private_git_use(
    tmp_path: pathlib.Path,
) -> None:
    repository, _base_sha = _repository(tmp_path)
    jobs = tmp_path / "jobs sanitized"
    jobs.mkdir()
    store = SQLiteStore(tmp_path / "sanitized.db")
    store.initialize()
    source_environment = {
        "PATH": os.environ.get("PATH", ""),
        "GITHUB_TOKEN": "must-not-survive",
        "NIKA_TEST_SECRET": "must-not-survive",
    }

    program = build_openhands_product_factory_program(
        store,
        workspace_parent=jobs,
        repositories={"repo-1": repository},
        sandbox_provider=_SandboxProvider(),
        client_factory=lambda _endpoint: None,
        agent_profile_id_factory=lambda _job, _endpoint: (
            "11111111-1111-4111-8111-111111111111"
        ),
        acceptance_runtime=_AcceptanceRuntime(),
        policy=_policy(),
        source_environment=source_environment,
    )

    assert "GITHUB_TOKEN" not in program.ports.source_environment
    assert "NIKA_TEST_SECRET" not in program.ports.source_environment
    assert program.ports.source_environment["GIT_TERMINAL_PROMPT"] == "0"


def test_builder_rejects_malformed_provider_or_verifier_before_composition(
    tmp_path: pathlib.Path,
) -> None:
    repository, _base_sha = _repository(tmp_path)
    jobs = tmp_path / "contract jobs"
    jobs.mkdir()
    store = SQLiteStore(tmp_path / "contract.db")
    store.initialize()
    common = {
        "workspace_parent": jobs,
        "repositories": {"repo-1": repository},
        "client_factory": lambda _endpoint: None,
        "agent_profile_id_factory": lambda _job, _endpoint: (
            "11111111-1111-4111-8111-111111111111"
        ),
        "policy": _policy(),
    }

    with pytest.raises(OpenHandsProductFactoryError, match="sandbox provider"):
        build_openhands_product_factory_program(
            store,
            sandbox_provider=object(),
            acceptance_runtime=_AcceptanceRuntime(),
            **common,
        )

    with pytest.raises(OpenHandsProductFactoryError, match="acceptance runtime"):
        build_openhands_product_factory_program(
            store,
            sandbox_provider=_SandboxProvider(),
            acceptance_runtime=object(),
            **common,
        )


class _MutatingRemoteRuntime:
    def __init__(self) -> None:
        self.calls = []

    async def execute(self, job, endpoint, prompt, source_root, source_evidence):
        self.calls.append((job, endpoint, prompt, source_root, source_evidence))
        files = []
        for item in source_evidence.files:
            data = (source_root / item.path).read_bytes()
            if item.path == "src/core.py":
                data = b"VALUE = 2\n"
            files.append(RemoteFile(item.path, data))
        return OpenHandsRunEvidence("conversation-host-seam", tuple(files))

    async def reconcile(self, job, binding, source_evidence):
        raise AssertionError((job, binding, source_evidence))

    async def cancel_recovery(self, binding):
        raise AssertionError(binding)

    async def cancel(self, job_id):
        raise AssertionError(job_id)


class _PassingAcceptanceRuntime:
    def __init__(self) -> None:
        self.calls = []

    async def execute(self, job, candidate_files, candidate_evidence):
        self.calls.append((job, candidate_files, candidate_evidence))
        evidence = tuple(
            TestEvidence(
                command.argv,
                0,
                hashlib.sha256("\0".join(command.argv).encode("utf-8")).hexdigest(),
            )
            for command in job.acceptance_commands
        )
        return SandboxedAcceptanceEvidence(
            IsolationClass.OS_SANDBOXED,
            candidate_evidence.digest,
            evidence,
        )

    async def cancel(self, job_id):
        raise AssertionError(job_id)


class _WorkingSandboxProvider:
    def __init__(self) -> None:
        self.released = []

    async def acquire(self, _job):
        return OpenHandsSandboxEndpoint(
            endpoint_id="sandbox-host-seam",
            host="http://127.0.0.1:30000",
            working_dir="/workspace/nika-job",
            isolation_class=IsolationClass.REMOTE_SANDBOXED,
            sandbox_egress_hosts=("localhost",),
            network_policy_enforced=True,
        )

    async def release(self, job, endpoint, *, succeeded):
        self.released.append((job.job_id, endpoint.endpoint_id, succeeded))


def test_ports_drive_real_openhands_worker_contract_to_private_candidate(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    _store, program = _program(tmp_path, repository)
    provider = _WorkingSandboxProvider()
    runtime = _MutatingRemoteRuntime()
    acceptance = _PassingAcceptanceRuntime()
    worker = OpenHandsRemoteCodingWorker(
        provider,
        runtime,
        acceptance_runtime=acceptance,
    )
    adapter = CodingWorkerComponentAdapter(worker, program.ports, program.ports)
    request = _request(base_sha, work_id="work-full-openhands-dispatch")

    envelope = _run(adapter.dispatch(request))

    assert envelope.base_sha == base_sha
    assert envelope.result_sha != base_sha
    assert len(envelope.diff_digest) == 64
    assert envelope.producer_actor_id == "openhands-remote-coding-worker"
    assert [item.path for item in envelope.coding_result.changed_files] == ["src/core.py"]
    assert envelope.coding_result.test_evidence[0].exit_code == 0
    assert provider.released == [
        (request.work_id, "sandbox-host-seam", True)
    ]
    assert len(runtime.calls) == 1
    assert len(acceptance.calls) == 1
    assert (repository / "src" / "core.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert program.ports.candidate_worktree(request.work_id).joinpath(
        "src", "core.py"
    ).read_text(encoding="utf-8") == "VALUE = 2\n"
