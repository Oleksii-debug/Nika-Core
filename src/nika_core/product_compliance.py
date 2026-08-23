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
_UNKNOWN_LICENSE_EXPRESSIONS = frozenset({"UNKNOWN", "NOASSERTION", "NONE", "UNLICENSED"})
_MUTABLE_SOURCE_TOKENS = frozenset({"latest", "head", "main", "master"})
_EXACT_VERSION_FORBIDDEN = re.compile(r"[<>=~^*,\s]")


class ProductComplianceError(ValueError):
    pass


class LicenseDisposition(StrEnum):
    APPROVED = "approved"
    REVIEW_REQUIRED = "review_required"
    BLOCKED = "blocked"


class ComplianceReviewAuthorityPort(Protocol):
    """Trusted resolver for PF10 review/legal/permission evidence references."""

    def verify(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool: ...


class ComplianceNoticeAuthorityPort(Protocol):
    """Trusted resolver for notice references emitted by the packaging notice subsystem."""

    def verify(
        self,
        *,
        project_id: str,
        package_name: str,
        version: str,
        notice_ref: str,
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
    snapshot_sha256: str = ""
    _authority_proof: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_text(self.project_id, "compliance decision project_id")
        if not isinstance(object.__getattribute__(self, "allowed"), bool):
            raise ProductComplianceError("compliance decision allowed must be a boolean")
        _require_unique_text(self.findings, "compliance finding", allow_empty=True)
        _require_unique_text(self.evidence_refs, "compliance evidence_ref", allow_empty=True)
        if self.snapshot_sha256 and not re.fullmatch(r"[0-9a-f]{64}", self.snapshot_sha256):
            raise ProductComplianceError("compliance decision snapshot_sha256 is invalid")
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
    """PF10 fail-closed adoption/release policy over authority-resolved evidence."""

    def __init__(
        self,
        *,
        review_authority: ComplianceReviewAuthorityPort | None = None,
        notice_authority: ComplianceNoticeAuthorityPort | None = None,
    ) -> None:
        self._review_authority = review_authority
        self._notice_authority = notice_authority

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

        snapshot_sha256 = _compliance_snapshot_sha256(
            project_id=project_id,
            dependencies=dependencies,
            obligation_evidence=obligation_evidence,
            competitor_evidence=competitor_evidence,
            scope_review_ref=scope_review_ref,
        )
        findings: list[str] = []
        evidence_refs: list[str] = []
        trusted_scope_refs: set[str] = set()
        component_ids: set[str] = set()
        physical_dependencies: set[tuple[str, str, str, str]] = set()

        if scope_review_ref is not None:
            evidence_refs.append(scope_review_ref)
            if self._review_allowed(
                project_id=project_id,
                evidence_ref=scope_review_ref,
                purpose="compliance-scope",
            ):
                trusted_scope_refs.add(scope_review_ref)
            else:
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

            physical_identity = (
                _normalize_package_name(dependency.package_name),
                dependency.version,
                dependency.source_ref,
                dependency.provenance_ref,
            )
            if physical_identity in physical_dependencies:
                findings.append(
                    f"duplicate:dependency-physical-identity:{dependency.package_name}:{dependency.version}"
                )
            else:
                physical_dependencies.add(physical_identity)

            evidence_refs.extend((dependency.source_ref, dependency.provenance_ref))
            if dependency.review_ref:
                evidence_refs.append(dependency.review_ref)
            evidence_refs.extend(dependency.notice_refs)

            if not _is_exact_version(dependency.version):
                findings.append(f"dependency-version:not-exact:{dependency.component_id}")
            if _source_ref_is_mutable(dependency.source_ref):
                findings.append(f"dependency-source:mutable:{dependency.component_id}")
            if not _content_addressed_provenance(dependency.provenance_ref):
                findings.append(f"dependency-provenance:not-content-addressed:{dependency.component_id}")
            if dependency.license_expression.strip().upper() in _UNKNOWN_LICENSE_EXPRESSIONS:
                findings.append(f"license:unknown:{dependency.component_id}")

            if dependency.license_disposition is LicenseDisposition.BLOCKED:
                findings.append(f"license:blocked:{dependency.component_id}")
            elif dependency.license_disposition is LicenseDisposition.REVIEW_REQUIRED:
                findings.append(f"license:review-required:{dependency.component_id}")
            elif dependency.review_ref is not None:
                purpose = f"license-disposition:{dependency.component_id}"
                if self._review_allowed(
                    project_id=project_id,
                    evidence_ref=dependency.review_ref,
                    purpose=purpose,
                ):
                    trusted_scope_refs.add(dependency.review_ref)
                else:
                    findings.append(
                        f"license:untrusted-review-authority:{dependency.component_id}"
                    )

            for obligation in dependency.distribution_obligations:
                if (dependency.component_id, obligation) not in obligations:
                    findings.append(
                        "distribution-obligation:unfulfilled:"
                        f"{dependency.component_id}:{obligation}"
                    )

            if dependency.notice_required:
                if not dependency.notice_refs:
                    findings.append(f"notice:missing:{dependency.component_id}")
                for notice_ref in dependency.notice_refs:
                    if not self._notice_allowed(
                        project_id=project_id,
                        package_name=dependency.package_name,
                        version=dependency.version,
                        notice_ref=notice_ref,
                    ):
                        findings.append(f"notice:untrusted:{dependency.component_id}:{notice_ref}")
            elif dependency.notice_refs:
                findings.append(f"orphan:notice:{dependency.component_id}")

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
                        purpose=f"proprietary-legal-basis:{evidence.evidence_id}",
                    )
                    reuse_ok = self._review_allowed(
                        project_id=project_id,
                        evidence_ref=evidence.reuse_authorization_ref,
                        purpose=f"proprietary-reuse-authorization:{evidence.evidence_id}",
                    )
                    if legal_ok and reuse_ok:
                        trusted_scope_refs.update(
                            (evidence.legal_basis_ref, evidence.reuse_authorization_ref)
                        )
                    else:
                        findings.append(
                            f"proprietary-reuse:untrusted-authority:{evidence.evidence_id}"
                        )
            elif evidence.permitted_public_evidence:
                permission_ref = evidence.permission_basis_ref
                if permission_ref is None:
                    findings.append(
                        f"research-source:untrusted-permission-authority:{evidence.evidence_id}"
                    )
                elif self._review_allowed(
                    project_id=project_id,
                    evidence_ref=permission_ref,
                    purpose=f"public-source-permission:{evidence.evidence_id}",
                ):
                    trusted_scope_refs.add(permission_ref)
                else:
                    findings.append(
                        f"research-source:untrusted-permission-authority:{evidence.evidence_id}"
                    )
            else:
                findings.append(f"research-source:not-permitted:{evidence.evidence_id}")

        if not trusted_scope_refs:
            findings.append("compliance-scope:unreviewed")

        snapshot_ref = f"compliance-snapshot:sha256:{snapshot_sha256}"
        evidence_refs.append(snapshot_ref)
        normalized_findings = tuple(dict.fromkeys(findings))
        normalized_evidence = tuple(dict.fromkeys(evidence_refs))
        return _issue_decision(
            project_id=project_id,
            allowed=not normalized_findings,
            findings=normalized_findings,
            evidence_refs=normalized_evidence,
            snapshot_sha256=snapshot_sha256,
        )

    def require_release_allowed(
        self,
        decision: ProductComplianceDecision,
        *,
        expected_snapshot_sha256: str | None = None,
    ) -> None:
        if not isinstance(decision, ProductComplianceDecision):
            raise ProductComplianceError("release requires ProductComplianceDecision")
        if expected_snapshot_sha256 is not None:
            if not re.fullmatch(r"[0-9a-f]{64}", expected_snapshot_sha256):
                raise ProductComplianceError("expected compliance snapshot SHA-256 is invalid")
            if decision.snapshot_sha256 != expected_snapshot_sha256:
                raise ProductComplianceError(
                    "release blocked by PF10 compliance gate: decision:stale-snapshot"
                )
        if not decision.allowed:
            findings = decision.findings
            raw_allowed = object.__getattribute__(decision, "allowed")
            if raw_allowed and not _valid_decision_proof(decision):
                findings = (*findings, "decision:untrusted-origin")
            joined = ", ".join(dict.fromkeys(findings))
            raise ProductComplianceError(f"release blocked by PF10 compliance gate: {joined}")

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

    def _notice_allowed(
        self,
        *,
        project_id: str,
        package_name: str,
        version: str,
        notice_ref: str,
    ) -> bool:
        authority = self._notice_authority
        if authority is None:
            return False
        try:
            result = authority.verify(
                project_id=project_id,
                package_name=package_name,
                version=version,
                notice_ref=notice_ref,
            )
        except (LookupError, PermissionError, RuntimeError, TypeError, ValueError):
            return False
        return result is True


def _issue_decision(
    *,
    project_id: str,
    allowed: bool,
    findings: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    snapshot_sha256: str,
) -> ProductComplianceDecision:
    proof = _decision_proof(
        project_id=project_id,
        allowed=allowed,
        findings=findings,
        evidence_refs=evidence_refs,
        snapshot_sha256=snapshot_sha256,
    )
    return ProductComplianceDecision(
        project_id=project_id,
        allowed=allowed,
        findings=findings,
        evidence_refs=evidence_refs,
        snapshot_sha256=snapshot_sha256,
        _authority_proof=proof,
    )


def _decision_proof(
    *,
    project_id: str,
    allowed: bool,
    findings: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    snapshot_sha256: str,
) -> str:
    payload = json.dumps(
        {
            "schema": "nika-pf10-decision-authority-v2",
            "project_id": project_id,
            "allowed": allowed,
            "findings": list(findings),
            "evidence_refs": list(evidence_refs),
            "snapshot_sha256": snapshot_sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_DECISION_AUTHORITY_KEY, payload, hashlib.sha256).hexdigest()


def _valid_decision_proof(decision: ProductComplianceDecision) -> bool:
    proof = object.__getattribute__(decision, "_authority_proof")
    snapshot_sha256 = object.__getattribute__(decision, "snapshot_sha256")
    if (
        not isinstance(proof, str)
        or not proof
        or not isinstance(snapshot_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", snapshot_sha256) is None
    ):
        return False
    expected = _decision_proof(
        project_id=object.__getattribute__(decision, "project_id"),
        allowed=object.__getattribute__(decision, "allowed"),
        findings=object.__getattribute__(decision, "findings"),
        evidence_refs=object.__getattribute__(decision, "evidence_refs"),
        snapshot_sha256=snapshot_sha256,
    )
    return hmac.compare_digest(proof, expected)


def _compliance_snapshot_sha256(
    *,
    project_id: str,
    dependencies: tuple[DependencyAdoption, ...],
    obligation_evidence: tuple[DistributionObligationEvidence, ...],
    competitor_evidence: tuple[CompetitorResearchEvidence, ...],
    scope_review_ref: str | None,
) -> str:
    dependency_payloads = [
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
        for item in dependencies
    ]
    obligation_payloads = [
        {
            "project_id": item.project_id,
            "component_id": item.component_id,
            "obligation": item.obligation,
            "fulfillment_ref": item.fulfillment_ref,
        }
        for item in obligation_evidence
    ]
    competitor_payloads = [
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
        for item in competitor_evidence
    ]
    payload = {
        "schema": "nika-pf10-compliance-snapshot-v1",
        "project_id": project_id,
        "scope_review_ref": scope_review_ref,
        "dependencies": _sort_payloads(dependency_payloads),
        "obligation_evidence": _sort_payloads(obligation_payloads),
        "competitor_evidence": _sort_payloads(competitor_payloads),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sort_payloads(items: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(
        items,
        key=lambda item: json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _normalize_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.strip()).casefold()


def _is_exact_version(value: str) -> bool:
    candidate = value.strip()
    return bool(candidate) and not _EXACT_VERSION_FORBIDDEN.search(candidate) and (
        candidate.casefold() not in _MUTABLE_SOURCE_TOKENS
    )


def _source_ref_is_mutable(value: str) -> bool:
    tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", value.casefold())
        if token
    }
    return bool(tokens & _MUTABLE_SOURCE_TOKENS)


def _content_addressed_provenance(value: str) -> bool:
    candidate = value.strip().casefold()
    for prefix in ("hash:sha256:", "sha256:", "digest:sha256:"):
        if candidate.startswith(prefix) and candidate.removeprefix(prefix).strip():
            return True
    return False


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
