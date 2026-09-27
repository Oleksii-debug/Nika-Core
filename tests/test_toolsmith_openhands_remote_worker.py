from __future__ import annotations

import asyncio
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

from nika_core.toolsmith.contracts import (
    AcceptanceCommand,
    AllowedPathPolicy,
    CodingJob,
    IsolationClass,
    NetworkMode,
    NetworkPolicy,
    ProcessPolicy,
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
    def __init__(self, files: tuple[RemoteFile, ...] | None = None, error: Exception | None = None):
        self.files = files
        self.error = error
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


def test_remote_worker_rejects_non_loopback_agent_server(tmp_path: Path) -> None:
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

    assert not result.succeeded
    assert "loopback" in result.failure.message


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
