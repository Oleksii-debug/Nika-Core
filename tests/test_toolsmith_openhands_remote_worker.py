from __future__ import annotations

import asyncio
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

import nika_core.toolsmith.openhands_remote_worker as openhands_worker_module
from nika_core.toolsmith.contracts import (
    AcceptanceCommand,
    AllowedPathPolicy,
    CodingJob,
    IsolationClass,
    NetworkMode,
    NetworkPolicy,
    ProcessPolicy,
    RecoveryState,
    RepositorySnapshot,
    ResourceBudget,
    WorkspaceLease,
)
from nika_core.toolsmith.openhands_remote_worker import (
    OpenHandsRemoteCodingWorker,
    OpenHandsRunEvidence,
    OpenHandsSandboxEndpoint,
    RemoteFile,
)
from nika_core.toolsmith.openhands_sdk_runtime import (
    OpenHandsSdkCompatibilityError,
    _read_snapshot_archive,
)
from nika_core.toolsmith.workspace_security import collect_tree_evidence

SHA = "a" * 40


def _run(coro):
    return asyncio.run(coro)


def _endpoint(host: str = "http://127.0.0.1:30000") -> OpenHandsSandboxEndpoint:
    return OpenHandsSandboxEndpoint(
        endpoint_id="sandbox-1",
        host=host,
        working_dir="/workspace/nika-job",
        isolation_class=IsolationClass.REMOTE_SANDBOXED,
        sandbox_egress_hosts=("localhost",),
        network_policy_enforced=True,
    )


def _job(
    root: Path,
    *,
    network: NetworkPolicy | None = None,
    max_changed_files: int = 8,
) -> CodingJob:
    evidence = collect_tree_evidence(root)
    return CodingJob(
        job_id="job-1",
        task_id="task-1",
        goal="Update the allowed source file",
        repository=RepositorySnapshot("repo-1", SHA, evidence.digest),
        lease=WorkspaceLease(
            "lease-1",
            root,
            IsolationClass.PROCESS_CONTAINED,
            "2099-01-01T00:00:00Z",
        ),
        allowed_paths=AllowedPathPolicy(("src",)),
        process_policy=ProcessPolicy((sys.executable,)),
        network_policy=network
        or NetworkPolicy(NetworkMode.APPROVED_HOSTS, ("127.0.0.1", "localhost")),
        resource_budget=ResourceBudget(30, 1024 * 1024, max_changed_files),
        acceptance_commands=(AcceptanceCommand((sys.executable, "-c", "print('ok')")),),
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )


class Provider:
    def __init__(self, endpoint: OpenHandsSandboxEndpoint | None = None) -> None:
        self.endpoint = endpoint or _endpoint()
        self.acquired = []
        self.released = []

    async def acquire(self, job):
        self.acquired.append(job.job_id)
        return self.endpoint

    async def release(self, job, endpoint, *, succeeded: bool):
        self.released.append((job.job_id, endpoint.endpoint_id, succeeded))


class Runtime:
    def __init__(
        self,
        files: tuple[RemoteFile, ...] | None = None,
        error: Exception | None = None,
        *,
        cancel_verified: bool = True,
    ):
        self.files = files
        self.error = error
        self.cancel_verified = cancel_verified
        self.calls = []
        self.cancelled = []

    async def execute(self, job, endpoint, prompt, source_root, source_evidence):
        self.calls.append((job, endpoint, prompt, source_root, source_evidence))
        if self.error is not None:
            raise self.error
        if self.files is None:
            self.files = tuple(
                RemoteFile(item.path, (source_root / item.path).read_bytes())
                for item in source_evidence.files
            )
        return OpenHandsRunEvidence("conversation-1", self.files)

    async def cancel(self, job_id):
        self.cancelled.append(job_id)
        return self.cancel_verified


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "worker root"
    (root / "src").mkdir(parents=True)
    (root / "src" / "value.txt").write_text("before\n", encoding="utf-8")
    return root


def test_remote_worker_applies_only_validated_delta_and_runs_nika_acceptance(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    runtime = Runtime((RemoteFile("src/value.txt", b"after\n"),))
    provider = Provider()
    worker = OpenHandsRemoteCodingWorker(provider, runtime)

    result = _run(worker.execute(_job(root)))

    assert result.succeeded
    assert root.joinpath("src/value.txt").read_bytes() == b"after\n"
    assert [item.path for item in result.changed_files] == ["src/value.txt"]
    assert result.test_evidence[0].exit_code == 0
    assert len(result.test_evidence[0].output_digest) == 64
    assert provider.released == [("job-1", "sandbox-1", True)]
    assert "Do not commit, push" in runtime.calls[0][2]



def test_remote_worker_does_not_disclose_acceptance_arguments_to_engine(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    job = _job(root)
    canary = "NIKA_ACCEPTANCE_ARGUMENT_CANARY"
    job = CodingJob(
        job.job_id,
        job.task_id,
        job.goal,
        job.repository,
        job.lease,
        job.allowed_paths,
        job.process_policy,
        job.network_policy,
        job.resource_budget,
        (AcceptanceCommand((sys.executable, "-c", f"print('{canary}')")),),
        job.permission_ceiling,
    )
    runtime = Runtime()

    result = _run(OpenHandsRemoteCodingWorker(Provider(), runtime).execute(job))

    assert result.succeeded
    assert canary not in runtime.calls[0][2]
    assert "Acceptance command arguments are intentionally withheld" in runtime.calls[0][2]


def test_failed_acceptance_preserves_changed_evidence_and_requires_new_job(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    job = _job(root)
    job = CodingJob(
        job.job_id,
        job.task_id,
        job.goal,
        job.repository,
        job.lease,
        job.allowed_paths,
        job.process_policy,
        job.network_policy,
        job.resource_budget,
        (AcceptanceCommand((sys.executable, "-c", "raise SystemExit(7)")),),
        job.permission_ceiling,
    )
    runtime = Runtime((RemoteFile("src/value.txt", b"after\n"),))
    worker = OpenHandsRemoteCodingWorker(Provider(), runtime)

    result = _run(worker.execute(job))

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "process_failed"
    assert result.failure.retryable is True
    assert [item.path for item in result.changed_files] == ["src/value.txt"]
    assert result.test_evidence[0].exit_code == 7
    assert result.recovery_state is not None
    assert result.recovery_state.phase == "repair_required"
    assert root.joinpath("src/value.txt").read_bytes() == b"after\n"

    recovery = _run(worker.recover(job, result.recovery_state))

    assert not recovery.succeeded
    assert recovery.failure is not None
    assert recovery.failure.kind.value == "invalid_request"
    assert len(runtime.calls) == 1


def test_worker_visible_git_metadata_is_rejected_before_remote_execution(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    root.joinpath(".git").mkdir()
    provider = Provider()
    runtime = Runtime()
    job = _job(root)

    result = _run(OpenHandsRemoteCodingWorker(provider, runtime).execute(job))

    assert not result.succeeded
    assert ".git" in result.failure.message
    assert provider.acquired == []
    assert runtime.calls == []


def test_remote_worker_rejects_out_of_scope_change_before_local_mutation(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    runtime = Runtime(
        (
            RemoteFile("src/value.txt", b"before\n"),
            RemoteFile("docs/escape.txt", b"not allowed\n"),
        )
    )
    worker = OpenHandsRemoteCodingWorker(Provider(), runtime)

    result = _run(worker.execute(_job(root)))

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "policy_violation"
    assert root.joinpath("src/value.txt").read_bytes() == b"before\n"
    assert not root.joinpath("docs/escape.txt").exists()


def test_remote_worker_rejects_deletion_fail_closed(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    runtime = Runtime(())

    result = _run(OpenHandsRemoteCodingWorker(Provider(), runtime).execute(_job(root)))

    assert not result.succeeded
    assert "deletion" in result.failure.message
    assert root.joinpath("src/value.txt").read_bytes() == b"before\n"


def test_remote_worker_requires_explicit_loopback_network_authorization(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    denied = _job(root, network=NetworkPolicy())
    worker = OpenHandsRemoteCodingWorker(Provider(), Runtime())

    result = _run(worker.execute(denied))

    assert not result.succeeded
    assert result.failure.kind.value == "policy_violation"


def test_remote_worker_accepts_approved_https_remote_agent_server(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    provider = Provider(_endpoint("https://agent.example.test"))
    job = _job(
        root,
        network=NetworkPolicy(
            NetworkMode.APPROVED_HOSTS,
            ("agent.example.test", "localhost"),
        ),
    )

    result = _run(OpenHandsRemoteCodingWorker(provider, Runtime()).execute(job))

    assert result.succeeded


def test_remote_worker_rejects_cleartext_non_loopback_control_plane(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    provider = Provider(_endpoint("http://agent.example.test"))
    job = _job(
        root,
        network=NetworkPolicy(
            NetworkMode.APPROVED_HOSTS,
            ("agent.example.test", "localhost"),
        ),
    )

    result = _run(OpenHandsRemoteCodingWorker(provider, Runtime()).execute(job))

    assert not result.succeeded
    assert "HTTPS" in result.failure.message


def test_endpoint_requires_explicit_network_enforcement_attestation() -> None:
    with pytest.raises(ValueError, match="network policy"):
        OpenHandsSandboxEndpoint(
            endpoint_id="sandbox-unenforced",
            host="https://agent.example.test",
            working_dir="/workspace/nika-job",
            isolation_class=IsolationClass.REMOTE_SANDBOXED,
            sandbox_egress_hosts=("model.example.test",),
        )


def test_remote_worker_rejects_stale_local_tree_before_acquiring_sandbox(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    job = _job(root)
    root.joinpath("src/value.txt").write_text("changed after evidence\n", encoding="utf-8")
    provider = Provider()

    result = _run(OpenHandsRemoteCodingWorker(provider, Runtime()).execute(job))

    assert not result.succeeded
    assert "tree evidence" in result.failure.message
    assert provider.acquired == []


def test_remote_worker_redacts_untrusted_engine_exception(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    provider = Provider()
    runtime = Runtime(error=RuntimeError("secret access_token=should-never-surface"))

    result = _run(OpenHandsRemoteCodingWorker(provider, runtime).execute(_job(root)))

    assert not result.succeeded
    assert "access_token" not in result.failure.message
    assert result.failure.message == "remote coding engine failed without trusted diagnostics"
    assert provider.released == [("job-1", "sandbox-1", False)]


def test_remote_worker_rejects_policy_only_local_staging(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    job = _job(root)
    job = CodingJob(
        job.job_id,
        job.task_id,
        job.goal,
        job.repository,
        WorkspaceLease("lease-2", root, IsolationClass.POLICY_ONLY, "2099-01-01T00:00:00Z"),
        job.allowed_paths,
        job.process_policy,
        job.network_policy,
        job.resource_budget,
        job.acceptance_commands,
        job.permission_ceiling,
    )

    result = _run(OpenHandsRemoteCodingWorker(Provider(), Runtime()).execute(job))

    assert not result.succeeded
    assert "isolation" in result.failure.message


def test_remote_worker_enforces_max_changed_files_before_apply(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    runtime = Runtime(
        (
            RemoteFile("src/value.txt", b"after\n"),
            RemoteFile("src/second.txt", b"second\n"),
        )
    )

    result = _run(
        OpenHandsRemoteCodingWorker(Provider(), runtime).execute(
            _job(root, max_changed_files=1)
        )
    )

    assert not result.succeeded
    assert root.joinpath("src/value.txt").read_bytes() == b"before\n"
    assert not root.joinpath("src/second.txt").exists()



@pytest.mark.parametrize(
    "host",
    (
        "https://user:secret@agent.example.test",
        "https://agent.example.test/path",
        "https://agent.example.test?token=secret",
        "https://agent.example.test#fragment",
    ),
)
def test_endpoint_rejects_non_authority_url_components(host: str) -> None:
    with pytest.raises(ValueError, match="authority only"):
        _endpoint(host)


def test_cancelled_recovery_is_terminal_and_never_reexecutes(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    runtime = Runtime()
    worker = OpenHandsRemoteCodingWorker(Provider(), runtime)

    result = _run(worker.recover(_job(root), RecoveryState("cancelled")))

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "cancelled"
    assert result.failure.retryable is False
    assert runtime.calls == []


def test_unverified_cancel_requires_manual_reconciliation(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    runtime = Runtime(cancel_verified=False)
    worker = OpenHandsRemoteCodingWorker(Provider(), runtime)
    job = _job(root)

    _run(worker.cancel(job.job_id))
    state = _run(worker.inspect(job.job_id))
    result = _run(worker.recover(job, state))

    assert state == RecoveryState("manual_reconcile_required")
    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.retryable is False
    assert runtime.calls == []


def test_post_apply_evidence_mismatch_rolls_back_preimage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workspace(tmp_path)
    job = _job(root)
    before = collect_tree_evidence(root)
    monkeypatch.setattr(
        openhands_worker_module,
        "collect_tree_evidence",
        lambda _root: before,
    )

    with pytest.raises(
        openhands_worker_module.OpenHandsWorkerError,
        match="post-apply evidence",
    ):
        openhands_worker_module._validate_and_apply_snapshot(
            job,
            root,
            before,
            (RemoteFile("src/value.txt", b"after\n"),),
        )

    assert root.joinpath("src/value.txt").read_bytes() == b"before\n"


def test_acceptance_mutation_invalidates_candidate_after_test_execution(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    base = _job(root)
    job = CodingJob(
        base.job_id,
        base.task_id,
        base.goal,
        base.repository,
        base.lease,
        base.allowed_paths,
        base.process_policy,
        base.network_policy,
        base.resource_budget,
        (
            AcceptanceCommand(
                (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('src/acceptance.txt').write_text('mutated')",
                )
            ),
        ),
        base.permission_ceiling,
    )

    result = _run(OpenHandsRemoteCodingWorker(Provider(), Runtime()).execute(job))

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "internal_error"
    assert result.recovery_state == RecoveryState("manual_reconcile_required")


def _tar_snapshot(files: dict[str, bytes], *, root: str = "nika-job") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for relative, content in files.items():
            info = tarfile.TarInfo(f"{root}/{relative}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def test_sdk_snapshot_reader_removes_only_consistent_synthetic_manifest() -> None:
    ordinary = {"src/value.txt": b"after\n"}
    manifest = json.dumps(
        {
            "format": "tar.gz",
            "source": "nika-job",
            "file_count": 1,
            "total_bytes": len(ordinary["src/value.txt"]),
            "excludes": [],
        }
    ).encode()
    payload = _tar_snapshot({**ordinary, "archive_manifest.json": manifest})

    files = _read_snapshot_archive(
        io.BytesIO(payload),
        expected_root="nika-job",
        baseline_paths={"src/value.txt"},
    )

    assert files == (RemoteFile("src/value.txt", b"after\n"),)


def test_sdk_snapshot_reader_preserves_authored_archive_manifest() -> None:
    payload = _tar_snapshot({"archive_manifest.json": b"authored\n"})

    files = _read_snapshot_archive(
        io.BytesIO(payload),
        expected_root="nika-job",
        baseline_paths={"archive_manifest.json"},
    )

    assert files == (RemoteFile("archive_manifest.json", b"authored\n"),)


def test_sdk_snapshot_reader_rejects_symlink_member() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo("nika-job/src/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside"
        archive.addfile(member)
    buffer.seek(0)

    with pytest.raises(OpenHandsSdkCompatibilityError, match="non-regular"):
        _read_snapshot_archive(
            buffer,
            expected_root="nika-job",
            baseline_paths=set(),
        )
