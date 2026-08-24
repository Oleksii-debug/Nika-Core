from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

_DECISION_AUTHORITY_KEY = secrets.token_bytes(32)
_SHA256_REF_RE = re.compile(r"^(?:hash:)?sha256:[0-9a-fA-F]{64}$")
_PACKAGE_SEPARATOR_RE = re.compile(r"[-_.]+")
_MUTABLE_VERSION_TOKENS = {"latest", "main", "master", "head", "trunk", "develop", "development"}
_MUTABLE_SOURCE_TOKENS = {"latest", "main", "master", "head", "trunk", "develop", "development"}
_UNKNOWN_LICENSE_TOKENS = {"unknown", "noassertion", "unlicensed", "tbd", "none", "n/a"}


class ProductComplianceError(ValueError):
    pass


class LicenseDisposition(StrEnum):
    APPROVED = "approved"
    REVIEW_REQUIRED = "review_required"
    BLOCKED = "blocked"


class ComplianceReviewAuthorityPort(Protocol):
    """Trusted resolver for PF10 review/legal/permission evidence references.

    ``purpose`` is deliberately scope-bearing. Release-allowing checks include a
    deterministic fingerprint of the exact dependency or research evidence (or the
    complete compliance input set) so an authority can reject stale/cross-scope
    evidence instead of treating an opaque reference as sufficient authority.
    """

    def verify(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool: ...


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
        if not isinstance(self.notice_required, bool):
            raise ProductComplianceError("dependency notice_required must be a boolean")
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
    """PF10 result whose positive authority is issued only by this process's gate."""

    project_id: str
    allowed: bool
    findings: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    input_fingerprint: str | None = None
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
        if self.input_fingerprint is not None:
            _require_text(self.input_fingerprint, "compliance decision input_fingerprint")
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
    """PF10 fail-closed adoption/release policy over authority-resolved evidence."""

    def __init__(
        self,
        *,
        review_authority: ComplianceReviewAuthorityPort | None = None,
    ) -> None:
        self._review_authority = review_authority

    def evaluate(
        self,
        *,
        project_id: str,
        dependencies: tuple[DependencyAdoption, ...] = (),
        obligation_evidence: tuple[DistributionObligationEvidence, ...] = (),
        competitor_evidence: tuple[CompetitorResearchEvidence, ...] = (),
        dependency_closure_ref: str | None = None,
        scope_review_ref: str | None = None,
    ) -> ProductComplianceDecision:
        _require_text(project_id, "project_id")
        _require_tuple(dependencies, "dependencies")
        _require_tuple(obligation_evidence, "obligation_evidence")
        _require_tuple(competitor_evidence, "competitor_evidence")
        if dependency_closure_ref is not None:
            _require_text(dependency_closure_ref, "dependency_closure_ref")
        if scope_review_ref is not None:
            _require_text(scope_review_ref, "scope_review_ref")

        input_fingerprint = compliance_input_fingerprint(
            project_id=project_id,
            dependencies=dependencies,
            obligation_evidence=obligation_evidence,
            competitor_evidence=competitor_evidence,
            dependency_closure_ref=dependency_closure_ref,
            scope_review_ref=scope_review_ref,
        )
        findings: list[str] = []
        evidence_refs: list[str] = []
        component_ids: set[str] = set()
        dependencies_by_component: dict[str, DependencyAdoption] = {}
        dependency_identities: set[tuple[str, str, str, str]] = set()
        package_versions: dict[tuple[str, str], tuple[str, str]] = {}

        if not dependencies:
            findings.append("dependency-inventory:empty")

        if dependency_closure_ref is None:
            findings.append("dependency-closure:unverified")
        else:
            evidence_refs.append(dependency_closure_ref)
            if not self._review_allowed(
                project_id=project_id,
                evidence_ref=dependency_closure_ref,
                purpose=f"dependency-closure:{input_fingerprint}",
            ):
                findings.append("dependency-closure:untrusted-review-authority")

        if scope_review_ref is None:
            findings.append("compliance-scope:unreviewed")
        else:
            evidence_refs.append(scope_review_ref)
            if not self._review_allowed(
                project_id=project_id,
                evidence_ref=scope_review_ref,
                purpose=f"compliance-scope:{input_fingerprint}",
            ):
                findings.append("compliance-scope:untrusted-review-authority")

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
            dependencies_by_component[dependency.component_id] = dependency
            evidence_refs.extend((dependency.source_ref, dependency.provenance_ref))
            if dependency.review_ref:
                evidence_refs.append(dependency.review_ref)
            evidence_refs.extend(dependency.notice_refs)

            package_name = _canonical_package_name(dependency.package_name)
            identity = (
                package_name,
                dependency.version.casefold(),
                dependency.source_ref.strip(),
                dependency.provenance_ref.casefold(),
            )
            if identity in dependency_identities:
                findings.append(f"duplicate:dependency-identity:{dependency.component_id}")
            dependency_identities.add(identity)
            package_version = (package_name, dependency.version.casefold())
            source_identity = (dependency.source_ref.strip(), dependency.provenance_ref.casefold())
            previous_source = package_versions.get(package_version)
            if previous_source is not None and previous_source != source_identity:
                findings.append(
                    f"conflict:dependency-source:{package_name}:{dependency.version}"
                )
            else:
                package_versions[package_version] = source_identity

            if _mutable_version(dependency.version):
                findings.append(f"dependency-version:mutable:{dependency.component_id}")
            if _mutable_source(dependency.source_ref):
                findings.append(f"dependency-source:mutable:{dependency.component_id}")
            if not _SHA256_REF_RE.fullmatch(dependency.provenance_ref.strip()):
                findings.append(f"dependency-provenance:not-exact-sha256:{dependency.component_id}")
            if dependency.license_expression.strip().casefold() in _UNKNOWN_LICENSE_TOKENS:
                findings.append(f"license:unknown:{dependency.component_id}")

            if dependency.license_disposition is LicenseDisposition.BLOCKED:
                findings.append(f"license:blocked:{dependency.component_id}")
            elif dependency.license_disposition is LicenseDisposition.REVIEW_REQUIRED:
                findings.append(f"license:review-required:{dependency.component_id}")
            elif dependency.review_ref is not None:
                dependency_fingerprint = _dependency_fingerprint(dependency)
                purpose = (
                    f"license-disposition:{dependency.component_id}:"
                    f"{dependency_fingerprint}"
                )
                if not self._review_allowed(
                    project_id=project_id,
                    evidence_ref=dependency.review_ref,
                    purpose=purpose,
                ):
                    findings.append(
                        f"license:untrusted-review-authority:{dependency.component_id}"
                    )

            for obligation in dependency.distribution_obligations:
                if (dependency.component_id, obligation) not in obligations:
                    findings.append(
                        "distribution-obligation:unfulfilled:"
                        f"{dependency.component_id}:{obligation}"
                    )
            if dependency.notice_required and not dependency.notice_refs:
                findings.append(f"notice:missing:{dependency.component_id}")
            if not dependency.notice_required and dependency.notice_refs:
                findings.append(f"orphan:notice-ref:{dependency.component_id}")

        for component_id, obligation in obligations.keys():
            dependency = dependencies_by_component.get(component_id)
            if dependency is None or obligation not in dependency.distribution_obligations:
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
            evidence_fingerprint = _competitor_fingerprint(evidence)

            if evidence.permission_basis_ref:
                evidence_refs.append(evidence.permission_basis_ref)
            if evidence.legal_basis_ref:
                evidence_refs.append(evidence.legal_basis_ref)
            if evidence.reuse_authorization_ref:
                evidence_refs.append(evidence.reuse_authorization_ref)

            if evidence.proprietary_material:
                if not evidence.legal_basis_ref or not evidence.reuse_authorization_ref:
                    findings.append(f"proprietary-reuse:not-authorized:{evidence.evidence_id}")
                else:
                    legal_ok = self._review_allowed(
                        project_id=project_id,
                        evidence_ref=evidence.legal_basis_ref,
                        purpose=(
                            f"proprietary-legal-basis:{evidence.evidence_id}:"
                            f"{evidence_fingerprint}"
                        ),
                    )
                    reuse_ok = self._review_allowed(
                        project_id=project_id,
                        evidence_ref=evidence.reuse_authorization_ref,
                        purpose=(
                            f"proprietary-reuse-authorization:{evidence.evidence_id}:"
                            f"{evidence_fingerprint}"
                        ),
                    )
                    if not (legal_ok and reuse_ok):
                        findings.append(
                            f"proprietary-reuse:untrusted-authority:{evidence.evidence_id}"
                        )
            elif evidence.permitted_public_evidence:
                permission_ref = evidence.permission_basis_ref
                if permission_ref is None or not self._review_allowed(
                    project_id=project_id,
                    evidence_ref=permission_ref or "missing",
                    purpose=(
                        f"public-source-permission:{evidence.evidence_id}:"
                        f"{evidence_fingerprint}"
                    ),
                ):
                    findings.append(
                        f"research-source:untrusted-permission-authority:{evidence.evidence_id}"
                    )
            else:
                findings.append(f"research-source:not-permitted:{evidence.evidence_id}")

        normalized_findings = tuple(dict.fromkeys(findings))
        normalized_evidence = tuple(dict.fromkeys(evidence_refs))
        return _issue_decision(
            project_id=project_id,
            allowed=not normalized_findings,
            findings=normalized_findings,
            evidence_refs=normalized_evidence,
            input_fingerprint=input_fingerprint,
        )

    def require_release_allowed(
        self,
        decision: ProductComplianceDecision,
        *,
        project_id: str | None = None,
        dependencies: tuple[DependencyAdoption, ...] | None = None,
        obligation_evidence: tuple[DistributionObligationEvidence, ...] | None = None,
        competitor_evidence: tuple[CompetitorResearchEvidence, ...] | None = None,
        dependency_closure_ref: str | None = None,
        scope_review_ref: str | None = None,
    ) -> None:
        if not isinstance(decision, ProductComplianceDecision):
            raise ProductComplianceError("release requires ProductComplianceDecision")
        if project_id is not None and decision.project_id != project_id:
            raise ProductComplianceError("release compliance decision project mismatch")
        if not decision.allowed:
            findings = decision.findings
            raw_allowed = object.__getattribute__(decision, "allowed")
            if raw_allowed and not _valid_decision_proof(decision):
                findings = (*findings, "decision:untrusted-origin")
            joined = ", ".join(dict.fromkeys(findings))
            raise ProductComplianceError(f"release blocked by PF10 compliance gate: {joined}")
        if dependencies is None or obligation_evidence is None or competitor_evidence is None:
            raise ProductComplianceError(
                "release requires current PF10 dependency, obligation and research inputs"
            )
        current_fingerprint = compliance_input_fingerprint(
            project_id=project_id or decision.project_id,
            dependencies=dependencies,
            obligation_evidence=obligation_evidence,
            competitor_evidence=competitor_evidence,
            dependency_closure_ref=dependency_closure_ref,
            scope_review_ref=scope_review_ref,
        )
        if decision.input_fingerprint != current_fingerprint:
            raise ProductComplianceError(
                "release blocked by stale PF10 compliance decision inputs"
            )

    def _review_allowed(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool:
        authority = self._review_authority
        if authority is None:
            return False
        try:
            result = authority.verify(
                project_id=project_id,
                evidence_ref=evidence_ref,
                purpose=purpose,
            )
        except (LookupError, PermissionError, RuntimeError, TypeError, ValueError):
            return False
        return result is True


def compliance_input_fingerprint(
    *,
    project_id: str,
    dependencies: tuple[DependencyAdoption, ...],
    obligation_evidence: tuple[DistributionObligationEvidence, ...],
    competitor_evidence: tuple[CompetitorResearchEvidence, ...],
    dependency_closure_ref: str | None,
    scope_review_ref: str | None,
) -> str:
    _require_text(project_id, "project_id")
    _require_tuple(dependencies, "dependencies")
    _require_tuple(obligation_evidence, "obligation_evidence")
    _require_tuple(competitor_evidence, "competitor_evidence")
    payload = {
        "schema": "nika-pf10-compliance-input-v2",
        "project_id": project_id,
        "dependencies": sorted(_dependency_payload(item) for item in dependencies),
        "obligation_evidence": sorted(_obligation_payload(item) for item in obligation_evidence),
        "competitor_evidence": sorted(_competitor_payload(item) for item in competitor_evidence),
        "dependency_closure_ref": dependency_closure_ref,
        "scope_review_ref": scope_review_ref,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _issue_decision(
    *,
    project_id: str,
    allowed: bool,
    findings: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    input_fingerprint: str,
) -> ProductComplianceDecision:
    proof = _decision_proof(
        project_id=project_id,
        allowed=allowed,
        findings=findings,
        evidence_refs=evidence_refs,
        input_fingerprint=input_fingerprint,
    )
    return ProductComplianceDecision(
        project_id=project_id,
        allowed=allowed,
        findings=findings,
        evidence_refs=evidence_refs,
        input_fingerprint=input_fingerprint,
        _authority_proof=proof,
    )


def _decision_proof(
    *,
    project_id: str,
    allowed: bool,
    findings: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    input_fingerprint: str | None,
) -> str:
    payload = {
        "schema": "nika-pf10-decision-authority-v2",
        "project_id": project_id,
        "allowed": allowed,
        "findings": list(findings),
        "evidence_refs": list(evidence_refs),
        "input_fingerprint": input_fingerprint,
    }
    return hmac.new(_DECISION_AUTHORITY_KEY, _canonical_json(payload), hashlib.sha256).hexdigest()


def _valid_decision_proof(decision: ProductComplianceDecision) -> bool:
    proof = object.__getattribute__(decision, "_authority_proof")
    if not isinstance(proof, str) or not proof:
        return False
    expected = _decision_proof(
        project_id=object.__getattribute__(decision, "project_id"),
        allowed=object.__getattribute__(decision, "allowed"),
        findings=object.__getattribute__(decision, "findings"),
        evidence_refs=object.__getattribute__(decision, "evidence_refs"),
        input_fingerprint=object.__getattribute__(decision, "input_fingerprint"),
    )
    return hmac.compare_digest(proof, expected)


def _dependency_payload(item: DependencyAdoption) -> str:
    return _canonical_json(
        {
            "project_id": item.project_id,
            "component_id": item.component_id,
            "package_name": item.package_name,
            "version": item.version,
            "source_ref": item.source_ref,
            "provenance_ref": item.provenance_ref,
            "license_expression": item.license_expression,
            "license_disposition": item.license_disposition.value,
            "distribution_obligations": sorted(item.distribution_obligations),
            "notice_required": item.notice_required,
            "notice_refs": sorted(item.notice_refs),
            "review_ref": item.review_ref,
        }
    ).decode("utf-8")


def _obligation_payload(item: DistributionObligationEvidence) -> str:
    return _canonical_json(
        {
            "project_id": item.project_id,
            "component_id": item.component_id,
            "obligation": item.obligation,
            "fulfillment_ref": item.fulfillment_ref,
        }
    ).decode("utf-8")


def _competitor_payload(item: CompetitorResearchEvidence) -> str:
    return _canonical_json(
        {
            "project_id": item.project_id,
            "evidence_id": item.evidence_id,
            "source_ref": item.source_ref,
            "provenance_ref": item.provenance_ref,
            "permitted_public_evidence": item.permitted_public_evidence,
            "proprietary_material": item.proprietary_material,
            "permission_basis_ref": item.permission_basis_ref,
            "legal_basis_ref": item.legal_basis_ref,
            "reuse_authorization_ref": item.reuse_authorization_ref,
        }
    ).decode("utf-8")


def _dependency_fingerprint(item: DependencyAdoption) -> str:
    return hashlib.sha256(_dependency_payload(item).encode("utf-8")).hexdigest()


def _competitor_fingerprint(item: CompetitorResearchEvidence) -> str:
    return hashlib.sha256(_competitor_payload(item).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_package_name(value: str) -> str:
    return _PACKAGE_SEPARATOR_RE.sub("-", value.strip().casefold())


def _mutable_version(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in _MUTABLE_VERSION_TOKENS:
        return True
    return any(marker in normalized for marker in ("*", "<", ">", "=", "~", "^", ","))


def _mutable_source(value: str) -> bool:
    normalized = value.strip().casefold()
    tokens = {token for token in re.split(r"[^a-z0-9._+-]+", normalized) if token}
    return bool(tokens & _MUTABLE_SOURCE_TOKENS)


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
