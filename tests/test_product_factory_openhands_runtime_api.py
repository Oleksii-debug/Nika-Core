from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import httpx
import pytest

from nika_core.product_factory_openhands_runtime_api import (
    OpenHandsRuntimeApiConfig,
    OpenHandsRuntimeApiError,
    OpenHandsRuntimeApiSandboxProvider,
)
from nika_core.toolsmith.contracts import (
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
from nika_core.toolsmith.openhands_remote_worker import OpenHandsSandboxProviderPort

_RUNTIME_SECRET = "runtime-secret-never-persist"
_SESSION_SECRET = "session-secret-never-persist"


def _run(coroutine):
    return asyncio.run(coroutine)


def _config(**overrides) -> OpenHandsRuntimeApiConfig:
    values = {
        "runtime_api_url": "https://runtime.example.test",
        "server_image": "ghcr.io/openhands/agent-server:1.49.2-python",
        "working_dir": "/workspace/nika-job",
        "agent_server_hosts": ("agent.example.test",),
        "sandbox_egress_hosts": ("models.example.test",),
        "network_policy_enforced": True,
        "poll_interval_seconds": 0.001,
    }
    values.update(overrides)
    return OpenHandsRuntimeApiConfig(**values)


def _job(
    tmp_path: pathlib.Path,
    *,
    approved_hosts: tuple[str, ...] = (
        "runtime.example.test",
        "agent.example.test",
        "models.example.test",
    ),
) -> CodingJob:
    return CodingJob(
        job_id="job-1",
        task_id="task-1",
        goal="update the allowed source",
        repository=RepositorySnapshot("repo-1", "a" * 40, "b" * 64),
        lease=WorkspaceLease(
            "lease-1",
            tmp_path,
            IsolationClass.PROCESS_CONTAINED,
            "2099-01-01T00:00:00+00:00",
        ),
        allowed_paths=AllowedPathPolicy(("src",)),
        process_policy=ProcessPolicy((sys.executable,)),
        network_policy=NetworkPolicy(NetworkMode.APPROVED_HOSTS, approved_hosts),
        resource_budget=ResourceBudget(30, 1024 * 1024, 8),
        acceptance_commands=(),
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )


class _RuntimeApi:
    def __init__(self) -> None:
        self.runtime_id = "runtime-1"
        self.agent_url = "https://agent.example.test"
        self.session_id: str | None = None
        self.status = "running"
        self.exists = True
        self.fail_start = False
        self.requests: list[tuple[str, str, dict[str, object] | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, payload))

        if request.method == "POST" and request.url.path == "/start":
            if self.fail_start:
                return httpx.Response(
                    500,
                    text=f"do not expose {_RUNTIME_SECRET} {_SESSION_SECRET}",
                )
            assert payload is not None
            self.session_id = str(payload["session_id"])
            self.exists = True
            self.status = "running"
            return httpx.Response(
                200,
                json={
                    "runtime_id": self.runtime_id,
                    "url": self.agent_url,
                    "session_api_key": _SESSION_SECRET,
                },
            )

        if request.method == "GET" and request.url.path.startswith("/sessions/"):
            requested = request.url.path.removeprefix("/sessions/")
            if not self.exists or requested != self.session_id:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(
                200,
                json={
                    "session_id": self.session_id,
                    "runtime_id": self.runtime_id,
                    "url": self.agent_url,
                    "session_api_key": _SESSION_SECRET,
                    "status": self.status,
                    "pod_status": "ready" if self.status == "running" else self.status,
                },
            )

        if request.method == "POST" and request.url.path == "/pause":
            assert payload == {"runtime_id": self.runtime_id}
            self.status = "paused"
            return httpx.Response(200, json={"ok": True})

        if request.method == "POST" and request.url.path == "/resume":
            assert payload == {"runtime_id": self.runtime_id}
            self.status = "running"
            return httpx.Response(200, json={"ok": True})

        if request.method == "POST" and request.url.path == "/stop":
            assert payload == {"runtime_id": self.runtime_id}
            self.exists = False
            self.status = "stopped"
            return httpx.Response(200, json={"ok": True})

        raise AssertionError((request.method, request.url, payload))


def _factory(api: _RuntimeApi, config: OpenHandsRuntimeApiConfig):
    transport = httpx.MockTransport(api)

    def create() -> httpx.Client:
        return httpx.Client(
            base_url=config.runtime_api_url,
            headers={"X-API-Key": _RUNTIME_SECRET},
            transport=transport,
        )

    return create


def test_config_requires_https_attested_network_and_pinned_image() -> None:
    with pytest.raises(ValueError, match="requires HTTPS"):
        _config(runtime_api_url="http://runtime.example.test")
    with pytest.raises(ValueError, match="attest enforced"):
        _config(network_policy_enforced=False)
    with pytest.raises(ValueError, match="latest"):
        _config(server_image="ghcr.io/openhands/agent-server:latest")


def test_provider_rejects_unapproved_runtime_host_before_external_effect(
    tmp_path: pathlib.Path,
) -> None:
    config = _config()
    api = _RuntimeApi()
    provider = OpenHandsRuntimeApiSandboxProvider(
        config,
        control_client_factory=_factory(api, config),
    )
    job = _job(
        tmp_path,
        approved_hosts=("agent.example.test", "models.example.test"),
    )

    with pytest.raises(OpenHandsRuntimeApiError, match="not approved"):
        _run(provider.acquire(job))

    assert api.requests == []


def test_acquire_returns_secret_free_attested_endpoint_and_redeems_client(
    tmp_path: pathlib.Path,
) -> None:
    config = _config()
    api = _RuntimeApi()
    provider = OpenHandsRuntimeApiSandboxProvider(
        config,
        control_client_factory=_factory(api, config),
    )

    endpoint = _run(provider.acquire(_job(tmp_path)))

    assert isinstance(provider, OpenHandsSandboxProviderPort)
    assert endpoint.endpoint_id == api.session_id
    assert endpoint.host == api.agent_url
    assert endpoint.working_dir == "/workspace/nika-job"
    assert endpoint.isolation_class is IsolationClass.REMOTE_SANDBOXED
    assert endpoint.sandbox_egress_hosts == ("models.example.test",)
    assert endpoint.network_policy_enforced is True
    assert endpoint.fresh_workspace is True
    assert _RUNTIME_SECRET not in repr(endpoint)
    assert _SESSION_SECRET not in repr(endpoint)

    start_payload = next(
        payload
        for method, path, payload in api.requests
        if method == "POST" and path == "/start"
    )
    assert start_payload is not None
    assert start_payload["environment"] == {}
    assert _RUNTIME_SECRET not in json.dumps(start_payload)
    assert _SESSION_SECRET not in json.dumps(start_payload)

    client = provider.client_for(endpoint)
    try:
        assert str(client.base_url).rstrip("/") == endpoint.host
        assert client.headers["X-Session-API-Key"] == _SESSION_SECRET
    finally:
        client.close()


def test_failed_work_pauses_runtime_and_fresh_provider_can_resume_recovery(
    tmp_path: pathlib.Path,
) -> None:
    config = _config()
    api = _RuntimeApi()
    first = OpenHandsRuntimeApiSandboxProvider(
        config,
        control_client_factory=_factory(api, config),
    )
    job = _job(tmp_path)
    endpoint = _run(first.acquire(job))

    _run(first.release(job, endpoint, succeeded=False))
    assert api.status == "paused"
    assert api.exists is True

    second = OpenHandsRuntimeApiSandboxProvider(
        config,
        control_client_factory=_factory(api, config),
    )
    client = second.client_for(endpoint)
    try:
        assert api.status == "running"
        assert client.headers["X-Session-API-Key"] == _SESSION_SECRET
    finally:
        client.close()

    assert any(method == "POST" and path == "/resume" for method, path, _ in api.requests)


def test_successful_work_stops_runtime(tmp_path: pathlib.Path) -> None:
    config = _config()
    api = _RuntimeApi()
    provider = OpenHandsRuntimeApiSandboxProvider(
        config,
        control_client_factory=_factory(api, config),
    )
    job = _job(tmp_path)
    endpoint = _run(provider.acquire(job))

    _run(provider.release(job, endpoint, succeeded=True))

    assert api.exists is False
    assert any(method == "POST" and path == "/stop" for method, path, _ in api.requests)


def test_recovery_fails_closed_if_bound_agent_server_url_changes(
    tmp_path: pathlib.Path,
) -> None:
    config = _config()
    api = _RuntimeApi()
    provider = OpenHandsRuntimeApiSandboxProvider(
        config,
        control_client_factory=_factory(api, config),
    )
    endpoint = _run(provider.acquire(_job(tmp_path)))
    api.agent_url = "https://other.example.test"

    with pytest.raises(OpenHandsRuntimeApiError, match="URL changed"):
        provider.client_for(endpoint)


def test_untrusted_agent_server_host_is_stopped_before_acquire_returns(
    tmp_path: pathlib.Path,
) -> None:
    config = _config()
    api = _RuntimeApi()
    api.agent_url = "https://evil.example.test"
    provider = OpenHandsRuntimeApiSandboxProvider(
        config,
        control_client_factory=_factory(api, config),
    )

    with pytest.raises(OpenHandsRuntimeApiError, match="trusted Runtime API configuration"):
        _run(provider.acquire(_job(tmp_path)))

    assert api.exists is False
    assert any(method == "POST" and path == "/stop" for method, path, _ in api.requests)


def test_control_plane_failure_does_not_echo_secret_response_body(
    tmp_path: pathlib.Path,
) -> None:
    config = _config()
    api = _RuntimeApi()
    api.fail_start = True
    provider = OpenHandsRuntimeApiSandboxProvider(
        config,
        control_client_factory=_factory(api, config),
    )

    with pytest.raises(OpenHandsRuntimeApiError) as caught:
        _run(provider.acquire(_job(tmp_path)))

    message = str(caught.value)
    assert _RUNTIME_SECRET not in message
    assert _SESSION_SECRET not in message
    assert "HTTP 500" in message


def test_control_client_must_be_authenticated(tmp_path: pathlib.Path) -> None:
    config = _config()
    api = _RuntimeApi()
    transport = httpx.MockTransport(api)

    def unauthenticated() -> httpx.Client:
        return httpx.Client(base_url=config.runtime_api_url, transport=transport)

    provider = OpenHandsRuntimeApiSandboxProvider(
        config,
        control_client_factory=unauthenticated,
    )

    with pytest.raises(OpenHandsRuntimeApiError, match="lacks X-API-Key"):
        _run(provider.acquire(_job(tmp_path)))

    assert api.requests == []


def test_workflows_route_and_execute_runtime_api_provider_regression() -> None:
    root = pathlib.Path(__file__).resolve().parents[1]
    pf11 = (root / ".github" / "workflows" / "pf11-openhands-backend.yml").read_text(
        encoding="utf-8"
    )
    m11 = (root / ".github" / "workflows" / "m11-windows-release.yml").read_text(
        encoding="utf-8"
    )
    source_path = "src/nika_core/product_factory_openhands_runtime_api.py"
    test_path = "tests/test_product_factory_openhands_runtime_api.py"

    assert source_path in pf11
    assert pf11.count(test_path) >= 3
    assert m11.count(f'- "{test_path}"') == 2
    assert m11.count(f"          {test_path}") == 1
