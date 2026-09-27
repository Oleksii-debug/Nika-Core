from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path

import httpx
import pytest

from nika_core.toolsmith.contracts import (
    AcceptanceCommand,
    AllowedPathPolicy,
    CodingJob,
    IsolationClass,
    NetworkPolicy,
    ProcessPolicy,
    RecoveryState,
    RepositorySnapshot,
    ResourceBudget,
    WorkerFailureKind,
    WorkspaceLease,
)
from nika_core.toolsmith.openhands_remote_worker import (
    OpenHandsRemoteCodingWorker,
    OpenHandsRemoteWorkerConfig,
    OpenHandsRemoteWorkerError,
    OpenHandsSandboxAttestation,
    PinnedOpenHandsSandboxAttestor,
)

SHA_A = "a" * 40
PROFILE_ID = "11111111-2222-3333-4444-555555555555"
DIGEST = "d" * 64


def _job(
    *,
    acceptance: tuple[AcceptanceCommand, ...] | None = None,
    timeout_seconds: int = 10,
) -> CodingJob:
    return CodingJob(
        job_id="work-1",
        task_id="task-1",
        goal="repair the bounded component",
        repository=RepositorySnapshot("repo-1", SHA_A, DIGEST),
        lease=WorkspaceLease(
            "lease-1",
            Path("/workspace/repo"),
            IsolationClass.REMOTE_SANDBOXED,
            "2026-09-27T12:00:00Z",
        ),
        allowed_paths=AllowedPathPolicy(("src",)),
        process_policy=ProcessPolicy(("python",)),
        network_policy=NetworkPolicy(),
        resource_budget=ResourceBudget(
            timeout_seconds=timeout_seconds,
            max_output_bytes=4096,
            max_changed_files=4,
        ),
        acceptance_commands=acceptance or (),
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )


def _attestation(job: CodingJob, **overrides: object) -> OpenHandsSandboxAttestation:
    values: dict[str, object] = {
        "lease_id": job.lease.lease_id,
        "repository_id": job.repository.repository_id,
        "base_sha": job.repository.base_sha,
        "tree_digest": job.repository.tree_digest,
        "workspace_root": job.lease.workspace_root.as_posix(),
        "isolation_class": job.lease.isolation_class,
        "expires_at": job.lease.expires_at,
        "allowed_paths": tuple(job.allowed_paths.roots),
        "process_policy": job.process_policy,
        "network_policy": job.network_policy,
        "resource_budget": job.resource_budget,
        "permission_ceiling": job.permission_ceiling,
    }
    values.update(overrides)
    return OpenHandsSandboxAttestation(**values)  # type: ignore[arg-type]


def _config(**overrides: object) -> OpenHandsRemoteWorkerConfig:
    values: dict[str, object] = {
        "base_url": "https://sandbox.example",
        "agent_profile_id": PROFILE_ID,
        "session_api_key": "secret-session-key",
        "poll_interval_seconds": 0.001,
    }
    values.update(overrides)
    return OpenHandsRemoteWorkerConfig(**values)  # type: ignore[arg-type]


def _factory(handler):
    transport = httpx.MockTransport(handler)

    def create(**kwargs):
        return httpx.AsyncClient(transport=transport, **kwargs)

    return create


def _json(request: httpx.Request) -> dict[str, object]:
    return json.loads(request.content.decode("utf-8"))


def _run(coro):
    return asyncio.run(coro)


def test_config_requires_authenticated_encrypted_remote_endpoint() -> None:
    with pytest.raises(OpenHandsRemoteWorkerError, match="X-Session-API-Key"):
        _config(session_api_key=None)
    with pytest.raises(OpenHandsRemoteWorkerError, match="prefer HTTPS"):
        _config(base_url="http://sandbox.example")

    loopback = _config(base_url="http://127.0.0.1:3000", session_api_key=None)
    assert loopback.base_url == "http://127.0.0.1:3000"


def test_success_uses_profile_not_model_secrets_and_returns_server_sourced_evidence() -> None:
    command = AcceptanceCommand(("python", "-c", "print('ok;still literal')"))
    job = _job(acceptance=(command,))
    calls = {"changes": 0, "conversation": 0}
    created_payload: dict[str, object] = {}
    bash_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Session-API-Key") == "secret-session-key"
        path = request.url.path
        if path == "/ready":
            return httpx.Response(200, json={"ready": True})
        if path == "/server_info":
            return httpx.Response(200, json={"version": "1.46.0"})
        if path == "/api/git/commits":
            return httpx.Response(200, json={"commits": [{"sha": SHA_A}], "has_more": False})
        if path == "/api/git/changes":
            calls["changes"] += 1
            if calls["changes"] == 1:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{"status": "UPDATED", "path": "src/core.py"}])
        if path == "/api/conversations" and request.method == "POST":
            created_payload.update(_json(request))
            return httpx.Response(
                201,
                json={
                    "id": created_payload["conversation_id"],
                    "workspace": {"working_dir": "/workspace/repo"},
                    "execution_status": "running",
                },
            )
        if path.startswith("/api/conversations/") and request.method == "GET":
            calls["conversation"] += 1
            status = "running" if calls["conversation"] == 1 else "finished"
            return httpx.Response(
                200,
                json={
                    "id": path.rsplit("/", 1)[-1],
                    "workspace": {"working_dir": "/workspace/repo"},
                    "execution_status": status,
                },
            )
        if path == "/api/file/download":
            return httpx.Response(200, content=b"VALUE = 2\n")
        if path == "/api/bash/execute_bash_command":
            bash_payload.update(_json(request))
            return httpx.Response(200, json={"exit_code": 0, "stdout": "ok\n", "stderr": ""})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    worker = OpenHandsRemoteCodingWorker(
        _config(),
        PinnedOpenHandsSandboxAttestor(_attestation(job)),
        client_factory=_factory(handler),
    )

    result = _run(worker.execute(job))

    assert result.succeeded
    assert result.recovery_state is not None
    assert result.recovery_state.phase == "finished"
    assert result.changed_files[0].path == "src/core.py"
    assert result.changed_files[0].sha256 == hashlib.sha256(b"VALUE = 2\n").hexdigest()
    assert result.test_evidence[0].command == command.argv
    assert result.test_evidence[0].exit_code == 0
    assert created_payload["agent_profile_id"] == PROFILE_ID
    assert "agent" not in created_payload
    assert "secrets" not in created_payload
    assert created_payload["autotitle"] is False
    initial_message = created_payload["initial_message"]
    assert isinstance(initial_message, dict)
    assert initial_message["run"] is True
    assert SHA_A in str(initial_message)
    assert "Allowed repository paths: src" in str(initial_message)
    assert bash_payload["command"] == "python -c 'print('\"'\"'ok;still literal'\"'\"')'"
    assert bash_payload["cwd"] == "/workspace/repo"


def test_dirty_pinned_workspace_fails_before_conversation_creation() -> None:
    job = _job()
    created = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal created
        if request.url.path == "/ready":
            return httpx.Response(200, json={})
        if request.url.path == "/server_info":
            return httpx.Response(200, json={})
        if request.url.path == "/api/git/commits":
            return httpx.Response(200, json={"commits": [{"sha": SHA_A}]})
        if request.url.path == "/api/git/changes":
            return httpx.Response(200, json=[{"status": "UPDATED", "path": "src/core.py"}])
        if request.url.path == "/api/conversations":
            created = True
        raise AssertionError(f"unexpected request {request.url}")

    worker = OpenHandsRemoteCodingWorker(
        _config(),
        PinnedOpenHandsSandboxAttestor(_attestation(job)),
        client_factory=_factory(handler),
    )
    result = _run(worker.execute(job))

    assert not result.succeeded
    assert result.failure is not None
    assert result.failure.kind is WorkerFailureKind.POLICY_VIOLATION
    assert result.recovery_state is None
    assert not created


def test_attestation_mismatch_fails_before_git_or_agent_execution() -> None:
    job = _job()
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/ready":
            return httpx.Response(200, json={})
        if request.url.path == "/server_info":
            return httpx.Response(200, json={})
        raise AssertionError("sandbox mismatch should stop before repository access")

    worker = OpenHandsRemoteCodingWorker(
        _config(),
        PinnedOpenHandsSandboxAttestor(_attestation(job, lease_id="wrong-lease")),
        client_factory=_factory(handler),
    )
    result = _run(worker.execute(job))

    assert result.failure is not None
    assert result.failure.kind is WorkerFailureKind.POLICY_VIOLATION
    assert "lease identity" in result.failure.message
    assert seen_paths == ["/ready", "/server_info"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"status": "DELETED", "path": "src/core.py"}, "DELETED"),
        ({"status": "UPDATED", "path": "../secret.txt"}, "escaped"),
        ({"status": "UPDATED", "path": "README.md"}, "outside"),
    ],
)
def test_unsafe_change_evidence_fails_closed(change: dict[str, str], message: str) -> None:
    job = _job()
    changes_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal changes_calls
        path = request.url.path
        if path == "/ready":
            return httpx.Response(200, json={})
        if path == "/server_info":
            return httpx.Response(200, json={})
        if path == "/api/git/commits":
            return httpx.Response(200, json={"commits": [{"sha": SHA_A}]})
        if path == "/api/git/changes":
            changes_calls += 1
            return httpx.Response(200, json=[] if changes_calls == 1 else [change])
        if path == "/api/conversations" and request.method == "POST":
            payload = _json(request)
            return httpx.Response(
                201,
                json={
                    "id": payload["conversation_id"],
                    "workspace": {"working_dir": "/workspace/repo"},
                    "execution_status": "running",
                },
            )
        if path.startswith("/api/conversations/"):
            return httpx.Response(
                200,
                json={
                    "id": path.rsplit("/", 1)[-1],
                    "workspace": {"working_dir": "/workspace/repo"},
                    "execution_status": "finished",
                },
            )
        raise AssertionError(f"unexpected request {request.url}")

    worker = OpenHandsRemoteCodingWorker(
        _config(),
        PinnedOpenHandsSandboxAttestor(_attestation(job)),
        client_factory=_factory(handler),
    )
    result = _run(worker.execute(job))

    assert result.failure is not None
    assert result.failure.kind is WorkerFailureKind.POLICY_VIOLATION
    assert message.casefold() in result.failure.message.casefold()


def test_creation_timeout_records_deterministic_recovery_identity_without_secret_leak() -> None:
    job = _job()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/ready":
            return httpx.Response(200, json={})
        if path == "/server_info":
            return httpx.Response(200, json={})
        if path == "/api/git/commits":
            return httpx.Response(200, json={"commits": [{"sha": SHA_A}]})
        if path == "/api/git/changes":
            return httpx.Response(200, json=[])
        if path == "/api/conversations":
            raise httpx.ReadTimeout("body contained secret-session-key", request=request)
        raise AssertionError(f"unexpected request {request.url}")

    worker = OpenHandsRemoteCodingWorker(
        _config(),
        PinnedOpenHandsSandboxAttestor(_attestation(job)),
        client_factory=_factory(handler),
    )
    result = _run(worker.execute(job))

    assert result.failure is not None
    assert result.failure.kind is WorkerFailureKind.TIMEOUT
    assert result.failure.retryable
    assert "secret-session-key" not in result.failure.message
    assert result.recovery_state is not None
    assert result.recovery_state.phase == "unknown"
    uuid.UUID(result.recovery_state.opaque_token or "")


def test_preflight_transport_timeout_is_not_falsely_recoverable() -> None:
    job = _job()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("offline", request=request)

    worker = OpenHandsRemoteCodingWorker(
        _config(),
        PinnedOpenHandsSandboxAttestor(_attestation(job)),
        client_factory=_factory(handler),
    )
    result = _run(worker.execute(job))

    assert result.failure is not None
    assert result.failure.kind is WorkerFailureKind.TIMEOUT
    assert result.recovery_state is None


def test_inspect_identity_survives_new_worker_instance() -> None:
    job = _job()
    requested_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        conversation_id = request.url.path.rsplit("/", 1)[-1]
        requested_ids.append(conversation_id)
        return httpx.Response(
            200,
            json={
                "id": conversation_id,
                "workspace": {"working_dir": "/workspace/repo"},
                "execution_status": "paused",
            },
        )

    attestor = PinnedOpenHandsSandboxAttestor(_attestation(job))
    first = OpenHandsRemoteCodingWorker(_config(), attestor, client_factory=_factory(handler))
    second = OpenHandsRemoteCodingWorker(_config(), attestor, client_factory=_factory(handler))

    state1 = _run(first.inspect(job.job_id))
    state2 = _run(second.inspect(job.job_id))

    assert state1 == state2
    assert state1 is not None
    assert state1.phase == "paused"
    assert requested_ids[0] == requested_ids[1] == state1.opaque_token


def test_recover_paused_conversation_revalidates_attestation_and_resumes() -> None:
    job = _job()
    expected_id: str | None = None
    run_called = False
    conversation_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal expected_id, run_called, conversation_reads
        path = request.url.path
        if path == "/ready":
            return httpx.Response(200, json={})
        if path == "/server_info":
            return httpx.Response(200, json={})
        if path.endswith("/run"):
            run_called = True
            return httpx.Response(200, json={"ok": True})
        if path.startswith("/api/conversations/"):
            conversation_reads += 1
            conversation_id = path.split("/")[3]
            expected_id = expected_id or conversation_id
            status = "paused" if conversation_reads == 1 else "finished"
            return httpx.Response(
                200,
                json={
                    "id": conversation_id,
                    "workspace": {"working_dir": "/workspace/repo"},
                    "execution_status": status,
                },
            )
        if path == "/api/git/changes":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    worker = OpenHandsRemoteCodingWorker(
        _config(),
        PinnedOpenHandsSandboxAttestor(_attestation(job)),
        client_factory=_factory(handler),
    )
    state = _run(worker.inspect(job.job_id))
    assert state is not None
    conversation_reads = 0
    result = _run(worker.recover(job, state))

    assert result.succeeded
    assert run_called
    assert result.recovery_state is not None
    assert result.recovery_state.phase == "finished"
    assert result.recovery_state.opaque_token == expected_id


def test_cancel_uses_deterministic_interrupt_endpoint() -> None:
    job = _job()
    interrupted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/interrupt")
        interrupted.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    worker = OpenHandsRemoteCodingWorker(
        _config(),
        PinnedOpenHandsSandboxAttestor(_attestation(job)),
        client_factory=_factory(handler),
    )
    _run(worker.cancel(job.job_id))

    assert len(interrupted) == 1
    uuid.UUID(interrupted[0].split("/")[-2])


def test_agent_run_timeout_interrupts_conversation_and_preserves_recovery_state() -> None:
    job = _job(timeout_seconds=2)
    now = [100.0]
    interrupted = False

    async def advance(_seconds: float) -> None:
        now[0] += 1.0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal interrupted
        path = request.url.path
        if path == "/ready":
            return httpx.Response(200, json={})
        if path == "/server_info":
            return httpx.Response(200, json={})
        if path == "/api/git/commits":
            return httpx.Response(200, json={"commits": [{"sha": SHA_A}]})
        if path == "/api/git/changes":
            return httpx.Response(200, json=[])
        if path == "/api/conversations" and request.method == "POST":
            payload = _json(request)
            return httpx.Response(
                201,
                json={
                    "id": payload["conversation_id"],
                    "workspace": {"working_dir": "/workspace/repo"},
                    "execution_status": "running",
                },
            )
        if path.endswith("/interrupt"):
            interrupted = True
            return httpx.Response(200, json={"success": True})
        if path.startswith("/api/conversations/"):
            return httpx.Response(
                200,
                json={
                    "id": path.rsplit("/", 1)[-1],
                    "workspace": {"working_dir": "/workspace/repo"},
                    "execution_status": "running",
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    worker = OpenHandsRemoteCodingWorker(
        _config(),
        PinnedOpenHandsSandboxAttestor(_attestation(job)),
        client_factory=_factory(handler),
        sleep=advance,
        clock=lambda: now[0],
    )
    result = _run(worker.execute(job))

    assert interrupted
    assert result.failure is not None
    assert result.failure.kind is WorkerFailureKind.TIMEOUT
    assert result.failure.retryable
    assert result.recovery_state is not None
    assert result.recovery_state.phase == "unknown"


def test_acceptance_command_candidate_mutation_fails_closed() -> None:
    command = AcceptanceCommand(("python", "-c", "print('ok')"))
    job = _job(acceptance=(command,))
    changes_calls = 0
    downloads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal changes_calls, downloads
        path = request.url.path
        if path == "/ready":
            return httpx.Response(200, json={})
        if path == "/server_info":
            return httpx.Response(200, json={})
        if path == "/api/git/commits":
            return httpx.Response(200, json={"commits": [{"sha": SHA_A}]})
        if path == "/api/git/changes":
            changes_calls += 1
            if changes_calls == 1:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{"status": "UPDATED", "path": "src/core.py"}])
        if path == "/api/conversations" and request.method == "POST":
            payload = _json(request)
            return httpx.Response(
                201,
                json={
                    "id": payload["conversation_id"],
                    "workspace": {"working_dir": "/workspace/repo"},
                    "execution_status": "running",
                },
            )
        if path.startswith("/api/conversations/"):
            return httpx.Response(
                200,
                json={
                    "id": path.rsplit("/", 1)[-1],
                    "workspace": {"working_dir": "/workspace/repo"},
                    "execution_status": "finished",
                },
            )
        if path == "/api/file/download":
            downloads += 1
            value = b"VALUE = 2\n" if downloads == 1 else b"VALUE = 3\n"
            return httpx.Response(200, content=value)
        if path == "/api/bash/execute_bash_command":
            return httpx.Response(200, json={"exit_code": 0, "stdout": "ok\n", "stderr": ""})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    worker = OpenHandsRemoteCodingWorker(
        _config(),
        PinnedOpenHandsSandboxAttestor(_attestation(job)),
        client_factory=_factory(handler),
    )
    result = _run(worker.execute(job))

    assert result.failure is not None
    assert result.failure.kind is WorkerFailureKind.POLICY_VIOLATION
    assert "mutated the candidate tree" in result.failure.message
    assert result.changed_files[0].sha256 == hashlib.sha256(b"VALUE = 3\n").hexdigest()


def test_pinned_server_build_sha_is_verified_from_current_server_info_field() -> None:
    job = _job()
    expected_build = "b" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ready":
            return httpx.Response(200, json={})
        if request.url.path == "/server_info":
            return httpx.Response(200, json={"build_git_sha": "c" * 40})
        raise AssertionError("build mismatch must stop before repository access")

    worker = OpenHandsRemoteCodingWorker(
        _config(),
        PinnedOpenHandsSandboxAttestor(
            _attestation(job, server_build_sha=expected_build)
        ),
        client_factory=_factory(handler),
    )
    result = _run(worker.execute(job))

    assert result.failure is not None
    assert result.failure.kind is WorkerFailureKind.POLICY_VIOLATION
    assert "build identity mismatch" in result.failure.message


def test_recover_transport_failure_keeps_deterministic_recovery_state() -> None:
    job = _job()
    token = str(uuid.uuid5(uuid.UUID("bca670e4-daf4-4a84-b06e-85e9ae48408f"), job.job_id))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ready":
            return httpx.Response(200, json={})
        if request.url.path == "/server_info":
            return httpx.Response(200, json={})
        if request.url.path.startswith("/api/conversations/"):
            raise httpx.ReadTimeout("temporary remote outage", request=request)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    worker = OpenHandsRemoteCodingWorker(
        _config(),
        PinnedOpenHandsSandboxAttestor(_attestation(job)),
        client_factory=_factory(handler),
    )
    result = _run(worker.recover(job, RecoveryState("paused", token)))

    assert result.failure is not None
    assert result.failure.kind is WorkerFailureKind.TIMEOUT
    assert result.failure.retryable
    assert result.recovery_state is not None
    assert result.recovery_state.phase == "unknown"
    assert result.recovery_state.opaque_token == token
