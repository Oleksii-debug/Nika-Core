from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from nika_core.product_factory_build_execution import (
    BuildExecutionScopeRequest,
    BuildExecutionSpec,
)
from nika_core.product_factory_coordinator import (
    ProductFactoryCoordinator,
    WorkRecord,
    WorkState,
)
from nika_core.product_factory_deployment import (
    ExecutionRequest,
    Platform,
    ResourceEnvelope,
)
from nika_core.product_factory_multi_repository import RepositoryGraphAuthority
from nika_core.product_factory_review_authority import ProductFactoryReviewSubject


class ReviewedBuildAdmissionError(ValueError):
    """Raised when PF4 evidence cannot safely authorize PF5 build admission."""


@dataclass(frozen=True, slots=True)
class ReviewedBuildExecutionPolicy:
    """Host-owned PF5 scope for one exact independently reviewed candidate.

    The policy deliberately carries only PF5 request scope. It never accepts argv from the
    ProductRepositoryGraph or worker result; the existing TrustedExecutionAuthorityPort
    remains authoritative for the command_id -> argv mapping at PF5 submit/dispatch time.
    """

    project_id: str
    repository_id: str
    component_id: str
    candidate_work_id: str
    source_sha: str
    review_fingerprint: str
    spec_version: int
    row_version: int
    graph_digest: str
    platform: Platform
    requested_node_ids: tuple[str, ...]
    workspace_relpath: str
    required_features: frozenset[str]
    required_toolchains: frozenset[str]
    resources: ResourceEnvelope
    network_scopes: tuple[str, ...] = ()
    credential_refs: tuple[str, ...] = ()
    command_id: str = "build"
    require_gpu: bool = False
    lease_seconds: int = 900

    def __post_init__(self) -> None:
        for label, value in (
            ("project_id", self.project_id),
            ("repository_id", self.repository_id),
            ("component_id", self.component_id),
            ("candidate_work_id", self.candidate_work_id),
            ("source_sha", self.source_sha),
            ("review_fingerprint", self.review_fingerprint),
            ("graph_digest", self.graph_digest),
            ("workspace_relpath", self.workspace_relpath),
            ("command_id", self.command_id),
        ):
            if type(value) is not str or not value or value != value.strip():
                raise ReviewedBuildAdmissionError(
                    f"reviewed-build policy {label} must be exact non-empty text"
                )
        _validate_sha(self.source_sha)
        _validate_digest(self.review_fingerprint, "review fingerprint")
        _validate_digest(self.graph_digest, "graph digest")
        if (
            type(self.spec_version) is not int
            or self.spec_version < 1
            or type(self.row_version) is not int
            or self.row_version < 0
        ):
            raise ReviewedBuildAdmissionError(
                "reviewed-build ProductProject versions must be exact valid integers"
            )
        if type(self.platform) is not Platform:
            raise ReviewedBuildAdmissionError("reviewed-build platform must be exact Platform")
        _exact_text_tuple(self.requested_node_ids, "requested node ids", non_empty=True)
        _exact_text_tuple(self.network_scopes, "network scopes")
        _exact_text_tuple(self.credential_refs, "credential refs")
        _exact_text_set(self.required_features, "required features")
        _exact_text_set(self.required_toolchains, "required toolchains")
        if type(self.resources) is not ResourceEnvelope:
            raise ReviewedBuildAdmissionError(
                "reviewed-build resources must be exact ResourceEnvelope"
            )
        if type(self.require_gpu) is not bool:
            raise ReviewedBuildAdmissionError("reviewed-build require_gpu must be exact bool")
        if type(self.lease_seconds) is not int or self.lease_seconds <= 0:
            raise ReviewedBuildAdmissionError(
                "reviewed-build lease duration must be an exact positive integer"
            )


class ReviewedBuildExecutionPolicyPort(Protocol):
    """Resolve trusted PF5 scope for one exact PF4-reviewed candidate."""

    def resolve(
        self,
        *,
        project_id: str,
        repository_id: str,
        component_id: str,
        candidate_work_id: str,
        source_sha: str,
        review_fingerprint: str,
        spec_version: int,
        row_version: int,
        graph_digest: str,
    ) -> ReviewedBuildExecutionPolicy: ...


def reviewed_component_build_spec(
    *,
    authority: RepositoryGraphAuthority,
    coordinator: ProductFactoryCoordinator,
    component_id: str,
    policies: ReviewedBuildExecutionPolicyPort,
) -> BuildExecutionSpec:
    """Convert exact PF4 acceptance into bounded PF5 request scope.

    This is a trust-preserving seam, not a build executor. PF4 remains the source of exact
    independently reviewed candidate identity. The host-owned policy supplies only bounded
    PF5 scope. PF5's existing trusted execution authority still resolves the actual argv,
    so ProductRepositoryGraph.build_commands can never become shell authority here.
    """

    if type(authority) is not RepositoryGraphAuthority:
        raise ReviewedBuildAdmissionError(
            "reviewed-build admission requires exact repository-graph authority"
        )
    if type(coordinator) is not ProductFactoryCoordinator:
        raise ReviewedBuildAdmissionError(
            "reviewed-build admission requires exact ProductFactoryCoordinator"
        )
    if (
        type(component_id) is not str
        or not component_id
        or component_id != component_id.strip()
    ):
        raise ReviewedBuildAdmissionError(
            "reviewed-build component id must be exact non-empty text"
        )
    _validate_authority(authority)

    snapshot = coordinator.snapshot()
    if (
        snapshot.project_id != authority.project_id
        or coordinator.graph.project_id != authority.project_id
    ):
        raise ReviewedBuildAdmissionError(
            "PF4 coordinator project does not match durable repository-graph authority"
        )
    if coordinator.graph != authority.graph:
        raise ReviewedBuildAdmissionError(
            "PF4 coordinator graph does not match durable repository-graph authority"
        )

    component = next(
        (item for item in authority.graph.components if item.component_id == component_id),
        None,
    )
    if component is None:
        raise ReviewedBuildAdmissionError(
            "reviewed-build component is absent from durable repository graph"
        )
    record = next(
        (item for item in snapshot.records if item.request.component_id == component_id),
        None,
    )
    if record is None:
        raise ReviewedBuildAdmissionError("reviewed-build component has no PF4 work record")
    _validate_accepted_record(authority, component, record)
    _verify_current_review_authority(coordinator, record)

    result = record.result
    review = record.review
    if result is None or review is None:
        raise ReviewedBuildAdmissionError(
            "accepted PF4 work lost result/review evidence during build admission"
        )

    review_fingerprint = reviewed_candidate_fingerprint(
        authority=authority,
        record=record,
    )
    policy = policies.resolve(
        project_id=authority.project_id,
        repository_id=record.request.repository_id,
        component_id=component_id,
        candidate_work_id=record.request.work_id,
        source_sha=result.result_sha,
        review_fingerprint=review_fingerprint,
        spec_version=authority.spec_version,
        row_version=authority.row_version,
        graph_digest=authority.graph_digest,
    )
    if type(policy) is not ReviewedBuildExecutionPolicy:
        raise ReviewedBuildAdmissionError(
            "reviewed-build policy port returned a noncanonical policy carrier"
        )
    expected_policy_identity = (
        authority.project_id,
        record.request.repository_id,
        component_id,
        record.request.work_id,
        result.result_sha,
        review_fingerprint,
        authority.spec_version,
        authority.row_version,
        authority.graph_digest,
    )
    actual_policy_identity = (
        policy.project_id,
        policy.repository_id,
        policy.component_id,
        policy.candidate_work_id,
        policy.source_sha,
        policy.review_fingerprint,
        policy.spec_version,
        policy.row_version,
        policy.graph_digest,
    )
    if actual_policy_identity != expected_policy_identity:
        raise ReviewedBuildAdmissionError(
            "reviewed-build policy does not match exact accepted candidate authority"
        )

    build_work_id = _build_work_id(authority=authority, record=record)
    return BuildExecutionSpec(
        request=ExecutionRequest(
            project_id=authority.project_id,
            work_id=build_work_id,
            platform=policy.platform,
            required_features=policy.required_features,
            required_toolchains=policy.required_toolchains,
            resources=policy.resources,
            require_gpu=policy.require_gpu,
        ),
        source_sha=result.result_sha,
        scope=BuildExecutionScopeRequest(
            repository_id=record.request.repository_id,
            workspace_relpath=policy.workspace_relpath,
            requested_node_ids=policy.requested_node_ids,
            network_scopes=policy.network_scopes,
            credential_refs=policy.credential_refs,
            command_id=policy.command_id,
        ),
        lease_seconds=policy.lease_seconds,
    )


def reviewed_candidate_fingerprint(
    *,
    authority: RepositoryGraphAuthority,
    record: WorkRecord,
) -> str:
    """Bind PF5 admission to exact PF4 candidate and independent-review evidence."""

    if type(record) is not WorkRecord:
        raise ReviewedBuildAdmissionError("reviewed-build record must be exact WorkRecord")
    if record.result is None or record.review is None:
        raise ReviewedBuildAdmissionError(
            "reviewed-build fingerprint requires result and review evidence"
        )
    result = record.result
    review = record.review
    payload = {
        "schema": "nika-product-factory-reviewed-build-admission-v1",
        "project_id": authority.project_id,
        "spec_version": authority.spec_version,
        "row_version": authority.row_version,
        "graph_digest": authority.graph_digest,
        "component_id": record.request.component_id,
        "repository_id": record.request.repository_id,
        "candidate_work_id": record.request.work_id,
        "attempt": record.request.attempt,
        "base_sha": record.request.base_sha,
        "result_sha": result.result_sha,
        "diff_digest": result.diff_digest,
        "producer_actor_id": result.producer_actor_id,
        "reviewer_id": review.reviewer_id,
        "accepted": review.accepted,
        "review_reason": review.reason,
        "review_evidence_refs": review.evidence_refs,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_authority(authority: RepositoryGraphAuthority) -> None:
    for label, value in (
        ("checkpoint id", authority.checkpoint_id),
        ("project id", authority.project_id),
        ("graph digest", authority.graph_digest),
    ):
        if type(value) is not str or not value or value != value.strip():
            raise ReviewedBuildAdmissionError(
                f"repository-graph {label} must be exact non-empty text"
            )
    _validate_digest(authority.graph_digest, "graph digest")
    if (
        type(authority.spec_version) is not int
        or authority.spec_version < 1
        or type(authority.row_version) is not int
        or authority.row_version < 0
        or type(authority.graph_version) is not int
        or authority.graph_version < 1
    ):
        raise ReviewedBuildAdmissionError(
            "repository-graph versions must be exact valid integers"
        )
    if authority.graph.project_id != authority.project_id:
        raise ReviewedBuildAdmissionError(
            "repository-graph authority project identity is inconsistent"
        )


def _validate_accepted_record(authority, component, record: WorkRecord) -> None:
    if type(record) is not WorkRecord:
        raise ReviewedBuildAdmissionError("PF4 work record must be exact WorkRecord")
    if record.state is not WorkState.ACCEPTED:
        raise ReviewedBuildAdmissionError(
            "PF5 build admission requires an ACCEPTED independently reviewed component"
        )
    if record.blocker is not None or record.result is None or record.review is None:
        raise ReviewedBuildAdmissionError(
            "accepted PF4 work must carry result/review evidence without blocker"
        )
    request = record.request
    result = record.result
    review = record.review
    if (
        request.project_id != authority.project_id
        or request.component_id != component.component_id
        or request.repository_id != component.repository_id
        or request.allowed_paths != component.paths
        or request.acceptance_commands != component.test_commands
    ):
        raise ReviewedBuildAdmissionError(
            "accepted PF4 work does not match durable repository-graph authority"
        )
    if "build_release" not in request.permission_ceiling:
        raise ReviewedBuildAdmissionError(
            "accepted PF4 work lacks build_release permission in trusted plan ceiling"
        )
    if (
        result.work_id != request.work_id
        or result.component_id != request.component_id
        or result.repository_id != request.repository_id
        or result.base_sha != request.base_sha
        or result.coding_result.job_id != request.work_id
    ):
        raise ReviewedBuildAdmissionError(
            "accepted PF4 result identity does not match active work request"
        )
    _validate_sha(result.result_sha)
    _validate_digest(result.diff_digest, "diff digest")
    if result.coding_result.succeeded is not True:
        raise ReviewedBuildAdmissionError("accepted PF4 result is not successful")
    if (
        type(result.producer_actor_id) is not str
        or not result.producer_actor_id
        or result.producer_actor_id != result.producer_actor_id.strip()
    ):
        raise ReviewedBuildAdmissionError(
            "accepted PF4 result lacks exact producer actor identity"
        )
    if (
        type(review.reviewer_id) is not str
        or not review.reviewer_id
        or review.reviewer_id != review.reviewer_id.strip()
        or review.accepted is not True
        or type(review.evidence_refs) is not tuple
        or not review.evidence_refs
        or any(type(ref) is not str or not ref.strip() for ref in review.evidence_refs)
    ):
        raise ReviewedBuildAdmissionError(
            "accepted PF4 review evidence is not canonical"
        )
    if result.producer_actor_id == review.reviewer_id:
        raise ReviewedBuildAdmissionError(
            "accepted PF4 producer and independent reviewer must differ"
        )


def _verify_current_review_authority(
    coordinator: ProductFactoryCoordinator,
    record: WorkRecord,
) -> None:
    result = record.result
    review = record.review
    if result is None or review is None or result.producer_actor_id is None:
        raise ReviewedBuildAdmissionError(
            "accepted PF4 work lacks exact trusted review subject evidence"
        )
    review_authority = coordinator.review_authority
    if review_authority is None:
        raise ReviewedBuildAdmissionError(
            "current trusted independent review authority is required for PF5 admission"
        )
    subject = ProductFactoryReviewSubject(
        project_id=record.request.project_id,
        component_id=record.request.component_id,
        work_id=record.request.work_id,
        repository_id=record.request.repository_id,
        base_sha=record.request.base_sha,
        result_sha=result.result_sha,
        diff_digest=result.diff_digest,
        attempt=record.request.attempt,
        producer_actor_id=result.producer_actor_id,
        reviewer_id=review.reviewer_id,
        accepted=review.accepted,
    )
    try:
        verified = review_authority.verify(subject, review.evidence_refs)
    except Exception as exc:
        raise ReviewedBuildAdmissionError(
            "current trusted independent review authority verification failed"
        ) from exc
    if verified is not True:
        raise ReviewedBuildAdmissionError(
            "current trusted independent review authority rejected build admission"
        )


def _build_work_id(*, authority: RepositoryGraphAuthority, record: WorkRecord) -> str:
    fingerprint = reviewed_candidate_fingerprint(authority=authority, record=record)
    return f"pf5-build:{fingerprint}"


def _exact_text_tuple(value: object, label: str, *, non_empty: bool = False) -> None:
    if type(value) is not tuple or (non_empty and not value):
        raise ReviewedBuildAdmissionError(f"{label} must be an exact tuple")
    if any(type(item) is not str or not item or item != item.strip() for item in value):
        raise ReviewedBuildAdmissionError(f"{label} must contain exact non-empty text")
    if len(value) != len(set(value)):
        raise ReviewedBuildAdmissionError(f"{label} must not contain duplicates")


def _exact_text_set(value: object, label: str) -> None:
    if type(value) is not frozenset:
        raise ReviewedBuildAdmissionError(f"{label} must be an exact frozenset")
    if any(type(item) is not str or not item or item != item.strip() for item in value):
        raise ReviewedBuildAdmissionError(f"{label} must contain exact non-empty text")


def _validate_sha(value: object) -> None:
    if (
        type(value) is not str
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ReviewedBuildAdmissionError("source SHA must be canonical lowercase hex")


def _validate_digest(value: object, label: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ReviewedBuildAdmissionError(f"{label} must be canonical lowercase sha256")
