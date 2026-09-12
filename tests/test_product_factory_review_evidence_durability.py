from __future__ import annotations

import hashlib

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import ProductFactoryCheckpointHost
from nika_core.product_factory_coordinator import ReviewDecision, WorkerResultEnvelope, WorkState
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

SHA_A = "a" * 40
SHA_B = "b" * 40
DIFF_DIGEST = "d" * 64
TEST_DIGEST = "e" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
ACCEPTANCE_COMMAND = ("python", "-m", "pytest", "tests/core")
DURABLE_REVIEW_REASON = "review rationale omitted from durable checkpoint"


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "pf12-review-evidence.db")
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id="pf12-review-project",
        name="PF12 review evidence",
        spec=ProductProjectSpec(
            goal="Build a safe product",
            desired_outcome="Keep reviewer diagnostics out of durable checkpoints",
            requirements=(
                ProductRequirement(
                    "req-review",
                    "review evidence stays safe",
                    ("checkpoint bytes exclude reviewer credential material",),
                ),
            ),
            repository_refs=("org/repo",),
        ),
        idempotency_key="pf12:review:create",
    )
    graph = ProductRepositoryGraph(
        project_id=project.project_id,
        repositories=(RepositoryRef("repo-1", "github", "org/repo", "main"),),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-1",
                paths=("src/core",),
                test_commands=(ACCEPTANCE_COMMAND,),
            ),
        ),
    )
    binding = ProductProjectCoordinatorBinding(project, graph)
    coordinator = binding.plan(
        base_shas={"repo-1": SHA_A},
        component_goals={"core": "build core"},
        permission_ceiling=PERMISSIONS,
    )
    task = TaskQueue(store).create(
        workspace_id="pf12-review-workspace",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": project.project_id,
        },
    )
    request = coordinator.start("core")
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=SHA_B,
            diff_digest=DIFF_DIGEST,
            coding_result=CodingResult(
                job_id=request.work_id,
                test_evidence=(
                    TestEvidence(
                        command=ACCEPTANCE_COMMAND,
                        exit_code=0,
                        output_digest=TEST_DIGEST,
                    ),
                ),
            ),
        )
    )
    return store, binding, coordinator, task.task_id


def _digest_ref(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def test_accepted_review_durable_projection_preserves_identity_not_raw_diagnostics(
    tmp_path,
) -> None:
    reason = "verified; Authorization: Bearer OWNER_REVIEW_CANARY"
    evidence_ref = "https://reviewer:OWNER_REF_CANARY@example.invalid/evidence"
    store, binding, coordinator, task_id = _setup(tmp_path)
    coordinator.review(
        "core",
        ReviewDecision(
            reviewer_id="independent-qa",
            accepted=True,
            reason=reason,
            evidence_refs=(evidence_ref,),
        ),
    )

    runtime = coordinator.snapshot().records[0]
    assert runtime.review is not None
    assert runtime.review.reason == reason
    assert runtime.review.evidence_refs == (evidence_ref,)

    checkpoint = binding.checkpoint(coordinator)
    durable = checkpoint.coordinator.records[0]
    assert durable.review is not None
    assert durable.review.reviewer_id == "independent-qa"
    assert durable.review.accepted is True
    assert durable.review.reason == DURABLE_REVIEW_REASON
    assert durable.review.evidence_refs == (_digest_ref(evidence_ref),)

    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=checkpoint)
    with store.connection() as conn:
        raw = str(
            conn.execute(
                "SELECT payload_json FROM checkpoints WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
        )
    assert "OWNER_REVIEW_CANARY" not in raw
    assert "OWNER_REF_CANARY" not in raw

    restored = host.restore_latest(host_task_id=task_id, binding=binding)
    restored_record = restored.snapshot().records[0]
    assert restored_record.state is WorkState.ACCEPTED
    assert restored_record.review == durable.review


def test_rejected_review_minimizes_reason_ref_and_mirrored_blocker(tmp_path) -> None:
    reason = "reject; Authorization: Bearer OWNER_REJECT_CANARY"
    evidence_ref = "https://reviewer:OWNER_REJECT_REF@example.invalid/evidence"
    store, binding, coordinator, task_id = _setup(tmp_path)
    coordinator.review(
        "core",
        ReviewDecision(
            reviewer_id="independent-qa",
            accepted=False,
            reason=reason,
            evidence_refs=(evidence_ref,),
        ),
    )

    runtime = coordinator.snapshot().records[0]
    assert runtime.review is not None
    assert runtime.review.reason == reason
    assert runtime.blocker == reason

    checkpoint = binding.checkpoint(coordinator)
    durable = checkpoint.coordinator.records[0]
    assert durable.review is not None
    assert durable.review.accepted is False
    assert durable.review.reason == DURABLE_REVIEW_REASON
    assert durable.review.evidence_refs == (_digest_ref(evidence_ref),)
    assert durable.blocker == DURABLE_REVIEW_REASON

    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=checkpoint)
    with store.connection() as conn:
        raw = str(
            conn.execute(
                "SELECT payload_json FROM checkpoints WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
        )
    assert "OWNER_REJECT_CANARY" not in raw
    assert "OWNER_REJECT_REF" not in raw

    restored = host.restore_latest(host_task_id=task_id, binding=binding)
    restored_record = restored.snapshot().records[0]
    assert restored_record.state is WorkState.REPAIR_REQUIRED
    assert restored_record.review is not None
    assert restored_record.review.reason == DURABLE_REVIEW_REASON
    assert restored_record.blocker == restored_record.review.reason


def test_accepted_review_minimizes_credential_assignments_and_encoded_keys(
    tmp_path,
) -> None:
    reason_canary = "OWNER_API_KEY_CANARY"
    password_canary = "OWNER_PASSWORD_CANARY"
    encoded_canary = "OWNER_ENCODED_API_KEY_CANARY"
    json_canary = "OWNER_JSON_API_KEY_CANARY"
    reason = f"verified; api_key={reason_canary}"
    evidence_refs = (
        f"https://example.invalid/evidence?password={password_canary}",
        f"https://example.invalid/evidence?api%5Fkey={encoded_canary}",
        (
            "https://example.invalid/evidence?payload="
            f"%7B%22api_key%22%3A%22{json_canary}%22%7D"
        ),
    )
    store, binding, coordinator, task_id = _setup(tmp_path)
    coordinator.review(
        "core",
        ReviewDecision(
            reviewer_id="independent-qa",
            accepted=True,
            reason=reason,
            evidence_refs=evidence_refs,
        ),
    )

    runtime = coordinator.snapshot().records[0]
    assert runtime.review is not None
    assert runtime.review.reason == reason
    assert runtime.review.evidence_refs == evidence_refs

    checkpoint = binding.checkpoint(coordinator)
    durable = checkpoint.coordinator.records[0]
    assert durable.review is not None
    assert durable.review.reason == DURABLE_REVIEW_REASON
    assert durable.review.evidence_refs == tuple(_digest_ref(value) for value in evidence_refs)

    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=checkpoint)
    with store.connection() as conn:
        raw = str(
            conn.execute(
                "SELECT payload_json FROM checkpoints WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
        )
    assert reason_canary not in raw
    assert password_canary not in raw
    assert encoded_canary not in raw
    assert json_canary not in raw

    restored = host.restore_latest(host_task_id=task_id, binding=binding)
    assert restored.snapshot().records[0].review == durable.review


def test_rejected_review_minimizes_secret_assignment_and_mirrored_blocker(
    tmp_path,
) -> None:
    canary = "OWNER_CLIENT_SECRET_CANARY"
    reason = f"rejected; client_secret={canary}"
    evidence_refs = ("review://core/rejected",)
    store, binding, coordinator, task_id = _setup(tmp_path)
    coordinator.review(
        "core",
        ReviewDecision(
            reviewer_id="independent-qa",
            accepted=False,
            reason=reason,
            evidence_refs=evidence_refs,
        ),
    )

    checkpoint = binding.checkpoint(coordinator)
    durable = checkpoint.coordinator.records[0]
    assert durable.review is not None
    assert durable.review.reason == DURABLE_REVIEW_REASON
    assert durable.review.evidence_refs == evidence_refs
    assert durable.blocker == DURABLE_REVIEW_REASON

    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=checkpoint)
    with store.connection() as conn:
        raw = str(
            conn.execute(
                "SELECT payload_json FROM checkpoints WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
        )
    assert canary not in raw

    restored_record = (
        host.restore_latest(
            host_task_id=task_id,
            binding=binding,
        )
        .snapshot()
        .records[0]
    )
    assert restored_record.review == durable.review
    assert restored_record.blocker == DURABLE_REVIEW_REASON


def test_safe_review_evidence_identity_survives_checkpoint_restart(tmp_path) -> None:
    reason = "verified by exact deterministic acceptance evidence"
    evidence_refs = (
        "tests://core/pass",
        "artifact-sha256:" + "f" * 64,
    )
    store, binding, coordinator, task_id = _setup(tmp_path)
    coordinator.review(
        "core",
        ReviewDecision(
            reviewer_id="independent-qa",
            accepted=True,
            reason=reason,
            evidence_refs=evidence_refs,
        ),
    )

    checkpoint = binding.checkpoint(coordinator)
    durable = checkpoint.coordinator.records[0]
    assert durable.review is not None
    assert durable.review.reason == reason
    assert durable.review.evidence_refs == evidence_refs

    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=checkpoint)
    restored = host.restore_latest(host_task_id=task_id, binding=binding)
    restored_review = restored.snapshot().records[0].review
    assert restored_review is not None
    assert restored_review.reason == reason
    assert restored_review.evidence_refs == evidence_refs


def test_safe_rejected_review_preserves_reason_and_blocker_across_restart(tmp_path) -> None:
    reason = "missing exact acceptance evidence"
    evidence_refs = ("tests://core/fail",)
    store, binding, coordinator, task_id = _setup(tmp_path)
    coordinator.review(
        "core",
        ReviewDecision(
            reviewer_id="independent-qa",
            accepted=False,
            reason=reason,
            evidence_refs=evidence_refs,
        ),
    )

    checkpoint = binding.checkpoint(coordinator)
    durable = checkpoint.coordinator.records[0]
    assert durable.review is not None
    assert durable.review.reason == reason
    assert durable.review.evidence_refs == evidence_refs
    assert durable.blocker == reason

    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=checkpoint)
    restored_record = host.restore_latest(
        host_task_id=task_id,
        binding=binding,
    ).snapshot().records[0]
    assert restored_record.review is not None
    assert restored_record.review.reason == reason
    assert restored_record.review.evidence_refs == evidence_refs
    assert restored_record.blocker == reason
