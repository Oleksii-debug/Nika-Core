from __future__ import annotations

import asyncio
import io
import json
import sys
import tarfile
import threading
import uuid
from pathlib import Path

import httpx
import pytest

import nika_core.toolsmith.openhands_remote_worker as openhands_worker_module
import nika_core.toolsmith.openhands_sdk_runtime as openhands_sdk_module
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
    OpenHandsAgentServerCompatibilityError,
    OpenHandsAgentServerExecutionCancelled,
    OpenHandsAgentServerRuntime,
    _read_snapshot_archive,
)
from nika_core.toolsmith.workspace_security import TreeEvidence, collect_tree_evidence

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
    def __init__(
        self,
        endpoint: OpenHandsSandboxEndpoint | None = None,
        *,
        acquire_error: Exception | None = None,
        release_error: Exception | None = None,
    ) -> None:
        self.endpoint = endpoint or _endpoint()
        self.acquire_error = acquire_error
        self.release_error = release_error
        self.acquired = []
        self.released = []

    async def acquire(self, job):
        self.acquired.append(job.job_id)
        if self.acquire_error is not None:
            raise self.acquire_error
        return self.endpoint

    async def release(self, job, endpoint, *, succeeded: bool):
        self.released.append((job.job_id, endpoint.endpoint_id, succeeded))
        if self.release_error is not None:
            raise self.release_error


class BlockingAcquireProvider(Provider):
    def __init__(self) -> None:
        super().__init__()
        self.acquire_started = asyncio.Event()
        self.allow_acquire = asyncio.Event()

    async def acquire(self, job):
        self.acquired.append(job.job_id)
        self.acquire_started.set()
        await self.allow_acquire.wait()
        return self.endpoint


class BlockingReleaseProvider(Provider):
    def __init__(self) -> None:
        super().__init__()
        self.release_started = asyncio.Event()
        self.never_release = asyncio.Event()
        self.release_cancelled = False

    async def release(self, job, endpoint, *, succeeded: bool):
        self.released.append((job.job_id, endpoint.endpoint_id, succeeded))
        self.release_started.set()
        try:
            await self.never_release.wait()
        except asyncio.CancelledError:
            self.release_cancelled = True
            raise


class Runtime:
    def __init__(
        self,
        files: tuple[RemoteFile, ...] | None = None,
        error: Exception | None = None,
        *,
        cancel_verified: object = True,
        cancel_error: Exception | None = None,
    ):
        self.files = files
        self.error = error
        self.cancel_verified = cancel_verified
        self.cancel_error = cancel_error
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
        if self.cancel_error is not None:
            raise self.cancel_error
        return self.cancel_verified


class BlockingRuntime(Runtime):
    def __init__(
        self,
        *,
        cancel_verified: object = True,
        cancel_error: Exception | None = None,
    ) -> None:
        super().__init__(
            cancel_verified=cancel_verified,
            cancel_error=cancel_error,
        )
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, job, endpoint, prompt, source_root, source_evidence):
        self.calls.append((job, endpoint, prompt, source_root, source_evidence))
        self.started.set()
        await self.release.wait()
        files = tuple(
            RemoteFile(item.path, (source_root / item.path).read_bytes())
            for item in source_evidence.files
        )
        return OpenHandsRunEvidence(f"conversation-{job.job_id}", files)


class DeferredCancelRuntime:
    def __init__(self) -> None:
        self.execute_started = asyncio.Event()
        self.allow_execute_finish = asyncio.Event()
        self.cancel_started = asyncio.Event()
        self.allow_cancel_finish = asyncio.Event()

    async def execute(self, job, endpoint, prompt, source_root, source_evidence):
        del job, endpoint, prompt
        self.execute_started.set()
        await self.allow_execute_finish.wait()
        files = tuple(
            RemoteFile(item.path, (source_root / item.path).read_bytes())
            for item in source_evidence.files
        )
        return OpenHandsRunEvidence("conversation-race", files)

    async def cancel(self, _job_id):
        self.cancel_started.set()
        await self.allow_cancel_finish.wait()
        return False


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "worker root"
    (root / "src").mkdir(parents=True)
    (root / "src" / "value.txt").write_bytes(b"before\n")
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

    assert recovery == result
    assert len(runtime.calls) == 1


def test_completed_job_identity_cannot_be_executed_twice(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    runtime = Runtime()
    worker = OpenHandsRemoteCodingWorker(Provider(), runtime)
    job = _job(root)

    first = _run(worker.execute(job))
    second = _run(worker.execute(job))
    _run(worker.cancel(job.job_id))
    state = _run(worker.inspect(job.job_id))
    recovered = _run(worker.recover(job, state))

    assert first.succeeded
    assert not second.succeeded
    assert second.failure is not None
    assert second.failure.kind.value == "invalid_request"
    assert second.recovery_state == first.recovery_state
    assert state == first.recovery_state
    assert recovered == first
    assert len(runtime.calls) == 1


def test_late_cancel_of_finalized_interruption_never_restarts_job(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    runtime = Runtime(error=RuntimeError("untrusted engine failure"))
    worker = OpenHandsRemoteCodingWorker(Provider(), runtime)
    job = _job(root)

    result = _run(worker.execute(job))
    _run(worker.cancel(job.job_id))
    state = _run(worker.inspect(job.job_id))
    recovered = _run(worker.recover(job, state))

    assert not result.succeeded
    assert result.recovery_state == RecoveryState("interrupted")
    assert state == result.recovery_state
    assert recovered == result
    assert len(runtime.calls) == 1


def test_fresh_worker_recovery_probe_failure_is_redacted_manual_reconciliation(
    tmp_path: Path,
) -> None:
    class FailingProbe:
        async def inspect(self, _job_id):
            raise RuntimeError("database=/secret/path diagnostic must not escape")

    root = _workspace(tmp_path)
    provider = Provider()
    runtime = Runtime()
    worker = OpenHandsRemoteCodingWorker(
        provider,
        runtime,
        recovery_probe=FailingProbe(),
    )

    result = _run(worker.recover(_job(root), RecoveryState("interrupted")))

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "internal_error"
    assert result.failure.retryable is False
    assert result.failure.message == "durable worker recovery state could not be inspected"
    assert result.recovery_state == RecoveryState("manual_reconcile_required")
    assert provider.acquired == []
    assert runtime.calls == []


def test_unfinalized_interrupted_state_can_resume_exact_job_once(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    runtime = Runtime()
    worker = OpenHandsRemoteCodingWorker(Provider(), runtime)
    job = _job(root)
    interrupted = RecoveryState("interrupted")
    _run(worker._set_state(job.job_id, interrupted))

    result = _run(worker.recover(job, interrupted))

    assert result.succeeded
    assert len(runtime.calls) == 1


def test_fresh_worker_cannot_replay_caller_supplied_interrupted_state_without_durable_evidence(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    provider = Provider()
    runtime = Runtime()
    worker = OpenHandsRemoteCodingWorker(provider, runtime)
    job = _job(root)

    result = _run(worker.recover(job, RecoveryState("interrupted")))

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "internal_error"
    assert result.failure.retryable is False
    assert result.recovery_state == RecoveryState("manual_reconcile_required")
    assert provider.acquired == []
    assert runtime.calls == []


def test_unexpected_post_apply_failure_requires_manual_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workspace(tmp_path)
    runtime = Runtime((RemoteFile("src/value.txt", b"after\n"),))
    worker = OpenHandsRemoteCodingWorker(Provider(), runtime)
    job = _job(root)

    def fail_acceptance(*_args):
        raise RuntimeError("untrusted post-apply failure")

    monkeypatch.setattr(openhands_worker_module, "_run_acceptance", fail_acceptance)
    result = _run(worker.execute(job))

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.retryable is False
    assert result.recovery_state == RecoveryState("manual_reconcile_required")
    assert root.joinpath("src/value.txt").read_bytes() == b"after\n"

    recovery = _run(worker.recover(job, result.recovery_state))
    assert not recovery.succeeded
    assert recovery.failure is not None
    assert recovery.failure.retryable is False
    assert len(runtime.calls) == 1


def test_worker_visible_git_metadata_is_rejected_before_remote_execution(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    job = _job(root)
    root.joinpath(".git").mkdir()
    provider = Provider()
    runtime = Runtime()

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


def test_remote_worker_rejects_backslash_path_spelling_without_reinterpretation(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    runtime = Runtime(
        (
            RemoteFile("src/value.txt", b"before\n"),
            RemoteFile("src\\escape.txt", b"not canonical\n"),
        )
    )

    result = _run(OpenHandsRemoteCodingWorker(Provider(), runtime).execute(_job(root)))

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "policy_violation"
    assert "canonical POSIX" in result.failure.message
    assert not root.joinpath("src", "escape.txt").exists()


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


def test_sandbox_endpoint_rejects_truthy_non_boolean_attestations() -> None:
    with pytest.raises(ValueError, match="exact booleans"):
        OpenHandsSandboxEndpoint(
            endpoint_id="sandbox-1",
            host="https://agent.example.test",
            working_dir="/workspace/nika-job",
            isolation_class=IsolationClass.REMOTE_SANDBOXED,
            sandbox_egress_hosts=("agent.example.test",),
            network_policy_enforced=1,
            fresh_workspace=True,
        )


def test_sandbox_endpoint_rejects_mutable_egress_host_carrier() -> None:
    with pytest.raises(ValueError, match="immutable string tuple"):
        OpenHandsSandboxEndpoint(
            endpoint_id="sandbox-1",
            host="https://agent.example.test",
            working_dir="/workspace/nika-job",
            isolation_class=IsolationClass.REMOTE_SANDBOXED,
            sandbox_egress_hosts=["agent.example.test"],
            network_policy_enforced=True,
            fresh_workspace=True,
        )


def test_remote_worker_rejects_windows_case_colliding_snapshot_before_mutation(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    runtime = Runtime(
        (
            RemoteFile("src/value.txt", b"before\n"),
            RemoteFile("src/Value.txt", b"ambiguous\n"),
        )
    )
    provider = Provider()

    result = _run(OpenHandsRemoteCodingWorker(provider, runtime).execute(_job(root)))

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "policy_violation"
    assert "case-colliding" in result.failure.message
    assert root.joinpath("src/value.txt").read_bytes() == b"before\n"


@pytest.mark.parametrize("endpoint_id", (" sandbox-1", "sandbox-1 ", "sandbox\n1"))
def test_endpoint_rejects_noncanonical_identity(endpoint_id: str) -> None:
    with pytest.raises(ValueError, match="identity"):
        OpenHandsSandboxEndpoint(
            endpoint_id=endpoint_id,
            host="https://agent.example.test",
            working_dir="/workspace/nika-job",
            isolation_class=IsolationClass.REMOTE_SANDBOXED,
            sandbox_egress_hosts=("localhost",),
            network_policy_enforced=True,
        )


def test_worker_blocks_concurrent_reuse_of_attested_fresh_endpoint(
    tmp_path: Path,
) -> None:
    root_one = _workspace(tmp_path / "one")
    root_two = _workspace(tmp_path / "two")
    root_three = _workspace(tmp_path / "three")
    job_one = _job(root_one)
    base_two = _job(root_two)
    job_two = CodingJob(
        "job-2",
        "task-2",
        base_two.goal,
        base_two.repository,
        base_two.lease,
        base_two.allowed_paths,
        base_two.process_policy,
        base_two.network_policy,
        base_two.resource_budget,
        base_two.acceptance_commands,
        base_two.permission_ceiling,
    )
    base_three = _job(root_three)
    job_three = CodingJob(
        "job-3",
        "task-3",
        base_three.goal,
        base_three.repository,
        base_three.lease,
        base_three.allowed_paths,
        base_three.process_policy,
        base_three.network_policy,
        base_three.resource_budget,
        base_three.acceptance_commands,
        base_three.permission_ceiling,
    )
    provider = Provider()
    runtime = BlockingRuntime()
    worker = OpenHandsRemoteCodingWorker(provider, runtime)

    async def scenario():
        first = asyncio.create_task(worker.execute(job_one))
        await asyncio.wait_for(runtime.started.wait(), timeout=2)

        collision = await worker.execute(job_two)
        assert not collision.succeeded
        assert collision.failure is not None
        assert collision.failure.kind.value == "internal_error"
        assert collision.failure.retryable is False
        assert collision.recovery_state == RecoveryState("manual_reconcile_required")
        assert collision.failure.message == (
            "remote sandbox endpoint identity collision requires provider reconciliation"
        )
        assert provider.released == []

        runtime.release.set()
        first_result = await asyncio.wait_for(first, timeout=2)
        third_result = await asyncio.wait_for(worker.execute(job_three), timeout=2)
        return first_result, third_result

    first_result, third_result = _run(scenario())

    assert first_result.succeeded
    assert third_result.succeeded
    assert [item[0] for item in runtime.calls] == [job_one, job_three]
    assert provider.released == [
        ("job-1", "sandbox-1", True),
        ("job-3", "sandbox-1", True),
    ]


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


@pytest.mark.parametrize(
    "working_dir",
    (
        "/workspace/../escape",
        "/workspace//nika-job",
        "/workspace/./nika-job",
        "/",
        "/workspace\\nika-job",
    ),
)
def test_endpoint_rejects_noncanonical_remote_working_directory(working_dir: str) -> None:
    with pytest.raises(ValueError, match="canonical absolute POSIX"):
        OpenHandsSandboxEndpoint(
            endpoint_id="sandbox-path",
            host="https://agent.example.test",
            working_dir=working_dir,
            isolation_class=IsolationClass.REMOTE_SANDBOXED,
            sandbox_egress_hosts=("model.example.test",),
            network_policy_enforced=True,
        )


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


def test_remote_result_cannot_clobber_local_change_made_during_remote_execution(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    provider = Provider()

    class ConcurrentMutationRuntime(Runtime):
        async def execute(self, job, endpoint, prompt, source_root, source_evidence):
            self.calls.append((job, endpoint, prompt, source_root, source_evidence))
            source_root.joinpath("src/value.txt").write_bytes(b"concurrent\n")
            return OpenHandsRunEvidence(
                "conversation-1",
                (RemoteFile("src/value.txt", b"remote\n"),),
            )

    runtime = ConcurrentMutationRuntime()

    result = _run(OpenHandsRemoteCodingWorker(provider, runtime).execute(_job(root)))

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "policy_violation"
    assert "changed during remote coding execution" in result.failure.message
    assert root.joinpath("src/value.txt").read_bytes() == b"concurrent\n"
    assert provider.released == [("job-1", "sandbox-1", False)]


def test_acquisition_failure_redacts_untrusted_provider_diagnostics(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    provider = Provider(
        acquire_error=ValueError("password=must-not-surface endpoint=https://private.invalid"),
    )

    result = _run(OpenHandsRemoteCodingWorker(provider, Runtime()).execute(_job(root)))

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "internal_error"
    assert result.failure.retryable is False
    assert result.failure.message == (
        "remote sandbox acquisition failed without trusted diagnostics"
    )
    assert "password" not in result.failure.message
    assert result.recovery_state == RecoveryState("manual_reconcile_required")
    assert provider.released == []


def test_task_cancellation_during_sandbox_release_is_manual_reconciliation(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path / "first")
    second_root = _workspace(tmp_path / "second")
    provider = BlockingReleaseProvider()
    runtime = Runtime()
    worker = OpenHandsRemoteCodingWorker(provider, runtime)
    job = _job(root)
    second_base = _job(second_root)
    second_job = CodingJob(
        "job-2",
        "task-2",
        second_base.goal,
        second_base.repository,
        second_base.lease,
        second_base.allowed_paths,
        second_base.process_policy,
        second_base.network_policy,
        second_base.resource_budget,
        second_base.acceptance_commands,
        second_base.permission_ceiling,
    )

    async def scenario():
        execution = asyncio.create_task(worker.execute(job))
        await asyncio.wait_for(provider.release_started.wait(), timeout=2)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution

        state = await worker.inspect(job.job_id)
        second = await worker.execute(second_job)
        return state, second

    state, second = _run(scenario())

    assert state == RecoveryState("manual_reconcile_required")
    assert provider.release_cancelled is True
    assert provider.released == [("job-1", "sandbox-1", True)]
    assert not second.succeeded
    assert second.failure is not None
    assert second.failure.kind.value == "internal_error"
    assert second.recovery_state == RecoveryState("manual_reconcile_required")
    assert second.failure.message == (
        "remote sandbox endpoint identity collision requires provider reconciliation"
    )


def test_release_failure_overrides_success_and_requires_manual_reconciliation(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    provider = Provider(
        release_error=RuntimeError("secret cleanup_token=must-not-surface"),
    )
    worker = OpenHandsRemoteCodingWorker(provider, Runtime())

    result = _run(worker.execute(_job(root)))

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "internal_error"
    assert result.failure.retryable is False
    assert "cleanup_token" not in result.failure.message
    assert result.failure.message == "remote sandbox cleanup could not be proven"
    assert result.recovery_state == RecoveryState("manual_reconcile_required")
    assert _run(worker.inspect("job-1")) == RecoveryState("manual_reconcile_required")
    assert provider.released == [("job-1", "sandbox-1", True)]


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


def test_cancel_during_sandbox_acquire_never_dispatches_remote_runtime(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    provider = BlockingAcquireProvider()
    runtime = Runtime()
    worker = OpenHandsRemoteCodingWorker(provider, runtime)
    job = _job(root)

    async def scenario():
        execution = asyncio.create_task(worker.execute(job))
        await asyncio.wait_for(provider.acquire_started.wait(), timeout=2)
        await worker.cancel(job.job_id)
        provider.allow_acquire.set()
        return await asyncio.wait_for(execution, timeout=2)

    result = _run(scenario())

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "cancelled"
    assert result.recovery_state == RecoveryState("cancelled")
    assert runtime.calls == []
    assert runtime.cancelled == []
    assert provider.released == [("job-1", "sandbox-1", False)]


def test_task_cancellation_during_unresolved_sandbox_acquire_requires_manual_reconciliation(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    provider = BlockingAcquireProvider()
    runtime = Runtime()
    worker = OpenHandsRemoteCodingWorker(provider, runtime)
    job = _job(root)

    async def scenario():
        execution = asyncio.create_task(worker.execute(job))
        await asyncio.wait_for(provider.acquire_started.wait(), timeout=2)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
        return await worker.inspect(job.job_id)

    state = _run(scenario())

    assert state == RecoveryState("manual_reconcile_required")
    assert runtime.calls == []
    assert runtime.cancelled == ["job-1"]
    assert provider.released == []


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


def test_unknown_process_cancel_probe_failure_becomes_manual_reconciliation() -> None:
    class FailingProbe:
        async def inspect(self, _job_id):
            raise RuntimeError("database diagnostic must not escape")

    worker = OpenHandsRemoteCodingWorker(
        Provider(),
        Runtime(),
        recovery_probe=FailingProbe(),
    )

    _run(worker.cancel("lost-job"))

    assert _run(worker.inspect("lost-job")) == RecoveryState("manual_reconcile_required")


def test_cancelled_unknown_process_probe_does_not_leave_transient_state() -> None:
    class BlockingProbe:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def inspect(self, _job_id):
            self.started.set()
            await self.release.wait()


    async def scenario():
        probe = BlockingProbe()
        worker = OpenHandsRemoteCodingWorker(
            Provider(),
            Runtime(),
            recovery_probe=probe,
        )
        cancellation = asyncio.create_task(worker.cancel("lost-job"))
        await asyncio.wait_for(probe.started.wait(), timeout=2)
        cancellation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancellation
        return await worker.inspect("lost-job")

    state = _run(scenario())

    assert state == RecoveryState("manual_reconcile_required")


def test_cancel_probe_pending_recovery_is_manual_reconciliation(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    worker = OpenHandsRemoteCodingWorker(Provider(), Runtime())

    result = _run(worker.recover(_job(root), RecoveryState("cancel_probe_pending")))

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "internal_error"
    assert result.failure.retryable is False
    assert result.recovery_state == RecoveryState("manual_reconcile_required")


def test_acceptance_cancel_pending_recovery_is_manual_reconciliation(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    worker = OpenHandsRemoteCodingWorker(Provider(), Runtime())

    result = _run(
        worker.recover(
            _job(root),
            RecoveryState("acceptance_cancel_requested"),
        )
    )

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "internal_error"
    assert result.failure.retryable is False
    assert result.recovery_state == RecoveryState("manual_reconcile_required")


def test_runtime_cancel_exception_is_fail_closed_and_redacted(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    runtime = Runtime(
        error=TimeoutError(),
        cancel_error=RuntimeError("secret cancellation_token=must-not-surface"),
    )
    provider = Provider()

    result = _run(OpenHandsRemoteCodingWorker(provider, runtime).execute(_job(root)))

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "timeout"
    assert result.failure.retryable is False
    assert "cancellation_token" not in result.failure.message
    assert result.recovery_state == RecoveryState("manual_reconcile_required")
    assert runtime.cancelled == ["job-1"]
    assert provider.released == [("job-1", "sandbox-1", False)]


def test_non_boolean_runtime_cancel_proof_is_not_trusted(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    runtime = Runtime(error=TimeoutError(), cancel_verified=1)
    provider = Provider()

    result = _run(OpenHandsRemoteCodingWorker(provider, runtime).execute(_job(root)))

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "timeout"
    assert result.failure.retryable is False
    assert result.recovery_state == RecoveryState("manual_reconcile_required")
    assert provider.released == [("job-1", "sandbox-1", False)]


def test_direct_cancel_runtime_failure_becomes_manual_reconciliation(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    runtime = BlockingRuntime(
        cancel_error=RuntimeError("secret stop_token=must-not-surface"),
    )
    provider = Provider()
    worker = OpenHandsRemoteCodingWorker(provider, runtime)
    job = _job(root)

    async def scenario():
        execution = asyncio.create_task(worker.execute(job))
        await asyncio.wait_for(runtime.started.wait(), timeout=2)
        await worker.cancel(job.job_id)
        state = await worker.inspect(job.job_id)
        runtime.release.set()
        result = await asyncio.wait_for(execution, timeout=2)
        return state, result

    state, result = _run(scenario())

    assert state == RecoveryState("manual_reconcile_required")
    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "internal_error"
    assert result.failure.retryable is False
    assert runtime.cancelled == ["job-1"]
    assert provider.released == [("job-1", "sandbox-1", False)]


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


def test_pending_cancel_never_becomes_terminal_before_stop_proof(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    provider = Provider()
    runtime = DeferredCancelRuntime()
    worker = OpenHandsRemoteCodingWorker(provider, runtime)
    job = _job(root)

    async def scenario():
        execution = asyncio.create_task(worker.execute(job))
        await asyncio.wait_for(runtime.execute_started.wait(), timeout=2)
        cancellation = asyncio.create_task(worker.cancel(job.job_id))
        await asyncio.wait_for(runtime.cancel_started.wait(), timeout=2)

        runtime.allow_execute_finish.set()
        result = await asyncio.wait_for(execution, timeout=2)

        runtime.allow_cancel_finish.set()
        await asyncio.wait_for(cancellation, timeout=2)
        return result

    result = _run(scenario())

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "internal_error"
    assert result.recovery_state == RecoveryState("manual_reconcile_required")
    assert _run(worker.inspect(job.job_id)) == RecoveryState("manual_reconcile_required")
    assert provider.released == [("job-1", "sandbox-1", False)]


def test_cancel_waits_for_acceptance_stop_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workspace(tmp_path)
    provider = Provider()
    runtime = Runtime()
    worker = OpenHandsRemoteCodingWorker(provider, runtime)
    job = _job(root)

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_acceptance(
            _job,
            _root,
            _candidate_evidence,
            cancellation_event,
        ):
            started.set()
            while not cancellation_event.is_set():
                await asyncio.sleep(0.01)
            await release.wait()
            return ()

        monkeypatch.setattr(
            openhands_worker_module,
            "_run_acceptance_async",
            blocked_acceptance,
        )
        execution = asyncio.create_task(worker.execute(job))
        await asyncio.wait_for(started.wait(), timeout=2)
        cancellation = asyncio.create_task(worker.cancel(job.job_id))
        await asyncio.sleep(0.1)
        assert not cancellation.done()

        release.set()
        result = await asyncio.wait_for(execution, timeout=2)
        await asyncio.wait_for(cancellation, timeout=2)
        return result

    result = _run(scenario())

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "cancelled"
    assert result.recovery_state == RecoveryState("cancelled")
    assert runtime.cancelled == []
    assert provider.released == [("job-1", "sandbox-1", False)]


def test_worker_cancel_stops_inflight_acceptance_process_tree(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    marker = tmp_path / "acceptance-started.txt"
    base = _job(root)
    command = (
        "from pathlib import Path; import time; "
        f"Path({str(marker)!r}).write_text('started', encoding='utf-8'); "
        "time.sleep(30)"
    )
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
        (AcceptanceCommand((sys.executable, "-c", command)),),
        base.permission_ceiling,
    )
    provider = Provider()
    runtime = Runtime()
    worker = OpenHandsRemoteCodingWorker(provider, runtime)

    async def scenario():
        execution = asyncio.create_task(worker.execute(job))
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(0.05)
        assert marker.exists()
        await worker.cancel(job.job_id)
        return await asyncio.wait_for(execution, timeout=5)

    result = _run(scenario())

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "cancelled"
    assert result.recovery_state == RecoveryState("cancelled")
    assert result.test_evidence
    assert result.test_evidence[-1].exit_code != 0
    assert provider.released == [("job-1", "sandbox-1", False)]


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


def test_rollback_evidence_mismatch_escalates_to_manual_reconcile_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workspace(tmp_path)
    job = _job(root)
    before = collect_tree_evidence(root)
    forged = TreeEvidence(before.files, "0" * 64, before.total_bytes)
    observations = iter((before, before, forged))
    monkeypatch.setattr(
        openhands_worker_module,
        "collect_tree_evidence",
        lambda _root: next(observations),
    )

    with pytest.raises(
        openhands_worker_module.OpenHandsWorkspaceMutationError,
        match="proven rolled back",
    ):
        openhands_worker_module._validate_and_apply_snapshot(
            job,
            root,
            before,
            (RemoteFile("src/value.txt", b"after\n"),),
        )

    assert root.joinpath("src/value.txt").read_bytes() == b"before\n"


def test_acceptance_side_effects_are_isolated_from_validated_candidate(tmp_path: Path) -> None:
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

    assert result.succeeded
    assert not root.joinpath("src/acceptance.txt").exists()


def test_acceptance_escape_mutation_invalidates_original_candidate(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    base = _job(root)
    escaped = root / "src" / "acceptance-escape.txt"
    command = (
        "from pathlib import Path; "
        f"Path({str(escaped)!r}).write_text('mutated', encoding='utf-8')"
    )
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
        (AcceptanceCommand((sys.executable, "-c", command)),),
        base.permission_ceiling,
    )

    result = _run(OpenHandsRemoteCodingWorker(Provider(), Runtime()).execute(job))

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind.value == "internal_error"
    assert result.recovery_state == RecoveryState("manual_reconcile_required")
    assert escaped.read_text(encoding="utf-8") == "mutated"


def _tar_snapshot(files: dict[str, bytes], *, root: str = "nika-job") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for relative, content in files.items():
            info = tarfile.TarInfo(f"{root}/{relative}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()



PROFILE_ID = "11111111-1111-4111-8111-111111111111"
SESSION_KEY = "session-key-header-only-canary"


def _agent_server_client(
    endpoint: OpenHandsSandboxEndpoint,
    handler,
    *,
    authenticated: bool = True,
) -> httpx.Client:
    headers = {"X-Session-API-Key": SESSION_KEY} if authenticated else {}
    return httpx.Client(
        base_url=endpoint.host,
        headers=headers,
        transport=httpx.MockTransport(handler),
    )


def test_agent_server_runtime_uses_authenticated_profile_only_contract(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    job = _job(root)
    evidence = collect_tree_evidence(root)
    endpoint = _endpoint()
    archive = _tar_snapshot({"src/value.txt": b"after-http\n"})
    requests: list[httpx.Request] = []
    create_payload: dict[str, object] = {}
    message_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["X-Session-API-Key"] == SESSION_KEY
        if request.url.path == "/api/file/upload":
            assert request.method == "POST"
            assert request.url.params["path"] == "/workspace/nika-job/src/value.txt"
            assert b"before\n" in request.content
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/api/conversations" and request.method == "POST":
            payload = json.loads(request.content)
            create_payload.update(payload)
            assert payload["agent_profile_id"] == PROFILE_ID
            assert payload["workspace"] == {
                "kind": "LocalWorkspace",
                "working_dir": "/workspace/nika-job",
            }
            assert payload["secrets"] == {}
            assert "agent" not in payload
            assert "agent_settings" not in payload
            assert "api_key" not in json.dumps(payload).casefold()
            assert SESSION_KEY not in request.content.decode()
            return httpx.Response(
                201,
                json={
                    "id": payload["conversation_id"],
                    "execution_status": "idle",
                },
            )
        if request.url.path.endswith("/events") and request.method == "POST":
            payload = json.loads(request.content)
            message_payload.update(payload)
            assert payload == {
                "role": "user",
                "content": [{"type": "text", "text": "do work"}],
                "run": False,
            }
            assert SESSION_KEY not in request.content.decode()
            return httpx.Response(200, json={"success": True})
        if request.url.path.endswith("/run") and request.method == "POST":
            assert json.loads(request.content) == {}
            return httpx.Response(200, json={"success": True})
        if request.url.path.startswith("/api/conversations/") and request.method == "GET":
            return httpx.Response(200, json={"execution_status": "finished"})
        if request.url.path == "/api/file/archive":
            assert request.url.params["path"] == endpoint.working_dir
            assert request.url.params["format"] == "tar.gz"
            return httpx.Response(200, content=archive)
        raise AssertionError(f"unexpected Agent Server request: {request.method} {request.url}")

    runtime = OpenHandsAgentServerRuntime(
        client_factory=lambda supplied: _agent_server_client(supplied, handler),
        agent_profile_id_factory=lambda _job, _endpoint: PROFILE_ID,
        max_iterations=7,
        poll_interval_seconds=0.01,
    )

    result = _run(runtime.execute(job, endpoint, "do work", root, evidence))

    expected_conversation_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"nika-core:openhands:{endpoint.endpoint_id}:{job.job_id}",
        )
    )
    assert result.conversation_id == expected_conversation_id
    assert result.files == (RemoteFile("src/value.txt", b"after-http\n"),)
    assert create_payload["conversation_id"] == expected_conversation_id
    assert create_payload["max_iterations"] == 7
    assert create_payload["autotitle"] is False
    assert message_payload["role"] == "user"
    assert [request.url.path for request in requests] == [
        "/api/file/upload",
        "/api/conversations",
        f"/api/conversations/{expected_conversation_id}/events",
        f"/api/conversations/{expected_conversation_id}/run",
        f"/api/conversations/{expected_conversation_id}",
        "/api/file/archive",
    ]


def test_agent_server_runtime_rejects_missing_session_auth_before_remote_effect(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    evidence = collect_tree_evidence(root)
    endpoint = _endpoint()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    runtime = OpenHandsAgentServerRuntime(
        client_factory=lambda supplied: _agent_server_client(
            supplied,
            handler,
            authenticated=False,
        ),
        agent_profile_id_factory=lambda _job, _endpoint: PROFILE_ID,
    )

    with pytest.raises(
        OpenHandsAgentServerCompatibilityError,
        match="session API authentication",
    ):
        _run(runtime.execute(_job(root), endpoint, "do work", root, evidence))

    assert requests == []


def test_agent_server_runtime_rejects_mismatched_client_endpoint_before_remote_effect(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    evidence = collect_tree_evidence(root)
    endpoint = _endpoint()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    runtime = OpenHandsAgentServerRuntime(
        client_factory=lambda _supplied: httpx.Client(
            base_url="http://127.0.0.1:39999",
            headers={"X-Session-API-Key": SESSION_KEY},
            transport=httpx.MockTransport(handler),
        ),
        agent_profile_id_factory=lambda _job, _endpoint: PROFILE_ID,
    )

    with pytest.raises(OpenHandsAgentServerCompatibilityError, match="different endpoint"):
        _run(runtime.execute(_job(root), endpoint, "do work", root, evidence))

    assert requests == []


def test_agent_server_runtime_validates_profile_identity_before_upload(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    evidence = collect_tree_evidence(root)
    endpoint = _endpoint()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    runtime = OpenHandsAgentServerRuntime(
        client_factory=lambda supplied: _agent_server_client(supplied, handler),
        agent_profile_id_factory=lambda _job, _endpoint: "not-a-profile-uuid",
    )

    with pytest.raises(OpenHandsAgentServerCompatibilityError, match="profile id"):
        _run(runtime.execute(_job(root), endpoint, "do work", root, evidence))

    assert requests == []


def test_agent_server_cancel_reserved_before_execute_prevents_client_acquisition(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    job = _job(root)
    evidence = collect_tree_evidence(root)
    endpoint = _endpoint()
    client_calls: list[OpenHandsSandboxEndpoint] = []
    runtime = OpenHandsAgentServerRuntime(
        client_factory=lambda supplied: client_calls.append(supplied),
        agent_profile_id_factory=lambda _job, _endpoint: PROFILE_ID,
    )

    assert _run(runtime.cancel(job.job_id)) is True
    with pytest.raises(
        OpenHandsAgentServerExecutionCancelled,
        match="before HTTP client acquisition",
    ):
        _run(runtime.execute(job, endpoint, "do work", root, evidence))

    assert client_calls == []


def test_agent_server_cancel_covers_upload_before_conversation_creation(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    job = _job(root)
    evidence = collect_tree_evidence(root)
    endpoint = _endpoint()
    upload_started = threading.Event()
    allow_upload = threading.Event()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/file/upload":
            upload_started.set()
            assert allow_upload.wait(timeout=5)
            return httpx.Response(200, json={"success": True})
        raise AssertionError("conversation must not be created after upload-window cancellation")

    runtime = OpenHandsAgentServerRuntime(
        client_factory=lambda supplied: _agent_server_client(supplied, handler),
        agent_profile_id_factory=lambda _job, _endpoint: PROFILE_ID,
    )

    async def scenario() -> None:
        execution = asyncio.create_task(
            runtime.execute(job, endpoint, "do work", root, evidence)
        )
        assert await asyncio.to_thread(upload_started.wait, 2)
        cancellation = asyncio.create_task(runtime.cancel(job.job_id))
        await asyncio.sleep(0)
        allow_upload.set()
        assert await cancellation is True
        with pytest.raises(OpenHandsAgentServerExecutionCancelled):
            await execution

    _run(scenario())

    assert paths == ["/api/file/upload"]


def test_agent_server_task_cancellation_preserves_one_shot_stop_proof(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    job = _job(root)
    evidence = collect_tree_evidence(root)
    endpoint = _endpoint()
    upload_started = threading.Event()
    allow_upload = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/file/upload":
            upload_started.set()
            assert allow_upload.wait(timeout=5)
            return httpx.Response(200, json={"success": True})
        raise AssertionError("unexpected request after task cancellation")

    runtime = OpenHandsAgentServerRuntime(
        client_factory=lambda supplied: _agent_server_client(supplied, handler),
        agent_profile_id_factory=lambda _job, _endpoint: PROFILE_ID,
    )

    async def scenario() -> None:
        execution = asyncio.create_task(
            runtime.execute(job, endpoint, "do work", root, evidence)
        )
        assert await asyncio.to_thread(upload_started.wait, 2)
        execution.cancel()
        await asyncio.sleep(0)
        allow_upload.set()
        with pytest.raises(asyncio.CancelledError):
            await execution
        assert await runtime.cancel(job.job_id) is True
        assert await runtime.cancel(job.job_id) is False

    _run(scenario())


def test_agent_server_upload_requires_exact_boolean_success_carrier(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    evidence = collect_tree_evidence(root)
    endpoint = _endpoint()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": 1})

    with _agent_server_client(endpoint, handler) as client:
        with pytest.raises(
            OpenHandsAgentServerCompatibilityError,
            match="non-canonical success",
        ):
            OpenHandsAgentServerRuntime._upload_source(
                client,
                endpoint,
                root,
                evidence,
                timeout_seconds=30,
            )


def test_agent_server_upload_rejects_changed_source_before_http_effect(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    evidence = collect_tree_evidence(root)
    endpoint = _endpoint()
    requests: list[httpx.Request] = []
    root.joinpath("src/value.txt").write_text(
        "changed-after-evidence\n",
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    with _agent_server_client(endpoint, handler) as client:
        with pytest.raises(
            OpenHandsAgentServerCompatibilityError,
            match="captured tree evidence",
        ):
            OpenHandsAgentServerRuntime._upload_source(
                client,
                endpoint,
                root,
                evidence,
                timeout_seconds=30,
            )

    assert requests == []


def test_agent_server_rejects_terminal_failure_without_snapshot(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    job = _job(root)
    evidence = collect_tree_evidence(root)
    endpoint = _endpoint()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/file/upload":
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/api/conversations" and request.method == "POST":
            payload = json.loads(request.content)
            return httpx.Response(201, json={"id": payload["conversation_id"]})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"success": True})
        if request.url.path.endswith("/run"):
            return httpx.Response(200, json={"success": True})
        if request.method == "GET" and request.url.path.startswith("/api/conversations/"):
            return httpx.Response(200, json={"execution_status": "error"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    runtime = OpenHandsAgentServerRuntime(
        client_factory=lambda supplied: _agent_server_client(supplied, handler),
        agent_profile_id_factory=lambda _job, _endpoint: PROFILE_ID,
        poll_interval_seconds=0.01,
    )

    with pytest.raises(OpenHandsAgentServerCompatibilityError, match="status error"):
        _run(runtime.execute(job, endpoint, "do work", root, evidence))

    assert "/api/file/archive" not in paths


def test_agent_server_cancel_interrupts_active_conversation(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    job = _job(root)
    evidence = collect_tree_evidence(root)
    endpoint = _endpoint()
    status_started = threading.Event()
    allow_status = threading.Event()
    interrupt_seen = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/file/upload":
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/api/conversations" and request.method == "POST":
            payload = json.loads(request.content)
            return httpx.Response(201, json={"id": payload["conversation_id"]})
        if request.url.path.endswith("/events") or request.url.path.endswith("/run"):
            return httpx.Response(200, json={"success": True})
        if request.url.path.endswith("/interrupt"):
            interrupt_seen.set()
            return httpx.Response(200, json={"success": True})
        if request.method == "GET" and request.url.path.startswith("/api/conversations/"):
            status_started.set()
            assert allow_status.wait(timeout=5)
            return httpx.Response(200, json={"execution_status": "paused"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    runtime = OpenHandsAgentServerRuntime(
        client_factory=lambda supplied: _agent_server_client(supplied, handler),
        agent_profile_id_factory=lambda _job, _endpoint: PROFILE_ID,
        poll_interval_seconds=0.01,
    )

    async def scenario() -> None:
        execution = asyncio.create_task(
            runtime.execute(job, endpoint, "do work", root, evidence)
        )
        assert await asyncio.to_thread(status_started.wait, 2)
        cancellation = asyncio.create_task(runtime.cancel(job.job_id))
        assert await asyncio.to_thread(interrupt_seen.wait, 2)
        allow_status.set()
        assert await cancellation is True
        with pytest.raises(
            (OpenHandsAgentServerCompatibilityError, OpenHandsAgentServerExecutionCancelled)
        ):
            await execution

    _run(scenario())


def test_agent_server_snapshot_reader_removes_only_consistent_synthetic_manifest() -> None:
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


def test_agent_server_snapshot_reader_preserves_authored_archive_manifest() -> None:
    payload = _tar_snapshot({"archive_manifest.json": b"authored\n"})

    files = _read_snapshot_archive(
        io.BytesIO(payload),
        expected_root="nika-job",
        baseline_paths={"archive_manifest.json"},
    )

    assert files == (RemoteFile("archive_manifest.json", b"authored\n"),)


def test_agent_server_snapshot_reader_rejects_member_count_exhaustion() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for index in range(openhands_sdk_module._MAX_SNAPSHOT_MEMBERS + 1):
            member = tarfile.TarInfo(f"nika-job/dirs/{index}")
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
    buffer.seek(0)

    with pytest.raises(OpenHandsAgentServerCompatibilityError, match="member-count"):
        _read_snapshot_archive(
            buffer,
            expected_root="nika-job",
            baseline_paths=set(),
        )


def test_agent_server_snapshot_reader_rejects_symlink_member() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo("nika-job/src/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside"
        archive.addfile(member)
    buffer.seek(0)

    with pytest.raises(OpenHandsAgentServerCompatibilityError, match="non-regular"):
        _read_snapshot_archive(
            buffer,
            expected_root="nika-job",
            baseline_paths=set(),
        )
