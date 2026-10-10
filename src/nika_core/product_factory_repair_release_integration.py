from __future__ import annotations

from nika_core.product_factory_deployment import NormalizedBuildEvidence, ReleaseRef
from nika_core.product_factory_incident_contracts import (
    RepairCandidateEvidence,
    RepairWorkOrder,
)


class ProductRepairReleaseIntegrationError(ValueError):
    """Raised when repair, build and immutable release identity do not agree."""


def release_ref_from_repair_build(
    *,
    work_order: RepairWorkOrder,
    candidate: RepairCandidateEvidence,
    build: NormalizedBuildEvidence,
    version: str,
) -> ReleaseRef:
    """Bind an independently accepted repair to one exact successful build.

    This is deliberately a thin integration seam. It does not execute builds, workers,
    deployments or reviews. PF5 remains responsible for producing normalized build
    evidence; PF8 only accepts that evidence when it is the exact reviewed repair artifact.
    """

    if not version.strip():
        raise ProductRepairReleaseIntegrationError("release version must not be empty")
    if candidate.incident_id != work_order.incident_id:
        raise ProductRepairReleaseIntegrationError("candidate belongs to another incident")
    if candidate.work_order_id != work_order.work_order_id:
        raise ProductRepairReleaseIntegrationError("candidate belongs to another repair work order")
    if candidate.base_release_sha != work_order.base_release_sha:
        raise ProductRepairReleaseIntegrationError("candidate base release does not match work order")
    if not candidate.review_accepted:
        raise ProductRepairReleaseIntegrationError("repair candidate is not independently accepted")
    if build.work_id != work_order.work_order_id:
        raise ProductRepairReleaseIntegrationError("build evidence belongs to another repair work order")
    if not build.succeeded:
        raise ProductRepairReleaseIntegrationError("repair build did not succeed")
    if build.release_sha != candidate.result_sha:
        raise ProductRepairReleaseIntegrationError("build release SHA does not match reviewed candidate")
    if build.artifact_digest != candidate.artifact_digest:
        raise ProductRepairReleaseIntegrationError(
            "build artifact digest does not match reviewed candidate"
        )

    return ReleaseRef(
        project_id=work_order.project_id,
        version=version.strip(),
        source_sha=candidate.result_sha,
        artifact_digest=candidate.artifact_digest,
    )
