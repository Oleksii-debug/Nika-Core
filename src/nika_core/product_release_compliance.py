from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from nika_core.packaging.notices import (
    build_third_party_notices,
    verified_third_party_notice_reference,
    verify_third_party_notices,
)
from nika_core.product_compliance import (
    CompetitorResearchEvidence,
    ComplianceReviewAuthorityPort,
    DependencyAdoption,
    DistributionObligationEvidence,
    ProductComplianceError,
    ProductComplianceGate,
)

_RELEASE_AUTHORITY_KEY = secrets.token_bytes(32)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_PACKAGE_SEPARATORS_RE = re.compile(r"[-_.]+")
_UNKNOWN_LICENSE_MARKERS = frozenset(
    {"UNKNOWN", "NOASSERTION", "NONE", "N/A", "NA", "UNLICENSED", "PROPRIETARY-UNKNOWN"}
)


@dataclass(frozen=True, slots=True)
class ReleaseDependency:
    """Exact dependency identity plus immutable source digest and dependency edges."""

    adoption: DependencyAdoption
    source_sha256: str
    requires_component_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.adoption, DependencyAdoption):
            raise TypeError("release dependency adoption must be DependencyAdoption")
        _sha256(self.source_sha256, "release dependency source_sha256")
        _unique_text(self.requires_component_ids, "required component id", allow_empty=True)
        if self.adoption.component_id in self.requires_component_ids:
            raise ProductComplianceError(
                f"dependency cannot require itself: {self.adoption.component_id}"
            )

    @property
    def canonical_package_name(self) -> str:
        return _canonical_package(self.adoption.package_name)


@dataclass(frozen=True, slots=True)
class ReleaseNoticeEvidence:
    """Exact mapping from a declared dependency notice ref to packaged evidence."""

    project_id: str
    component_id: str
    notice_ref: str
    package_name: str
    version: str

    def __post_init__(self) -> None:
        _text(self.project_id, "release notice project_id")
        _text(self.component_id, "release notice component_id")
        _text(self.notice_ref, "release notice_ref")
        _text(self.package_name, "release notice package_name")
        _text(self.version, "release notice version")


@dataclass(frozen=True, slots=True)
class ReleaseComplianceSnapshot:
    """Immutable PF10 input representing the exact release being evaluated."""

    project_id: str
    release_id: str
    project_source_ref: str
    project_source_sha256: str
    artifact_ref: str
    artifact_sha256: str
    notice_bundle_sha256: str
    dependencies: tuple[ReleaseDependency, ...]
    dependency_closure_ref: str
    obligation_evidence: tuple[DistributionObligationEvidence, ...] = ()
    notice_evidence: tuple[ReleaseNoticeEvidence, ...] = ()
    competitor_evidence: tuple[CompetitorResearchEvidence, ...] = ()
    scope_review_ref: str | None = None

    def __post_init__(self) -> None:
        _text(self.project_id, "release project_id")
        _text(self.release_id, "release_id")
        _text(self.project_source_ref, "project_source_ref")
        _sha256(self.project_source_sha256, "project_source_sha256")
        _text(self.artifact_ref, "release artifact_ref")
        _sha256(self.artifact_sha256, "release artifact_sha256")
        _sha256(self.notice_bundle_sha256, "notice_bundle_sha256")
        _tuple(self.dependencies, "release dependencies")
        _text(self.dependency_closure_ref, "release dependency_closure_ref")
        _tuple(self.obligation_evidence, "release obligation_evidence")
        _tuple(self.notice_evidence, "release notice_evidence")
        _tuple(self.competitor_evidence, "release competitor_evidence")
        if self.scope_review_ref is not None:
            _text(self.scope_review_ref, "release scope_review_ref")

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            _snapshot_payload(self),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ReleaseComplianceDecision:
    """PF10 result bound to one exact release snapshot."""

    project_id: str
    release_id: str
    artifact_ref: str
    snapshot_digest: str
    allowed: bool
    findings: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    _authority_proof: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _text(self.project_id, "release decision project_id")
        _text(self.release_id, "release decision release_id")
        _text(self.artifact_ref, "release decision artifact_ref")
        _sha256(self.snapshot_digest, "release decision snapshot_digest")
        raw_allowed = object.__getattribute__(self, "allowed")
        if not isinstance(raw_allowed, bool):
            raise TypeError("release decision allowed must be a boolean")
        _unique_text(self.findings, "release finding", allow_empty=True)
        _unique_text(self.evidence_refs, "release evidence_ref", allow_empty=True)
        if raw_allowed and self.findings:
            raise ProductComplianceError("allowed release decision cannot contain findings")
        if not raw_allowed and not self.findings:
            raise ProductComplianceError("blocked release decision requires findings")
        if self._authority_proof is not None:
            _text(self._authority_proof, "release decision authority proof")

    def __getattribute__(self, name: str):
        if name == "allowed":
            raw_allowed = object.__getattribute__(self, "allowed")
            if not raw_allowed:
                return False
            return _valid_decision_proof(self)
        return object.__getattribute__(self, name)


@dataclass(frozen=True, slots=True)
class ReleaseComplianceGrant:
    """Delivery authority issued only after current release state is revalidated."""

    project_id: str
    release_id: str
    artifact_ref: str
    artifact_sha256: str
    snapshot_digest: str
    evidence_refs: tuple[str, ...]
    _authority_proof: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _text(self.project_id, "release grant project_id")
        _text(self.release_id, "release grant release_id")
        _text(self.artifact_ref, "release grant artifact_ref")
        _sha256(self.artifact_sha256, "release grant artifact_sha256")
        _sha256(self.snapshot_digest, "release grant snapshot_digest")
        _unique_text(self.evidence_refs, "release grant evidence_ref", allow_empty=True)
        if self._authority_proof is not None:
            _text(self._authority_proof, "release grant authority proof")

    @property
    def allowed(self) -> bool:
        return _valid_grant_proof(self)


class ProductReleaseComplianceGate:
    """Fail-closed release/delivery gate over exact PF10 and packaged notices."""

    def __init__(
        self,
        *,
        review_authority: ComplianceReviewAuthorityPort | None = None,
    ) -> None:
        self._base_gate = ProductComplianceGate(review_authority=review_authority)

    def evaluate(
        self,
        snapshot: ReleaseComplianceSnapshot,
        *,
        bundle_dir: Path,
    ) -> ReleaseComplianceDecision:
        if not isinstance(snapshot, ReleaseComplianceSnapshot):
            raise TypeError("release evaluation requires ReleaseComplianceSnapshot")
        findings = [*_release_findings(snapshot), *_notice_bundle_findings(snapshot, bundle_dir)]
        base = self._base_gate.evaluate(
            project_id=snapshot.project_id,
            dependencies=tuple(item.adoption for item in snapshot.dependencies),
            obligation_evidence=snapshot.obligation_evidence,
            competitor_evidence=snapshot.competitor_evidence,
            dependency_closure_ref=snapshot.dependency_closure_ref,
            scope_review_ref=snapshot.scope_review_ref,
        )
        findings.extend(base.findings)
        evidence_refs = [
            *base.evidence_refs,
            snapshot.project_source_ref,
            f"sha256:{snapshot.project_source_sha256}",
            snapshot.artifact_ref,
            f"sha256:{snapshot.artifact_sha256}",
            f"notices:sha256:{snapshot.notice_bundle_sha256}",
            f"pf10:snapshot:{snapshot.digest}",
            *(item.notice_ref for item in snapshot.notice_evidence),
        ]
        normalized_findings = tuple(dict.fromkeys(findings))
        normalized_evidence = tuple(dict.fromkeys(evidence_refs))
        return _issue_decision(
            snapshot=snapshot,
            allowed=not normalized_findings and base.allowed,
            findings=normalized_findings,
            evidence_refs=normalized_evidence,
        )

    def require_release_allowed(
        self,
        decision: ReleaseComplianceDecision,
        snapshot: ReleaseComplianceSnapshot,
        *,
        bundle_dir: Path,
    ) -> ReleaseComplianceGrant:
        if not isinstance(decision, ReleaseComplianceDecision):
            raise ProductComplianceError("release requires an exact ReleaseComplianceDecision")
        if not isinstance(snapshot, ReleaseComplianceSnapshot):
            raise ProductComplianceError("release requires current ReleaseComplianceSnapshot")
        context_matches = (
            decision.project_id == snapshot.project_id
            and decision.release_id == snapshot.release_id
            and decision.artifact_ref == snapshot.artifact_ref
            and decision.snapshot_digest == snapshot.digest
        )
        packaging_findings = _notice_bundle_findings(snapshot, bundle_dir)
        if context_matches and not packaging_findings and decision.allowed:
            return _issue_grant(decision, snapshot)

        findings = list(decision.findings)
        if not context_matches:
            findings.append("decision:stale-or-wrong-release-snapshot")
        findings.extend(packaging_findings)
        raw_allowed = object.__getattribute__(decision, "allowed")
        if raw_allowed and not _valid_decision_proof(decision):
            findings.append("decision:untrusted-origin")
        joined = ", ".join(dict.fromkeys(findings)) or "not-authorized"
        raise ProductComplianceError(f"release blocked by exact PF10 release gate: {joined}")


def build_verified_notice_bundle(bundle_dir: Path) -> str:
    """Reuse canonical packaging notices and return the verified bundle SHA-256."""

    directory = Path(bundle_dir)
    target = build_third_party_notices(directory)
    findings = verify_third_party_notices(directory)
    if findings:
        raise ProductComplianceError(
            "third-party notice generation did not verify: " + ", ".join(findings)
        )
    return _file_sha256(target)


def _release_findings(snapshot: ReleaseComplianceSnapshot) -> tuple[str, ...]:
    findings: list[str] = []
    component_map: dict[str, ReleaseDependency] = {}
    package_map: dict[str, ReleaseDependency] = {}
    exact_identities: set[tuple[str, str, str]] = set()

    for item in snapshot.dependencies:
        adoption = item.adoption
        if adoption.project_id != snapshot.project_id:
            findings.append(f"cross-project:release-dependency:{adoption.component_id}")
            continue
        if adoption.component_id in component_map:
            findings.append(f"duplicate:release-component:{adoption.component_id}")
            continue
        component_map[adoption.component_id] = item

        package_name = item.canonical_package_name
        identity = (package_name, adoption.version, item.source_sha256.casefold())
        if identity in exact_identities:
            findings.append(
                f"duplicate:dependency-identity:{package_name}:{adoption.version}:"
                f"{item.source_sha256.casefold()}"
            )
        exact_identities.add(identity)
        previous = package_map.get(package_name)
        if previous is not None:
            if (
                previous.adoption.version != adoption.version
                or previous.source_sha256.casefold() != item.source_sha256.casefold()
            ):
                findings.append(f"conflict:dependency-package:{package_name}")
            else:
                findings.append(f"duplicate:dependency-package:{package_name}")
        else:
            package_map[package_name] = item

        provenance_digest = adoption.provenance_ref.rsplit(":", 1)[-1].casefold()
        if provenance_digest != item.source_sha256.casefold():
            findings.append(
                f"dependency-provenance:source-digest-mismatch:{adoption.component_id}"
            )
        if adoption.license_expression.strip().upper() in _UNKNOWN_LICENSE_MARKERS:
            findings.append(f"license:unknown:{adoption.component_id}")

    for item in snapshot.dependencies:
        component_id = item.adoption.component_id
        if component_id not in component_map:
            continue
        for required_id in item.requires_component_ids:
            if required_id not in component_map:
                findings.append(f"transitive-dependency:missing:{component_id}:{required_id}")

    declared_notices = {
        (component_id, notice_ref)
        for component_id, item in component_map.items()
        for notice_ref in item.adoption.notice_refs
    }
    seen_notices: set[tuple[str, str]] = set()
    for notice in snapshot.notice_evidence:
        if notice.project_id != snapshot.project_id:
            findings.append(f"cross-project:notice-evidence:{notice.component_id}")
            continue
        dependency = component_map.get(notice.component_id)
        if dependency is None:
            findings.append(f"orphan:notice:{notice.component_id}:{notice.notice_ref}")
            continue
        key = (notice.component_id, notice.notice_ref)
        if key in seen_notices:
            findings.append(f"duplicate:notice:{notice.component_id}:{notice.notice_ref}")
            continue
        seen_notices.add(key)
        if key not in declared_notices:
            findings.append(f"orphan:notice:{notice.component_id}:{notice.notice_ref}")
        if (
            _canonical_package(notice.package_name) != dependency.canonical_package_name
            or notice.version != dependency.adoption.version
        ):
            findings.append(f"notice:identity-mismatch:{notice.component_id}")

    for component_id, notice_ref in sorted(declared_notices - seen_notices):
        findings.append(f"notice:evidence-missing:{component_id}:{notice_ref}")
    return tuple(dict.fromkeys(findings))


def _notice_bundle_findings(
    snapshot: ReleaseComplianceSnapshot,
    bundle_dir: Path,
) -> tuple[str, ...]:
    directory = Path(bundle_dir)
    findings = [f"packaging:{item}" for item in verify_third_party_notices(directory)]
    target = directory / "THIRD_PARTY_NOTICES.txt"
    if target.is_file():
        if not hmac.compare_digest(
            _file_sha256(target).casefold(), snapshot.notice_bundle_sha256.casefold()
        ):
            findings.append("packaging:notices-digest-mismatch")
    elif "packaging:missing:THIRD_PARTY_NOTICES.txt" not in findings:
        findings.append("packaging:missing:THIRD_PARTY_NOTICES.txt")
    if not findings:
        for notice in snapshot.notice_evidence:
            expected_ref = verified_third_party_notice_reference(
                directory,
                package_name=notice.package_name,
                version=notice.version,
            )
            if expected_ref is None or not hmac.compare_digest(
                expected_ref,
                notice.notice_ref,
            ):
                findings.append(f"packaging:notice-ref-unverified:{notice.component_id}")
    return tuple(dict.fromkeys(findings))


def _snapshot_payload(snapshot: ReleaseComplianceSnapshot) -> dict[str, object]:
    dependencies = []
    for item in sorted(snapshot.dependencies, key=lambda value: value.adoption.component_id):
        adoption = item.adoption
        dependencies.append(
            {
                "project_id": adoption.project_id,
                "component_id": adoption.component_id,
                "package_name": adoption.package_name,
                "version": adoption.version,
                "source_ref": adoption.source_ref,
                "source_sha256": item.source_sha256.casefold(),
                "provenance_ref": adoption.provenance_ref,
                "license_expression": adoption.license_expression,
                "license_disposition": adoption.license_disposition.value,
                "distribution_obligations": sorted(adoption.distribution_obligations),
                "notice_required": adoption.notice_required,
                "notice_refs": sorted(adoption.notice_refs),
                "review_ref": adoption.review_ref,
                "requires_component_ids": sorted(item.requires_component_ids),
            }
        )
    obligations = [
        {
            "project_id": item.project_id,
            "component_id": item.component_id,
            "obligation": item.obligation,
            "fulfillment_ref": item.fulfillment_ref,
        }
        for item in sorted(
            snapshot.obligation_evidence,
            key=lambda value: (value.component_id, value.obligation, value.fulfillment_ref),
        )
    ]
    notices = [
        {
            "project_id": item.project_id,
            "component_id": item.component_id,
            "notice_ref": item.notice_ref,
            "package_name": item.package_name,
            "version": item.version,
        }
        for item in sorted(
            snapshot.notice_evidence,
            key=lambda value: (value.component_id, value.notice_ref),
        )
    ]
    competitor = [
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
        for item in sorted(snapshot.competitor_evidence, key=lambda value: value.evidence_id)
    ]
    return {
        "schema": "nika-pf10-release-snapshot-v2",
        "project_id": snapshot.project_id,
        "release_id": snapshot.release_id,
        "project_source_ref": snapshot.project_source_ref,
        "project_source_sha256": snapshot.project_source_sha256.casefold(),
        "artifact_ref": snapshot.artifact_ref,
        "artifact_sha256": snapshot.artifact_sha256.casefold(),
        "notice_bundle_sha256": snapshot.notice_bundle_sha256.casefold(),
        "dependencies": dependencies,
        "dependency_closure_ref": snapshot.dependency_closure_ref,
        "obligation_evidence": obligations,
        "notice_evidence": notices,
        "competitor_evidence": competitor,
        "scope_review_ref": snapshot.scope_review_ref,
    }


def _issue_decision(
    *,
    snapshot: ReleaseComplianceSnapshot,
    allowed: bool,
    findings: tuple[str, ...],
    evidence_refs: tuple[str, ...],
) -> ReleaseComplianceDecision:
    proof = _decision_proof(
        project_id=snapshot.project_id,
        release_id=snapshot.release_id,
        artifact_ref=snapshot.artifact_ref,
        snapshot_digest=snapshot.digest,
        allowed=allowed,
        findings=findings,
        evidence_refs=evidence_refs,
    )
    return ReleaseComplianceDecision(
        project_id=snapshot.project_id,
        release_id=snapshot.release_id,
        artifact_ref=snapshot.artifact_ref,
        snapshot_digest=snapshot.digest,
        allowed=allowed,
        findings=findings,
        evidence_refs=evidence_refs,
        _authority_proof=proof,
    )


def _issue_grant(
    decision: ReleaseComplianceDecision,
    snapshot: ReleaseComplianceSnapshot,
) -> ReleaseComplianceGrant:
    evidence_refs = tuple(
        dict.fromkeys(
            (
                *decision.evidence_refs,
                f"pf10:snapshot:{snapshot.digest}",
                f"artifact:sha256:{snapshot.artifact_sha256.casefold()}",
            )
        )
    )
    proof = _grant_proof(
        project_id=snapshot.project_id,
        release_id=snapshot.release_id,
        artifact_ref=snapshot.artifact_ref,
        artifact_sha256=snapshot.artifact_sha256,
        snapshot_digest=snapshot.digest,
        evidence_refs=evidence_refs,
    )
    return ReleaseComplianceGrant(
        project_id=snapshot.project_id,
        release_id=snapshot.release_id,
        artifact_ref=snapshot.artifact_ref,
        artifact_sha256=snapshot.artifact_sha256,
        snapshot_digest=snapshot.digest,
        evidence_refs=evidence_refs,
        _authority_proof=proof,
    )


def _decision_proof(
    *,
    project_id: str,
    release_id: str,
    artifact_ref: str,
    snapshot_digest: str,
    allowed: bool,
    findings: tuple[str, ...],
    evidence_refs: tuple[str, ...],
) -> str:
    return _proof(
        {
            "schema": "nika-pf10-release-decision-authority-v1",
            "project_id": project_id,
            "release_id": release_id,
            "artifact_ref": artifact_ref,
            "snapshot_digest": snapshot_digest,
            "allowed": allowed,
            "findings": list(findings),
            "evidence_refs": list(evidence_refs),
        }
    )


def _valid_decision_proof(decision: ReleaseComplianceDecision) -> bool:
    proof = object.__getattribute__(decision, "_authority_proof")
    if not isinstance(proof, str) or not proof:
        return False
    expected = _decision_proof(
        project_id=object.__getattribute__(decision, "project_id"),
        release_id=object.__getattribute__(decision, "release_id"),
        artifact_ref=object.__getattribute__(decision, "artifact_ref"),
        snapshot_digest=object.__getattribute__(decision, "snapshot_digest"),
        allowed=object.__getattribute__(decision, "allowed"),
        findings=object.__getattribute__(decision, "findings"),
        evidence_refs=object.__getattribute__(decision, "evidence_refs"),
    )
    return hmac.compare_digest(proof, expected)


def _grant_proof(
    *,
    project_id: str,
    release_id: str,
    artifact_ref: str,
    artifact_sha256: str,
    snapshot_digest: str,
    evidence_refs: tuple[str, ...],
) -> str:
    return _proof(
        {
            "schema": "nika-pf10-release-grant-authority-v1",
            "project_id": project_id,
            "release_id": release_id,
            "artifact_ref": artifact_ref,
            "artifact_sha256": artifact_sha256.casefold(),
            "snapshot_digest": snapshot_digest,
            "evidence_refs": list(evidence_refs),
        }
    )


def _valid_grant_proof(grant: ReleaseComplianceGrant) -> bool:
    proof = object.__getattribute__(grant, "_authority_proof")
    if not isinstance(proof, str) or not proof:
        return False
    expected = _grant_proof(
        project_id=grant.project_id,
        release_id=grant.release_id,
        artifact_ref=grant.artifact_ref,
        artifact_sha256=grant.artifact_sha256,
        snapshot_digest=grant.snapshot_digest,
        evidence_refs=grant.evidence_refs,
    )
    return hmac.compare_digest(proof, expected)


def _proof(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_RELEASE_AUTHORITY_KEY, encoded, hashlib.sha256).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_package(value: str) -> str:
    return _PACKAGE_SEPARATORS_RE.sub("-", value).casefold()


def _text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ProductComplianceError(f"{label} must be non-empty text")


def _sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value.strip()) is None:
        raise ProductComplianceError(f"{label} must be an exact SHA-256 hex digest")


def _tuple(value: object, label: str) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{label} must be a tuple")


def _unique_text(
    values: tuple[str, ...],
    label: str,
    *,
    allow_empty: bool = False,
) -> None:
    _tuple(values, label)
    seen: set[str] = set()
    for value in values:
        _text(value, label)
        if value in seen:
            raise ProductComplianceError(f"duplicate {label}: {value}")
        seen.add(value)
    if not allow_empty and not values:
        raise ProductComplianceError(f"{label} must not be empty")
