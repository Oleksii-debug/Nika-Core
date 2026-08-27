from __future__ import annotations

import hashlib
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import ProductFactoryCheckpointHost
from nika_core.product_factory_coordinator import ReviewDecision, WorkerResultEnvelope
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
_DIFF_DIGEST = "d" * 64
_TEST_DIGEST = "e" * 64
_LOCATOR = "org/pf12-evidence"
_ACCEPTANCE_COMMAND = ("python", "-m", "pytest", "tests/core")
_PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
_REVIEW_OMITTED = "review rationale omitted from durable checkpoint"


def _setup(tmp_path: Path):
    store = SQLiteStore(tmp_path / "pf12-evidence.db")
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id="pf12-evidence-project",
        name="PF12 durable evidence",
        spec=ProductProjectSpec(
            goal="Persist only authority-relevant safe evidence",
            desired_outcome="Worker and review diagnostics cannot leak into checkpoints",
            requirements=(
                ProductRequirement(
                    "req-evidence",
                    "Durable evidence follows declared acceptance authority",
                    ("Extra worker evidence and credential assignments are minimized",),
                ),
            ),
            repository_refs=(_LOCATOR,),
        ),
        idempotency_key="pf12:evidence:create",
    )
    graph = ProductRepositoryGraph(
        project_id=project.project_id,
        repositories=(RepositoryRef("repo-1", "github", _LOCATOR, "main"),),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-1",
                paths=("src/core",),
                test_commands=(_ACCEPTANCE_COMMAND,),
            ),
        ),
    )
    binding = ProductProjectCoordinatorBinding(project, graph)
    coordinator = binding.plan(
        base_shas={"repo-1": _SOURCE_SHA},
        component_goals={"core": "build core"},
        permission_ceiling=_PERMISSIONS,
    )
    task = TaskQueue(store).create(
        workspace_id="pf12-evidence",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": project.project_id,
        },
    )
    return store, binding, coordinator, task.task_id


def _record_success(coordinator, *, extra_command=None) -> None:
    request = coordinator.start("core")
    evidence = [TestEvidence(_ACCEPTANCE_COMMAND, 0, _TEST_DIGEST)]
    if extra_command is not None:
        evidence.append(TestEvidence(extra_command, 0, _TEST_DIGEST))
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=_RESULT_SHA,
            diff_digest=_DIFF_DIGEST,
            coding_result=CodingResult(
                job_id=request.work_id,
                test_evidence=tuple(evidence),
            ),
        )
    )


def _raw_checkpoint(store: SQLiteStore, checkpoint_id: str) -> str:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM checkpoints WHERE checkpoint_id = ?",
            (checkpoint_id,),
        ).fetchone()
    assert row is not None
    return str(row["payload_json"])


def _digest_ref(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def test_extra_worker_test_evidence_is_not_durable_authority(tmp_path: Path) -> None:
    canary = "NIKA_EXTRA_WORKER_TEST_CANARY_77D1"
    extra = ("python", "checks.py", "--api-key", canary)
    store, binding, coordinator, task_id = _setup(tmp_path)
    _record_success(coordinator, extra_command=extra)

    runtime = coordinator.snapshot().records[0]
    assert runtime.result is not None
    assert tuple(item.command for item in runtime.result.coding_result.test_evidence) == (
        _ACCEPTANCE_COMMAND,
        extra,
    )

    checkpoint = binding.checkpoint(coordinator)
    durable = checkpoint.coordinator.records[0]
    assert durable.result is not None
    assert tuple(item.command for item in durable.result.coding_result.test_evidence) == (
        _ACCEPTANCE_COMMAND,
    )

    host = ProductFactoryCheckpointHost(store)
    saved = host.save(host_task_id=task_id, checkpoint=checkpoint)
    assert canary not in _raw_checkpoint(store, saved.checkpoint_id)

    restored = host.restore_latest(host_task_id=task_id, binding=binding)
    restored_record = restored.snapshot().records[0]
    assert restored_record.result is not None
    assert tuple(
        item.command for item in restored_record.result.coding_result.test_evidence
    ) == (_ACCEPTANCE_COMMAND,)


def test_nested_and_aws_review_credentials_are_minimized_without_safe_ref_loss(
    tmp_path: Path,
) -> None:
    nested_canary = "NIKA_NESTED_API_KEY_CANARY_88E2"
    aws_canary = "NIKA_AWS_SECRET_CANARY_99F3"
    nested_ref = (
        "https://example.invalid/review?api%255Fkey=" + nested_canary
    )
    aws_ref = (
        "https://example.invalid/review?aws_secret_access_key=" + aws_canary
    )
    safe_ref = "tests://core/pass"
    reason = f"verified; aws_access_key_id={aws_canary}"

    store, binding, coordinator, task_id = _setup(tmp_path)
    _record_success(coordinator)
    coordinator.review(
        "core",
        ReviewDecision(
            reviewer_id="independent-qa",
            accepted=True,
            reason=reason,
            evidence_refs=(nested_ref, aws_ref, safe_ref),
        ),
    )

    runtime = coordinator.snapshot().records[0]
    assert runtime.review is not None
    assert runtime.review.reason == reason
    assert runtime.review.evidence_refs == (nested_ref, aws_ref, safe_ref)

    checkpoint = binding.checkpoint(coordinator)
    durable = checkpoint.coordinator.records[0]
    assert durable.review is not None
    assert durable.review.reason == _REVIEW_OMITTED
    assert durable.review.evidence_refs == (
        _digest_ref(nested_ref),
        _digest_ref(aws_ref),
        safe_ref,
    )

    host = ProductFactoryCheckpointHost(store)
    saved = host.save(host_task_id=task_id, checkpoint=checkpoint)
    raw = _raw_checkpoint(store, saved.checkpoint_id)
    assert nested_canary not in raw
    assert aws_canary not in raw
    assert safe_ref in raw

    restored = host.restore_latest(host_task_id=task_id, binding=binding)
    assert restored.snapshot().records[0].review == durable.review
