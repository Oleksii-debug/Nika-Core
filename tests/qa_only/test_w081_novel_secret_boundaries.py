from __future__ import annotations

import asyncio
import json
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.packaging.release import build_release_manifest, verify_release_manifest
from nika_core.product_factory_checkpoint_host import ProductFactoryCheckpointHost
from nika_core.product_factory_coordinator import WorkerResultEnvelope
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)
from nika_core.toolsmith.contracts import CodingResult, TestEvidence

_SOURCE_SHA = "a" * 40
_RESULT_SHA = "b" * 40
_DIGEST = "d" * 64
_PACKAGE_CANARY = "NIKA_QA_W081_PACKAGE_CONTENT_CANARY_7ef390"
_CHECKPOINT_CANARY = "NIKA_QA_W081_CHECKPOINT_ARGV_CANARY_91bc42"
_MODEL_CANARY = "NIKA_QA_W081_MODEL_ID_CANARY_c047ad"


def test_release_manifest_fails_closed_on_secret_content_under_benign_filename(
    tmp_path: Path,
) -> None:
    """QA_ONLY: benign package paths must not bless raw credential-like content."""

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"MZ-QA-W081-placeholder")
    (bundle / "config.json").write_text(
        json.dumps({"api_key": _PACKAGE_CANARY}),
        encoding="utf-8",
    )

    manifest = build_release_manifest(
        bundle,
        product="NikaCore",
        version="0.0.2",
        source_sha=_SOURCE_SHA,
    )
    findings = verify_release_manifest(bundle, manifest)

    assert findings, (
        "release manifest accepted and blessed a synthetic secret canary stored "
        "under a benign package filename"
    )


class _CanaryModelProvider:
    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="qa-w081-local",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            request_id=request.request_id,
            text="synthetic response",
            provider_id=self.capabilities.provider_id,
            provider_kind=self.capabilities.kind,
            model=request.model or "default",
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


def test_model_gateway_success_audit_does_not_persist_credential_bearing_model_id(
    tmp_path: Path,
) -> None:
    """QA_ONLY: successful ModelGateway metadata must not persist a raw canary."""

    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    audit = AuditLog(store)
    gateway = ModelGateway(audit_log=audit)
    gateway.register(_CanaryModelProvider(), default=True)
    model_id = f"https://models.invalid/v1/model?api_key={_MODEL_CANARY}"
    request = ModelRequest(
        request_id="qa-w081-model",
        messages=(ModelMessage("user", "synthetic prompt"),),
        model=model_id,
        provider_kind=ProviderKind.LOCAL,
        privacy=PrivacyClass.PRIVATE,
    )

    asyncio.run(gateway.complete(request))
    events = audit.list_for(entity_type="model_request", entity_id=request.request_id)
    durable = json.dumps(
        [event.payload for event in events],
        ensure_ascii=False,
        sort_keys=True,
    )

    assert _MODEL_CANARY not in durable, (
        "successful ModelGateway audit persisted a synthetic credential-bearing "
        "model identifier"
    )


def _checkpoint_binding(tmp_path: Path):
    store = SQLiteStore(tmp_path / "checkpoint.db")
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id="qa-w081-project",
        name="QA W081",
        spec=ProductProjectSpec(
            goal="Synthetic checkpoint evidence test",
            desired_outcome="No secret canary in durable checkpoint bytes",
            requirements=(
                ProductRequirement(
                    "req-1",
                    "Durable worker evidence",
                    ("Checkpoint survives restart without secret material",),
                ),
            ),
            repository_refs=("org/repo",),
        ),
        idempotency_key="qa-w081:create",
    )
    graph = ProductRepositoryGraph(
        project_id=project.project_id,
        repositories=(RepositoryRef("repo-1", "github", "org/repo", "main"),),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-1",
                paths=("src/core",),
                test_commands=(("python", "-m", "pytest", "tests/core"),),
            ),
        ),
    )
    binding = ProductProjectCoordinatorBinding(project, graph)
    coordinator = binding.plan(
        base_shas={"repo-1": _SOURCE_SHA},
        component_goals={"core": "synthetic QA work"},
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )
    task = TaskQueue(store).create(
        workspace_id="qa-w081",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": project.project_id,
        },
    )
    return store, binding, coordinator, task.task_id


def test_pf12_checkpoint_does_not_persist_worker_test_command_secret_canary(
    tmp_path: Path,
) -> None:
    """QA_ONLY: worker TestEvidence argv must be minimized before PF12 persistence."""

    store, binding, coordinator, task_id = _checkpoint_binding(tmp_path)
    request = coordinator.start("core")
    result = CodingResult(
        job_id=request.work_id,
        changed_files=(),
        test_evidence=(
            TestEvidence(
                ("python", "-m", "pytest", "--api-key", _CHECKPOINT_CANARY),
                0,
                _DIGEST,
            ),
        ),
        artifacts=(),
    )
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=_RESULT_SHA,
            diff_digest=_DIGEST,
            coding_result=result,
        )
    )

    saved = ProductFactoryCheckpointHost(store).save(
        host_task_id=task_id,
        checkpoint=binding.checkpoint(coordinator),
    )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM checkpoints WHERE checkpoint_id=?",
            (saved.checkpoint_id,),
        ).fetchone()

    assert row is not None
    assert _CHECKPOINT_CANARY not in row["payload_json"], (
        "PF12 persisted a synthetic secret canary from worker TestEvidence.command"
    )
