from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProductComplianceError(ValueError):
    pass


class LicenseDisposition(StrEnum):
    APPROVED = "approved"
    REVIEW_REQUIRED = "review_required"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class DependencyAdoption:
    project_id: str
    component_id: str
    package_name: str
    version: str
    source_ref: str
    provenance_ref: str
    license_expression: str
    license_disposition: LicenseDisposition
    distribution_obligations: tuple[str, ...] = ()
    notice_required: bool = False
    notice_refs: tuple[str, ...] = ()
    review_ref: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.project_id, "dependency project_id")
        _require_text(self.component_id, "dependency component_id")
        _require_text(self.package_name, "dependency package_name")
        _require_text(self.version, "dependency version")
        _require_text(self.source_ref, "dependency source_ref")
        _require_text(self.provenance_ref, "dependency provenance_ref")
        _require_text(self.license_expression, "dependency license_expression")
        if not isinstance(self.license_disposition, LicenseDisposition):
            raise ProductComplianceError("dependency license_disposition is invalid")
        _require_unique_text(self.distribution_obligations, "distribution obligation")
        _require_unique_text(self.notice_refs, "dependency notice_ref")
        if self.review_ref is not None:
            _require_text(self.review_ref, "dependency review_ref")


@dataclass(frozen=True, slots=True)
class DistributionObligationEvidence:
    project_id: str
    component_id: str
    obligation: str
    fulfillment_ref: str

    def __post_init__(self) -> None:
        _require_text(self.project_id, "obligation project_id")
        _require_text(self.component_id, "obligation component_id")
        _require_text(self.obligation, "obligation")
        _require_text(self.fulfillment_ref, "obligation fulfillment_ref")


@dataclass(frozen=True, slots=True)
class CompetitorResearchEvidence:
    project_id: str
    evidence_id: str
    source_ref: str
    provenance_ref: str
    permitted_public_evidence: bool
    proprietary_material: bool = False
    legal_basis_ref: str | None = None
    reuse_authorization_ref: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.project_id, "competitor evidence project_id")
        _require_text(self.evidence_id, "competitor evidence_id")
        _require_text(self.source_ref, "competitor source_ref")
        _require_text(self.provenance_ref, "competitor provenance_ref")
        if self.proprietary_material and self.permitted_public_evidence:
            raise ProductComplianceError(
                "competitor evidence cannot be both proprietary material and public evidence"
            )
        if self.legal_basis_ref is not None:
            _require_text(self.legal_basis_ref, "competitor legal_basis_ref")
        if self.reuse_authorization_ref is not None:
            _require_text(self.reuse_authorization_ref, "competitor reuse_authorization_ref")


@dataclass(frozen=True, slots=True)
class ProductComplianceDecision:
    project_id: str
    allowed: bool
    findings: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.project_id, "compliance decision project_id")
        _require_unique_text(self.findings, "compliance finding", allow_empty=True)
        _require_unique_text(self.evidence_refs, "compliance evidence_ref", allow_empty=True)
        if self.allowed and self.findings:
            raise ProductComplianceError("allowed compliance decision cannot contain findings")
        if not self.allowed and not self.findings:
            raise ProductComplianceError("blocked compliance decision requires findings")


class ProductComplianceGate:
    """PF10 fail-closed adoption/release policy over recorded evidence.

    This gate does not invent legal conclusions. License disposition and any proprietary
    reuse authorization must already come from an authorized review/policy process.
    """

    def evaluate(
        self,
        *,
        project_id: str,
        dependencies: tuple[DependencyAdoption, ...] = (),
        obligation_evidence: tuple[DistributionObligationEvidence, ...] = (),
        competitor_evidence: tuple[CompetitorResearchEvidence, ...] = (),
    ) -> ProductComplianceDecision:
        _require_text(project_id, "project_id")
        findings: list[str] = []
        evidence_refs: list[str] = []
        component_ids: set[str] = set()

        obligations = {
            (item.component_id, item.obligation): item
            for item in obligation_evidence
            if item.project_id == project_id
        }
        for item in obligation_evidence:
            if item.project_id != project_id:
                findings.append("cross-project:distribution-obligation")
            evidence_refs.append(item.fulfillment_ref)

        for dependency in dependencies:
            if dependency.project_id != project_id:
                findings.append(f"cross-project:dependency:{dependency.component_id}")
                continue
            if dependency.component_id in component_ids:
                findings.append(f"duplicate:dependency-component:{dependency.component_id}")
                continue
            component_ids.add(dependency.component_id)
            evidence_refs.extend((dependency.source_ref, dependency.provenance_ref))
            if dependency.review_ref:
                evidence_refs.append(dependency.review_ref)
            evidence_refs.extend(dependency.notice_refs)

            if dependency.license_disposition is LicenseDisposition.BLOCKED:
                findings.append(f"license:blocked:{dependency.component_id}")
            elif dependency.license_disposition is LicenseDisposition.REVIEW_REQUIRED:
                findings.append(f"license:review-required:{dependency.component_id}")
            for obligation in dependency.distribution_obligations:
                if (dependency.component_id, obligation) not in obligations:
                    findings.append(
                        "distribution-obligation:unfulfilled:"
                        f"{dependency.component_id}:{obligation}"
                    )
            if dependency.notice_required and not dependency.notice_refs:
                findings.append(f"notice:missing:{dependency.component_id}")

        evidence_ids: set[str] = set()
        for evidence in competitor_evidence:
            if evidence.project_id != project_id:
                findings.append(f"cross-project:competitor-evidence:{evidence.evidence_id}")
                continue
            if evidence.evidence_id in evidence_ids:
                findings.append(f"duplicate:competitor-evidence:{evidence.evidence_id}")
                continue
            evidence_ids.add(evidence.evidence_id)
            evidence_refs.extend((evidence.source_ref, evidence.provenance_ref))
            if evidence.legal_basis_ref:
                evidence_refs.append(evidence.legal_basis_ref)
            if evidence.reuse_authorization_ref:
                evidence_refs.append(evidence.reuse_authorization_ref)

            if evidence.proprietary_material:
                if not evidence.legal_basis_ref or not evidence.reuse_authorization_ref:
                    findings.append(f"proprietary-reuse:not-authorized:{evidence.evidence_id}")
            elif not evidence.permitted_public_evidence:
                findings.append(f"research-source:not-permitted:{evidence.evidence_id}")

        normalized_findings = tuple(dict.fromkeys(findings))
        normalized_evidence = tuple(dict.fromkeys(evidence_refs))
        return ProductComplianceDecision(
            project_id=project_id,
            allowed=not normalized_findings,
            findings=normalized_findings,
            evidence_refs=normalized_evidence,
        )

    def require_release_allowed(self, decision: ProductComplianceDecision) -> None:
        if not decision.allowed:
            joined = ", ".join(decision.findings)
            raise ProductComplianceError(f"release blocked by PF10 compliance gate: {joined}")


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ProductComplianceError(f"{label} must be non-empty text")


def _require_unique_text(
    values: tuple[str, ...],
    label: str,
    *,
    allow_empty: bool = False,
) -> None:
    if not isinstance(values, tuple):
        raise ProductComplianceError(f"{label}s must be a tuple")
    if not allow_empty and not values:
        return
    for value in values:
        _require_text(value, label)
    if len(set(values)) != len(values):
        raise ProductComplianceError(f"duplicate {label}")
