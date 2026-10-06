from __future__ import annotations

import pathlib
import sys

import httpx
import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_openhands_program import (
    OpenHandsProductFactoryError,
    OpenHandsProductFactoryPolicy,
)
from nika_core.product_factory_openhands_runtime_api import (
    OpenHandsRuntimeApiConfig,
    OpenHandsRuntimeApiSandboxProvider,
)
from nika_core.product_factory_openhands_runtime_program import (
    build_openhands_runtime_api_product_factory_program,
)
from nika_core.toolsmith.contracts import ResourceBudget


class _AcceptanceRuntime:
    async def execute(self, job, candidate_files, candidate_evidence):
        raise AssertionError((job.job_id, candidate_files, candidate_evidence))

    async def cancel(self, job_id):
        raise AssertionError(job_id)


def _config() -> OpenHandsRuntimeApiConfig:
    return OpenHandsRuntimeApiConfig(
        runtime_api_url="http://127.0.0.1:3000",
        server_image=(
            "ghcr.io/openhands/agent-server@sha256:"
            "1111111111111111111111111111111111111111111111111111111111111111"
        ),
        working_dir="/workspace/nika-job",
        agent_server_hosts=("127.0.0.1",),
        sandbox_egress_hosts=("models.example.test",),
        network_policy_enforced=True,
        runtime_class=None,
    )


def _policy(*, include_egress: bool = True) -> OpenHandsProductFactoryPolicy:
    hosts = ("127.0.0.1", "models.example.test") if include_egress else ("127.0.0.1",)
    return OpenHandsProductFactoryPolicy(
        allowed_executables=(str(pathlib.Path(sys.executable).resolve(strict=True)),),
        approved_hosts=hosts,
        resource_budget=ResourceBudget(30, 1024 * 1024, 20),
        lease_seconds=300,
    )


def _program(tmp_path: pathlib.Path, *, policy: OpenHandsProductFactoryPolicy):
    repository = tmp_path / "trusted repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    workspace = tmp_path / "OpenHands jobs"
    workspace.mkdir()
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    config = _config()

    def control_client() -> httpx.Client:
        return httpx.Client(
            base_url=config.runtime_api_url,
            headers={"X-API-Key": "test-runtime-secret"},
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(500, json={"detail": "unused"})
            ),
        )

    def agent_client(base_url: str, session_key: str) -> httpx.Client:
        return httpx.Client(
            base_url=base_url,
            headers={"X-Session-API-Key": session_key},
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"status": "ok"})
            ),
        )

    program = build_openhands_runtime_api_product_factory_program(
        store,
        workspace_parent=workspace,
        repositories={"repo-1": repository},
        runtime_api_config=config,
        control_client_factory=control_client,
        agent_client_factory=agent_client,
        agent_profile_id_factory=lambda _job, _endpoint: (
            "11111111-1111-4111-8111-111111111111"
        ),
        acceptance_runtime=_AcceptanceRuntime(),
        policy=policy,
    )
    return store, program


def test_runtime_api_composition_binds_one_provider_to_sandbox_and_agent_auth(
    tmp_path: pathlib.Path,
) -> None:
    store, program = _program(tmp_path, policy=_policy())

    provider = program.worker._sandbox_provider
    assert isinstance(provider, OpenHandsRuntimeApiSandboxProvider)
    assert program.host.store is store
    assert program.multi_repository_host.store is store
    assert program.multi_repository_host._program is program.host
    assert program.multi_repository_host.worker is program.host.worker
    assert program.host.worker.worker is program.worker
    assert program.runtime._client_factory.__self__ is provider
    assert program.runtime._client_factory.__func__ is OpenHandsRuntimeApiSandboxProvider.client_for


def test_runtime_api_composition_rejects_policy_host_gap_before_client_use(
    tmp_path: pathlib.Path,
) -> None:
    repository = tmp_path / "trusted repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    workspace = tmp_path / "OpenHands jobs"
    workspace.mkdir()
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    config = _config()
    client_calls = 0

    def control_client() -> httpx.Client:
        nonlocal client_calls
        client_calls += 1
        return httpx.Client(
            base_url=config.runtime_api_url,
            headers={"X-API-Key": "test-runtime-secret"},
        )

    with pytest.raises(OpenHandsProductFactoryError, match="missing approved hosts") as caught:
        build_openhands_runtime_api_product_factory_program(
            store,
            workspace_parent=workspace,
            repositories={"repo-1": repository},
            runtime_api_config=config,
            control_client_factory=control_client,
            agent_profile_id_factory=lambda _job, _endpoint: (
                "11111111-1111-4111-8111-111111111111"
            ),
            acceptance_runtime=_AcceptanceRuntime(),
            policy=_policy(include_egress=False),
        )

    assert "models.example.test" in str(caught.value)
    assert client_calls == 0


def test_runtime_api_composition_rejects_config_substitution(
    tmp_path: pathlib.Path,
) -> None:
    class DerivedConfig(OpenHandsRuntimeApiConfig):
        pass

    repository = tmp_path / "trusted repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    workspace = tmp_path / "OpenHands jobs"
    workspace.mkdir()
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    base = _config()
    derived = DerivedConfig(
        runtime_api_url=base.runtime_api_url,
        server_image=base.server_image,
        working_dir=base.working_dir,
        agent_server_hosts=base.agent_server_hosts,
        sandbox_egress_hosts=base.sandbox_egress_hosts,
        network_policy_enforced=base.network_policy_enforced,
        runtime_class=None,
    )

    with pytest.raises(OpenHandsProductFactoryError, match="exact config carrier"):
        build_openhands_runtime_api_product_factory_program(
            store,
            workspace_parent=workspace,
            repositories={"repo-1": repository},
            runtime_api_config=derived,
            control_client_factory=lambda: httpx.Client(base_url=base.runtime_api_url),
            agent_profile_id_factory=lambda _job, _endpoint: (
                "11111111-1111-4111-8111-111111111111"
            ),
            acceptance_runtime=_AcceptanceRuntime(),
            policy=_policy(),
        )


def test_workflows_route_runtime_api_program_composition_regression() -> None:
    root = pathlib.Path(__file__).resolve().parents[1]
    pf11 = (root / ".github" / "workflows" / "pf11-openhands-backend.yml").read_text(
        encoding="utf-8"
    )
    m11 = (root / ".github" / "workflows" / "m11-windows-release.yml").read_text(
        encoding="utf-8"
    )
    source_path = "src/nika_core/product_factory_openhands_runtime_program.py"
    test_path = "tests/test_product_factory_openhands_runtime_program.py"

    assert source_path in pf11
    assert test_path in pf11
    assert test_path in m11
