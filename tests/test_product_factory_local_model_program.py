from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.model_gateway.contracts import (
    ModelResponse,
    ModelUsage,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.product_factory_local_coding import ContainedLocalCodingPolicy
from nika_core.product_factory_local_coding_planner import ModelGatewayLocalCodingPlanner
from nika_core.product_factory_local_model_program import (
    build_modelgateway_contained_local_coding_program,
)
from nika_core.toolsmith.contracts import ResourceBudget


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


def _repository(tmp_path: pathlib.Path) -> pathlib.Path:
    if shutil.which("git") is None:
        pytest.skip("Git CLI unavailable")
    root = tmp_path / "trusted repository"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Nika Test")
    _git(root, "config", "user.email", "nika@example.invalid")
    (root / "src").mkdir()
    (root / "src" / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "src/value.py")
    _git(root, "commit", "-m", "base")
    return root


class _Provider:
    def __init__(self) -> None:
        self.calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="local-planner",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    async def complete(self, request):
        self.calls += 1
        return ModelResponse(
            request_id=request.request_id,
            text=(
                '{"schema":"nika.local-coding-plan:v1","edits":'
                '[{"path":"src/value.py","content":"VALUE = 2\\n"}]}'
            ),
            provider_id="local-planner",
            provider_kind=ProviderKind.LOCAL,
            model=request.model or "planner-model",
            usage=ModelUsage(),
        )


def _policy() -> ContainedLocalCodingPolicy:
    python = str(pathlib.Path(sys.executable).resolve(strict=True))
    return ContainedLocalCodingPolicy(
        allowed_executables=(python,),
        resource_budget=ResourceBudget(30, 1024 * 1024, 20),
        lease_seconds=300,
    )


def test_builder_reuses_canonical_local_worker_program_and_modelgateway(
    tmp_path: pathlib.Path,
) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "local jobs"
    workspace.mkdir()
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    gateway = ModelGateway()
    provider = _Provider()
    gateway.register(provider)

    program = build_modelgateway_contained_local_coding_program(
        store,
        workspace_parent=workspace,
        repositories={"repo-1": repository},
        gateway=gateway,
        provider_id="local-planner",
        provider_kind=ProviderKind.LOCAL,
        model="planner-model",
        policy=_policy(),
    )

    assert program.host.store is store
    assert program.multi_repository_host.store is store
    assert program.multi_repository_host._program is program.host
    assert program.multi_repository_host.worker is program.host.worker
    assert program.host.worker.worker is program.worker
    assert program.worker.planner is not None
    assert isinstance(program.worker.planner, ModelGatewayLocalCodingPlanner)
    assert program.worker.planner.gateway is gateway
    assert program.worker.planner.provider_id == "local-planner"
    assert program.worker.planner.provider_kind is ProviderKind.LOCAL
    assert program.worker.planner.model == "planner-model"
    assert provider.calls == 0


def test_builder_fails_closed_without_explicit_model_route(
    tmp_path: pathlib.Path,
) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "local jobs"
    workspace.mkdir()
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    gateway = ModelGateway()
    gateway.register(_Provider())

    with pytest.raises(ValueError, match="explicit ModelGateway provider route"):
        build_modelgateway_contained_local_coding_program(
            store,
            workspace_parent=workspace,
            repositories={"repo-1": repository},
            gateway=gateway,
            provider_id=None,
            provider_kind=None,
            model="planner-model",
            policy=_policy(),
        )
