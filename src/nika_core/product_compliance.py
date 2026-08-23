from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from enum import StrEnum


_DECISION_AUTHORITY_KEY = secrets.token_bytes(32)


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
        if self.license_disposition is LicenseDisposition.APPROVED and self.review_ref is None:
            raise ProductComplianceError(
                "approved dependency license requires authorized review_ref evidence"
            )


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
    permission_basis_ref: str | None = None
    legal_basis_ref: str | None = None
    reuse_authorization_ref: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.project_id, "competitor evidence project_id")
        _require_text(self.evidence_id, "competitor evidence_id")
        _require_text(self.source_ref, "competitor source_ref")
        _require_text(self.provenance_ref, "competitor provenance_ref")
        if not isinstance(self.permitted_public_evidence, bool):
            raise ProductComplianceError("permitted_public_evidence must be a boolean")
        if not isinstance(self.proprietary_material, bool):
            raise ProductComplianceError("proprietary_material must be a boolean")
        if self.proprietary_material and self.permitted_public_evidence:
            raise ProductComplianceError(
                "competitor evidence cannot be both proprietary material and public evidence"
            )
        if self.permission_basis_ref is not None:
            _require_text(self.permission_basis_ref, "competitor permission_basis_ref")
        if self.permitted_public_evidence and self.permission_basis_ref is None:
            raise ProductComplianceError(
                "permitted public competitor evidence requires permission_basis_ref"
            )
        if self.legal_basis_ref is not None:
            _require_text(self.legal_basis_ref, "competitor legal_basis_ref")
        if self.reuse_authorization_ref is not None:
            _require_text(self.reuse_authorization_ref, "competitor reuse_authorization_ref")


@dataclass(frozen=True, slots=True)
class ProductComplianceDecision:
    """PF10 decision result whose positive authority is issued only by this process's gate.

    The proof prevents an ordinary caller from turning an arbitrary dataclass into an allowed
    release decision. It is deliberately process-local and is not a signature or durable trust
    anchor. A decision that is copied or modified loses positive authority and reads as blocked.
    """

    project_id: str
    allowed: bool
    findings: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    _authority_proof: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_text(self.project_id, "compliance decision project_id")
        if not isinstance(object.__getattribute__(self, "allowed"), bool):
            raise ProductComplianceError("compliance decision allowed must be a boolean")
        _require_unique_text(self.findings, "compliance finding", allow_empty=True)
        _require_unique_text(self.evidence_refs, "compliance evidence_ref", allow_empty=True)
        raw_allowed = object.__getattribute__(self, "allowed")
        if raw_allowed and self.findings:
            raise ProductComplianceError("allowed compliance decision cannot contain findings")
        if not raw_allowed and not self.findings:
            raise ProductComplianceError("blocked compliance decision requires findings")
        if self._authority_proof is not None:
            _require_text(self._authority_proof, "compliance decision authority proof")

    def __getattribute__(self, name: str):
        if name == "allowed":
            raw_allowed = object.__getattribute__(self, "allowed")
            if not raw_allowed:
                return False
            return _valid_decision_proof(self)
        return object.__getattribute__(self, name)


class ProductComplianceGate:
    """PF10 fail-closed adoption/release policy over recorded evidence.

    This gate does not invent legal conclusions. License disposition and proprietary reuse
    authorization must already come from an authorized review/policy process. A product with no
    other review-bearing evidence needs an explicit scope_review_ref to prove that an empty
    compliance inventory was reviewed rather than silently omitted.
    """

    def evaluate(
        self,
        *,
        project_id: str,
        dependencies: tuple[DependencyAdoption, ...] = (),
        obligation_evidence: tuple[DistributionObligationEvidence, ...] = (),
        competitor_evidence: tuple[CompetitorResearchEvidence, ...] = (),
        scope_review_ref: str | None = None,
    ) -> ProductComplianceDecision:
        _require_text(project_id, "project_id")
        _require_tuple(dependencies, "dependencies")
        _require_tuple(obligation_evidence, "obligation_evidence")
        _require_tuple(competitor_evidence, "competitor_evidence")
        if scope_review_ref is not None:
            _require_text(scope_review_ref, "scope_review_ref")

        findings: list[str] = []
        evidence_refs: list[str] = []
        scope_review_refs: set[str] = set()
        component_ids: set[str] = set()

        if scope_review_ref is not None:
            evidence_refs.append(scope_review_ref)
            scope_review_refs.add(scope_review_ref)

        obligations: dict[tuple[str, str], DistributionObligationEvidence] = {}
        for item in obligation_evidence:
            evidence_refs.append(item.fulfillment_ref)
            if item.project_id != project_id:
                findings.append("cross-project:distribution-obligation")
                continue
            obligation_key = (item.component_id, item.obligation)
            if obligation_key in obligations:
                findings.append(
                    "duplicate:distribution-obligation:"
                    f"{item.component_id}:{item.obligation}"
                )
                continue
            obligations[obligation_key] = item

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
                scope_review_refs.add(dependency.review_ref)
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

        for component_id, obligation in obligations:
            if component_id not in component_ids:
                findings.append(
                    f"orphan:distribution-obligation:{component_id}:{obligation}"
                )

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
            if evidence.permission_basis_ref:
                evidence_refs.append(evidence.permission_basis_ref)
                scope_review_refs.add(evidence.permission_basis_ref)
            if evidence.legal_basis_ref:
                evidence_refs.append(evidence.legal_basis_ref)
                scope_review_refs.add(evidence.legal_basis_ref)
            if evidence.reuse_authorization_ref:
                evidence_refs.append(evidence.reuse_authorization_ref)
                scope_review_refs.add(evidence.reuse_authorization_ref)

            if evidence.proprietary_material:
                if not evidence.legal_basis_ref or not evidence.reuse_authorization_ref:
                    findings.append(f"proprietary-reuse:not-authorized:{evidence.evidence_id}")
            elif not evidence.permitted_public_evidence:
                findings.append(f"research-source:not-permitted:{evidence.evidence_id}")

        if not scope_review_refs:
            findings.append("compliance-scope:unreviewed")

        normalized_findings = tuple(dict.fromkeys(findings))
        normalized_evidence = tuple(dict.fromkeys(evidence_refs))
        return _issue_decision(
            project_id=project_id,
            allowed=not normalized_findings,
            findings=normalized_findings,
            evidence_refs=normalized_evidence,
        )

    def require_release_allowed(self, decision: ProductComplianceDecision) -> None:
        if not isinstance(decision, ProductComplianceDecision):
            raise ProductComplianceError("release requires ProductComplianceDecision")
        if not decision.allowed:
            findings = decision.findings
            raw_allowed = object.__getattribute__(decision, "allowed")
            if raw_allowed and not _valid_decision_proof(decision):
                findings = (*findings, "decision:untrusted-origin")
            joined = ", ".join(dict.fromkeys(findings))
            raise ProductComplianceError(f"release blocked by PF10 compliance gate: {joined}")


def _issue_decision(
    *,
    project_id: str,
    allowed: bool,
    findings: tuple[str, ...],
    evidence_refs: tuple[str, ...],
) -> ProductComplianceDecision:
    proof = _decision_proof(
        project_id=project_id,
        allowed=allowed,
        findings=findings,
        evidence_refs=evidence_refs,
    )
    return ProductComplianceDecision(
        project_id=project_id,
        allowed=allowed,
        findings=findings,
        evidence_refs=evidence_refs,
        _authority_proof=proof,
    )


def _decision_proof(
    *,
    project_id: str,
    allowed: bool,
    findings: tuple[str, ...],
    evidence_refs: tuple[str, ...],
) -> str:
    payload = json.dumps(
        {
            "schema": "nika-pf10-decision-authority-v1",
            "project_id": project_id,
            "allowed": allowed,
            "findings": list(findings),
            "evidence_refs": list(evidence_refs),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_DECISION_AUTHORITY_KEY, payload, hashlib.sha256).hexdigest()


def _valid_decision_proof(decision: ProductComplianceDecision) -> bool:
    proof = object.__getattribute__(decision, "_authority_proof")
    if not isinstance(proof, str) or not proof:
        return False
    expected = _decision_proof(
        project_id=object.__getattribute__(decision, "project_id"),
        allowed=object.__getattribute__(decision, "allowed"),
        findings=object.__getattribute__(decision, "findings"),
        evidence_refs=object.__getattribute__(decision, "evidence_refs"),
    )
    return hmac.compare_digest(proof, expected)


def _require_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ProductComplianceError(f"{label} must be non-empty text")


def _require_tuple(value: object, label: str) -> None:
    if not isinstance(value, tuple):
        raise ProductComplianceError(f"{label} must be a tuple")


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
