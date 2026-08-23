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
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PACKAGE_SEPARATOR_RE = re.compile(r"[-_.]+")
_UNKNOWN_LICENSE_EXPRESSIONS = frozenset({"UNKNOWN", "NOASSERTION"})


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


@dataclass(frozen=True, slots=True)
class PackagedDependencyEvidence:
    """Exact package/version and notice section observed by the packaging authority."""

    package_name: str
    version: str
    notice_ref: str
    notice_sha256: str

    def __post_init__(self) -> None:
        _require_text(self.package_name, "packaged dependency package_name")
        _require_text(self.version, "packaged dependency version")
        _require_text(self.notice_ref, "packaged dependency notice_ref")
        _require_sha256(self.notice_sha256, "packaged dependency notice_sha256")

    @property
    def identity(self) -> tuple[str, str]:
        return (_canonical_package_name(self.package_name), self.version)


class PackagingComplianceAuthorityPort(Protocol):
    """Trusted packaging boundary that reports and verifies the exact distributed inventory."""

    def inventory(self, *, project_id: str) -> tuple[PackagedDependencyEvidence, ...]: ...

    def verify_notice(
        self,
        *,
        project_id: str,
        package: PackagedDependencyEvidence,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class DependencyAdoption:
    project_id: str
    component_id: str
    package_name: str
    version: str
    source_ref: str
    source_sha256: str
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
        _require_sha256(self.source_sha256, "dependency source_sha256")
        _require_text(self.provenance_ref, "dependency provenance_ref")
        _require_text(self.license_expression, "dependency license_expression")
        if self.license_expression.strip().upper() in _UNKNOWN_LICENSE_EXPRESSIONS:
            raise ProductComplianceError("dependency license_expression is unknown or unasserted")
        if not isinstance(self.license_disposition, LicenseDisposition):
            raise ProductComplianceError("dependency license_disposition is invalid")
        _require_unique_text(self.distribution_obligations, "distribution obligation")
        _require_unique_text(self.notice_refs, "dependency notice_ref")
        if not isinstance(self.notice_required, bool):
            raise ProductComplianceError("dependency notice_required must be a boolean")
        if not self.notice_required and self.notice_refs:
            raise ProductComplianceError("dependency notice_refs are orphaned without notice_required")
        if self.review_ref is not None:
            _require_text(self.review_ref, "dependency review_ref")
        if self.license_disposition is LicenseDisposition.APPROVED and self.review_ref is None:
            raise ProductComplianceError(
                "approved dependency license requires authorized review_ref evidence"
            )

    @property
    def physical_identity(self) -> tuple[str, str, str]:
        return (_canonical_package_name(self.package_name), self.version, self.source_sha256)

    @property
    def review_fingerprint(self) -> str:
        return _sha256_json(_dependency_payload(self))

    @property
    def license_review_purpose(self) -> str:
        return (
            f"license-disposition:{self.component_id}:"
            f"sha256:{self.review_fingerprint}"
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

    @property
    def review_fingerprint(self) -> str:
        return _sha256_json(_competitor_payload(self))

    @property
    def public_permission_purpose(self) -> str:
        return (
            f"public-source-permission:{self.evidence_id}:"
            f"sha256:{self.review_fingerprint}"
        )

    @property
    def proprietary_legal_purpose(self) -> str:
        return (
            f"proprietary-legal-basis:{self.evidence_id}:"
            f"sha256:{self.review_fingerprint}"
        )

    @property
    def proprietary_reuse_purpose(self) -> str:
        return (
            f"proprietary-reuse-authorization:{self.evidence_id}:"
            f"sha256:{self.review_fingerprint}"
        )


@dataclass(frozen=True, slots=True)
class ProductComplianceSnapshot:
    """Exact PF10 input state that a release decision authorizes."""

    project_id: str
    dependencies: tuple[DependencyAdoption, ...]
    obligation_evidence: tuple[DistributionObligationEvidence, ...]
    competitor_evidence: tuple[CompetitorResearchEvidence, ...]
    packaged_dependencies: tuple[PackagedDependencyEvidence, ...]
    scope_review_ref: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.project_id, "compliance snapshot project_id")
        _require_tuple(self.dependencies, "compliance snapshot dependencies")
        _require_tuple(self.obligation_evidence, "compliance snapshot obligation_evidence")
        _require_tuple(self.competitor_evidence, "compliance snapshot competitor_evidence")
        _require_tuple(self.packaged_dependencies, "compliance snapshot packaged_dependencies")
        if self.scope_review_ref is not None:
            _require_text(self.scope_review_ref, "compliance snapshot scope_review_ref")

    @property
    def fingerprint(self) -> str:
        return _sha256_json(_snapshot_payload(self))

    @property
    def scope_review_purpose(self) -> str:
        return f"compliance-scope:sha256:{self.fingerprint}"


@dataclass(frozen=True, slots=True)
class ProductComplianceDecision:
    """PF10 result whose positive authority is issued only by this process's gate."""

    project_id: str
    snapshot_fingerprint: str
    allowed: bool
    findings: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    _authority_proof: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_text(self.project_id, "compliance decision project_id")
        _require_sha256(self.snapshot_fingerprint, "compliance decision snapshot_fingerprint")
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
    """PF10 fail-closed adoption/release policy over trusted review and packaging evidence."""

    def __init__(
        self,
        *,
        review_authority: ComplianceReviewAuthorityPort | None = None,
        packaging_authority: PackagingComplianceAuthorityPort | None = None,
    ) -> None:
        self._review_authority = review_authority
        self._packaging_authority = packaging_authority

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

        packaged_dependencies, packaging_findings = self._packaging_inventory(project_id)
        snapshot = ProductComplianceSnapshot(
            project_id=project_id,
            dependencies=dependencies,
            obligation_evidence=obligation_evidence,
            competitor_evidence=competitor_evidence,
            packaged_dependencies=packaged_dependencies,
            scope_review_ref=scope_review_ref,
        )
        findings: list[str] = list(packaging_findings)
        evidence_refs: list[str] = [
            f"compliance-snapshot:sha256:{snapshot.fingerprint}",
        ]
        component_ids: set[str] = set()
        physical_dependencies: set[tuple[str, str, str]] = set()
        declared_package_versions: dict[tuple[str, str], DependencyAdoption] = {}

        if scope_review_ref is None:
            findings.append("compliance-scope:unreviewed")
        else:
            evidence_refs.append(scope_review_ref)
            if not self._review_allowed(
                project_id=project_id,
                evidence_ref=scope_review_ref,
                purpose=snapshot.scope_review_purpose,
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
            if dependency.physical_identity in physical_dependencies:
                findings.append(
                    "duplicate:physical-dependency:"
                    f"{_canonical_package_name(dependency.package_name)}:"
                    f"{dependency.version}:{dependency.source_sha256}"
                )
            else:
                physical_dependencies.add(dependency.physical_identity)

            package_version = (_canonical_package_name(dependency.package_name), dependency.version)
            if package_version in declared_package_versions:
                findings.append(
                    "duplicate:package-version:"
                    f"{package_version[0]}:{package_version[1]}"
                )
            else:
                declared_package_versions[package_version] = dependency

            evidence_refs.extend(
                (
                    dependency.source_ref,
                    f"source-sha256:{dependency.source_sha256}",
                    dependency.provenance_ref,
                )
            )
            if dependency.review_ref:
                evidence_refs.append(dependency.review_ref)
            evidence_refs.extend(dependency.notice_refs)

            if dependency.license_disposition is LicenseDisposition.BLOCKED:
                findings.append(f"license:blocked:{dependency.component_id}")
            elif dependency.license_disposition is LicenseDisposition.REVIEW_REQUIRED:
                findings.append(f"license:review-required:{dependency.component_id}")
            elif dependency.review_ref is not None:
                if not self._review_allowed(
                    project_id=project_id,
                    evidence_ref=dependency.review_ref,
                    purpose=dependency.license_review_purpose,
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

        for component_id, obligation in obligations:
            if component_id not in component_ids:
                findings.append(
                    f"orphan:distribution-obligation:{component_id}:{obligation}"
                )

        packaged_by_identity: dict[tuple[str, str], PackagedDependencyEvidence] = {}
        for package in packaged_dependencies:
            evidence_refs.extend(
                (
                    package.notice_ref,
                    f"notice-sha256:{package.notice_sha256}",
                )
            )
            if package.identity in packaged_by_identity:
                findings.append(
                    "duplicate:packaged-dependency:"
                    f"{package.identity[0]}:{package.identity[1]}"
                )
                continue
            packaged_by_identity[package.identity] = package
            dependency = declared_package_versions.get(package.identity)
            if dependency is None:
                findings.append(
                    "packaging:undeclared-dependency:"
                    f"{package.identity[0]}:{package.identity[1]}"
                )
                continue
            if not dependency.notice_required:
                findings.append(f"notice:orphan:{dependency.component_id}")
                continue
            if package.notice_ref not in dependency.notice_refs:
                findings.append(f"notice:mismatch:{dependency.component_id}")
                continue
            if not self._notice_allowed(project_id=project_id, package=package):
                findings.append(f"notice:untrusted-packaging-authority:{dependency.component_id}")

        for package_identity, dependency in declared_package_versions.items():
            if package_identity not in packaged_by_identity:
                findings.append(f"packaging:missing-dependency:{dependency.component_id}")

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
                        purpose=evidence.proprietary_legal_purpose,
                    )
                    reuse_ok = self._review_allowed(
                        project_id=project_id,
                        evidence_ref=evidence.reuse_authorization_ref,
                        purpose=evidence.proprietary_reuse_purpose,
                    )
                    if not legal_ok or not reuse_ok:
                        findings.append(
                            f"proprietary-reuse:untrusted-authority:{evidence.evidence_id}"
                        )
            elif evidence.permitted_public_evidence:
                permission_ref = evidence.permission_basis_ref
                if permission_ref is None or not self._review_allowed(
                    project_id=project_id,
                    evidence_ref=permission_ref or "missing",
                    purpose=evidence.public_permission_purpose,
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
            snapshot_fingerprint=snapshot.fingerprint,
            allowed=not normalized_findings,
            findings=normalized_findings,
            evidence_refs=normalized_evidence,
        )

    def require_release_allowed(
        self,
        decision: ProductComplianceDecision,
        *,
        current_snapshot: ProductComplianceSnapshot,
    ) -> None:
        if not isinstance(decision, ProductComplianceDecision):
            raise ProductComplianceError("release requires ProductComplianceDecision")
        if not isinstance(current_snapshot, ProductComplianceSnapshot):
            raise ProductComplianceError("release requires exact ProductComplianceSnapshot")
        if decision.project_id != current_snapshot.project_id:
            raise ProductComplianceError("release blocked: compliance decision project mismatch")
        if decision.snapshot_fingerprint != current_snapshot.fingerprint:
            raise ProductComplianceError("release blocked: compliance decision is stale for current snapshot")
        if not decision.allowed:
            findings = decision.findings
            raw_allowed = object.__getattribute__(decision, "allowed")
            if raw_allowed and not _valid_decision_proof(decision):
                findings = (*findings, "decision:untrusted-origin")
            joined = ", ".join(dict.fromkeys(findings))
            raise ProductComplianceError(f"release blocked by PF10 compliance gate: {joined}")

    def snapshot(
        self,
        *,
        project_id: str,
        dependencies: tuple[DependencyAdoption, ...] = (),
        obligation_evidence: tuple[DistributionObligationEvidence, ...] = (),
        competitor_evidence: tuple[CompetitorResearchEvidence, ...] = (),
        scope_review_ref: str | None = None,
    ) -> ProductComplianceSnapshot:
        packaged_dependencies, findings = self._packaging_inventory(project_id)
        if findings:
            raise ProductComplianceError(
                "cannot build release snapshot without trusted packaging inventory: "
                + ", ".join(findings)
            )
        return ProductComplianceSnapshot(
            project_id=project_id,
            dependencies=dependencies,
            obligation_evidence=obligation_evidence,
            competitor_evidence=competitor_evidence,
            packaged_dependencies=packaged_dependencies,
            scope_review_ref=scope_review_ref,
        )

    def _packaging_inventory(
        self,
        project_id: str,
    ) -> tuple[tuple[PackagedDependencyEvidence, ...], tuple[str, ...]]:
        authority = self._packaging_authority
        if authority is None:
            return (), ("packaging:untrusted-inventory-authority",)
        try:
            inventory = authority.inventory(project_id=project_id)
        except (LookupError, PermissionError, RuntimeError, TypeError, ValueError):
            return (), ("packaging:untrusted-inventory-authority",)
        if not isinstance(inventory, tuple) or not all(
            isinstance(item, PackagedDependencyEvidence) for item in inventory
        ):
            return (), ("packaging:invalid-inventory",)
        return inventory, ()

    def _notice_allowed(
        self,
        *,
        project_id: str,
        package: PackagedDependencyEvidence,
    ) -> bool:
        authority = self._packaging_authority
        if authority is None:
            return False
        try:
            result = authority.verify_notice(project_id=project_id, package=package)
        except (LookupError, PermissionError, RuntimeError, TypeError, ValueError):
            return False
        return result is True

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


def _issue_decision(
    *,
    project_id: str,
    snapshot_fingerprint: str,
    allowed: bool,
    findings: tuple[str, ...],
    evidence_refs: tuple[str, ...],
) -> ProductComplianceDecision:
    proof = _decision_proof(
        project_id=project_id,
        snapshot_fingerprint=snapshot_fingerprint,
        allowed=allowed,
        findings=findings,
        evidence_refs=evidence_refs,
    )
    return ProductComplianceDecision(
        project_id=project_id,
        snapshot_fingerprint=snapshot_fingerprint,
        allowed=allowed,
        findings=findings,
        evidence_refs=evidence_refs,
        _authority_proof=proof,
    )


def _decision_proof(
    *,
    project_id: str,
    snapshot_fingerprint: str,
    allowed: bool,
    findings: tuple[str, ...],
    evidence_refs: tuple[str, ...],
) -> str:
    payload = json.dumps(
        {
            "schema": "nika-pf10-decision-authority-v2",
            "project_id": project_id,
            "snapshot_fingerprint": snapshot_fingerprint,
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
        snapshot_fingerprint=object.__getattribute__(decision, "snapshot_fingerprint"),
        allowed=object.__getattribute__(decision, "allowed"),
        findings=object.__getattribute__(decision, "findings"),
        evidence_refs=object.__getattribute__(decision, "evidence_refs"),
    )
    return hmac.compare_digest(proof, expected)


def _snapshot_payload(snapshot: ProductComplianceSnapshot) -> dict[str, object]:
    return {
        "schema": "nika-pf10-compliance-snapshot-v2",
        "project_id": snapshot.project_id,
        "scope_review_ref": snapshot.scope_review_ref,
        "dependencies": sorted(
            (_dependency_payload(item) for item in snapshot.dependencies),
            key=lambda item: (
                str(item["component_id"]),
                str(item["package_name"]),
                str(item["version"]),
            ),
        ),
        "obligation_evidence": sorted(
            (
                {
                    "project_id": item.project_id,
                    "component_id": item.component_id,
                    "obligation": item.obligation,
                    "fulfillment_ref": item.fulfillment_ref,
                }
                for item in snapshot.obligation_evidence
            ),
            key=lambda item: (
                str(item["project_id"]),
                str(item["component_id"]),
                str(item["obligation"]),
                str(item["fulfillment_ref"]),
            ),
        ),
        "competitor_evidence": sorted(
            (_competitor_payload(item) for item in snapshot.competitor_evidence),
            key=lambda item: (str(item["project_id"]), str(item["evidence_id"])),
        ),
        "packaged_dependencies": sorted(
            (
                {
                    "package_name": _canonical_package_name(item.package_name),
                    "version": item.version,
                    "notice_ref": item.notice_ref,
                    "notice_sha256": item.notice_sha256,
                }
                for item in snapshot.packaged_dependencies
            ),
            key=lambda item: (
                str(item["package_name"]),
                str(item["version"]),
                str(item["notice_ref"]),
            ),
        ),
    }


def _dependency_payload(dependency: DependencyAdoption) -> dict[str, object]:
    return {
        "project_id": dependency.project_id,
        "component_id": dependency.component_id,
        "package_name": _canonical_package_name(dependency.package_name),
        "version": dependency.version,
        "source_ref": dependency.source_ref,
        "source_sha256": dependency.source_sha256,
        "provenance_ref": dependency.provenance_ref,
        "license_expression": dependency.license_expression,
        "license_disposition": dependency.license_disposition.value,
        "distribution_obligations": sorted(dependency.distribution_obligations),
        "notice_required": dependency.notice_required,
        "notice_refs": sorted(dependency.notice_refs),
        "review_ref": dependency.review_ref,
    }


def _competitor_payload(evidence: CompetitorResearchEvidence) -> dict[str, object]:
    return {
        "project_id": evidence.project_id,
        "evidence_id": evidence.evidence_id,
        "source_ref": evidence.source_ref,
        "provenance_ref": evidence.provenance_ref,
        "permitted_public_evidence": evidence.permitted_public_evidence,
        "proprietary_material": evidence.proprietary_material,
        "permission_basis_ref": evidence.permission_basis_ref,
        "legal_basis_ref": evidence.legal_basis_ref,
        "reuse_authorization_ref": evidence.reuse_authorization_ref,
    }


def _sha256_json(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_package_name(value: str) -> str:
    return _PACKAGE_SEPARATOR_RE.sub("-", value).casefold()


def _require_sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ProductComplianceError(f"{label} must be exact lowercase SHA-256")


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
