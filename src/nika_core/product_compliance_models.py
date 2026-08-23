from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PACKAGE_NORMALIZE_RE = re.compile(r"[-_.]+")
_UNKNOWN_LICENSE_EXPRESSIONS = frozenset(
    {"unknown", "noassertion", "n/a", "na", "none", "unspecified"}
)


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
class DependencyAdoption:
    project_id: str
    component_id: str
    package_name: str
    version: str
    source_ref: str
    provenance_ref: str
    license_expression: str
    license_disposition: LicenseDisposition
    source_sha256: str | None = None
    parent_component_ids: tuple[str, ...] = ()
    distribution_obligations: tuple[str, ...] = ()
    notice_required: bool = False
    notice_refs: tuple[str, ...] = ()
    review_ref: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.project_id, "dependency project_id")
        _require_text(self.component_id, "dependency component_id")
        _require_text(self.package_name, "dependency package_name")
        _require_exact_version(self.version)
        _require_text(self.source_ref, "dependency source_ref")
        _require_text(self.provenance_ref, "dependency provenance_ref")
        _require_known_license_expression(self.license_expression)
        if not isinstance(self.license_disposition, LicenseDisposition):
            raise ProductComplianceError("dependency license_disposition is invalid")
        if self.source_sha256 is not None:
            _require_sha256(self.source_sha256, "dependency source_sha256")
        _require_unique_text(self.parent_component_ids, "dependency parent component id")
        if self.component_id in self.parent_component_ids:
            raise ProductComplianceError("dependency cannot be its own parent component")
        _require_unique_text(self.distribution_obligations, "distribution obligation")
        _require_unique_text(self.notice_refs, "dependency notice_ref")
        if not isinstance(self.notice_required, bool):
            raise ProductComplianceError("dependency notice_required must be a boolean")
        if self.review_ref is not None:
            _require_text(self.review_ref, "dependency review_ref")
        if self.license_disposition is LicenseDisposition.APPROVED and self.review_ref is None:
            raise ProductComplianceError(
                "approved dependency license requires authorized review_ref evidence"
            )


@dataclass(frozen=True, slots=True)
class PackagedDependencyEvidence:
    """Exact dependency identity observed in the package/release candidate."""

    project_id: str
    component_id: str
    package_name: str
    version: str
    source_sha256: str
    parent_component_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.project_id, "packaged dependency project_id")
        _require_text(self.component_id, "packaged dependency component_id")
        _require_text(self.package_name, "packaged dependency package_name")
        _require_exact_version(self.version)
        _require_sha256(self.source_sha256, "packaged dependency source_sha256")
        _require_unique_text(self.parent_component_ids, "packaged dependency parent component id")
        if self.component_id in self.parent_component_ids:
            raise ProductComplianceError("packaged dependency cannot be its own parent component")


@dataclass(frozen=True, slots=True)
class PackagingNoticeEvidence:
    """Notice entry produced by the canonical packaging notice path for one exact component."""

    project_id: str
    component_id: str
    package_name: str
    version: str
    notice_ref: str

    def __post_init__(self) -> None:
        _require_text(self.project_id, "notice project_id")
        _require_text(self.component_id, "notice component_id")
        _require_text(self.package_name, "notice package_name")
        _require_exact_version(self.version)
        _require_text(self.notice_ref, "notice_ref")


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


def _normalize_package_name(value: str) -> str:
    return _PACKAGE_NORMALIZE_RE.sub("-", value).casefold()


def _require_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ProductComplianceError(f"{label} must be non-empty text")


def _require_exact_version(value: object) -> None:
    _require_text(value, "dependency version")
    assert isinstance(value, str)
    if value != value.strip() or any(char.isspace() for char in value):
        raise ProductComplianceError("dependency version must be an exact version string")
    if any(marker in value for marker in ("<", ">", "=", "~", "*", ",", ";", "@")):
        raise ProductComplianceError("dependency version must not be a range or mutable reference")


def _require_known_license_expression(value: object) -> None:
    _require_text(value, "dependency license_expression")
    assert isinstance(value, str)
    if value.strip().casefold() in _UNKNOWN_LICENSE_EXPRESSIONS:
        raise ProductComplianceError("dependency license_expression is unknown or unresolved")


def _require_sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ProductComplianceError(f"{label} must be a canonical lowercase SHA-256 digest")


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
