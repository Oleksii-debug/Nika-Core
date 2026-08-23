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
_NON_EXACT_VERSION_RE = re.compile(r"[<>=!~*^]|\s")
_UNKNOWN_LICENSES = frozenset({"unknown", "noassertion", "none", "unspecified"})


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


class ComplianceStateAuthorityPort(Protocol):
    """Trusted current-state source used at the release/delivery boundary."""

    def current_snapshot(self, *, project_id: str) -> ProductComplianceSnapshot | None: ...


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
    artifact_sha256: str | None = None
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
        if self.artifact_sha256 is not None:
            digest = self.artifact_sha256.casefold()
            if not _SHA256_RE.fullmatch(digest):
                raise ProductComplianceError(
                    "dependency artifact_sha256 must be an exact 64-character SHA-256"
                )
        _require_unique_text(self.distribution_obligations, "distribution obligation")
        _require_unique_text(self.notice_refs, "dependency notice_ref")
        if self.review_ref is not None:
            _require_text(self.review_ref, "dependency review_ref")
        if self.license_disposition is LicenseDisposition.APPROVED and self.review_ref is None:
            raise ProductComplianceError(
                "approved dependency license requires authorized review_ref evidence"
            )


@dataclass(frozen=True, slots=True)
class ResolvedDependency:
    component_id: str
    package_name: str
    version: str
    parent_component_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.component_id, "resolved dependency component_id")
        _require_text(self.package_name, "resolved dependency package_name")
        _require_text(self.version, "resolved dependency version")
        _require_unique_text(
            self.parent_component_ids,
            "resolved dependency parent_component_id",
            allow_empty=True,
        )


@dataclass(frozen=True, slots=True)
class ResolvedDependencyInventory:
    project_id: str
    inventory_ref: str
    dependencies: tuple[ResolvedDependency, ...]

    def __post_init__(self) -> None:
        _require_text(self.project_id, "resolved inventory project_id")
        _require_text(self.inventory_ref, "resolved inventory ref")
        _require_tuple(self.dependencies, "resolved inventory dependencies")


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
class NoticeEvidence:
    project_id: str
    component_id: str
    notice_ref: str
    artifact_ref: str

    def __post_init__(self) -> None:
        _require_text(self.project_id, "notice project_id")
        _require_text(self.component_id, "notice component_id")
        _require_text(self.notice_ref, "notice_ref")
        _require_text(self.artifact_ref, "notice artifact_ref")


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
class ProductComplianceSnapshot:
    project_id: str
    revision: int
    dependencies: tuple[DependencyAdoption, ...] = ()
    resolved_inventory: ResolvedDependencyInventory | None = None
    obligation_evidence: tuple[DistributionObligationEvidence, ...] = ()
    notice_evidence: tuple[NoticeEvidence, ...] = ()
    competitor_evidence: tuple[CompetitorResearchEvidence, ...] = ()
    scope_review_ref: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.project_id, "compliance snapshot project_id")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ProductComplianceError("compliance snapshot revision must be a non-negative int")
        _require_tuple(self.dependencies, "dependencies")
        _require_tuple(self.obligation_evidence, "obligation_evidence")
        _require_tuple(self.notice_evidence, "notice_evidence")
        _require_tuple(self.competitor_evidence, "competitor_evidence")
        if self.resolved_inventory is not None and not isinstance(
            self.resolved_inventory, ResolvedDependencyInventory
        ):
            raise ProductComplianceError("resolved_inventory is invalid")
        if self.scope_review_ref is not None:
            _require_text(self.scope_review_ref, "scope_review_ref")

    @property
    def digest(self) -> str:
        return compliance_snapshot_digest(self)


@dataclass(frozen=True, slots=True)
class ProductComplianceDecision:
    """PF10 result bound to an exact compliance-state revision and digest."""

    project_id: str
    allowed: bool
    findings: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    snapshot_revision: int = 0
    snapshot_digest: str = "0" * 64
    _authority_proof: str | None = field(default=None, repr=False, compare=False)
    _release_gate: ProductComplianceGate | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_text(self.project_id, "compliance decision project_id")
        if not isinstance(object.__getattribute__(self, "allowed"), bool):
            raise ProductComplianceError("compliance decision allowed must be a boolean")
        _require_unique_text(self.findings, "compliance finding", allow_empty=True)
        _require_unique_text(self.evidence_refs, "compliance evidence_ref", allow_empty=True)
        if (
            isinstance(self.snapshot_revision, bool)
            or not isinstance(self.snapshot_revision, int)
            or self.snapshot_revision < 0
        ):
            raise ProductComplianceError("compliance decision snapshot_revision is invalid")
        if not _SHA256_RE.fullmatch(self.snapshot_digest.casefold()):
            raise ProductComplianceError("compliance decision snapshot_digest is invalid")
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
            if not raw_allowed or not _valid_decision_proof(self):
                return False
            gate = object.__getattribute__(self, "_release_gate")
            if gate is None:
                return False
            return gate._decision_still_current(self)
        return object.__getattribute__(self, name)


class ProductComplianceGate:
    """PF10 policy plus an exact-current-state release/delivery authority."""

    def __init__(
        self,
        *,
        review_authority: ComplianceReviewAuthorityPort | None = None,
        state_authority: ComplianceStateAuthorityPort | None = None,
    ) -> None:
        self._review_authority = review_authority
        self._state_authority = state_authority

    def evaluate(
        self,
        *,
        project_id: str,
        dependencies: tuple[DependencyAdoption, ...] = (),
        resolved_inventory: ResolvedDependencyInventory | None = None,
        obligation_evidence: tuple[DistributionObligationEvidence, ...] = (),
        notice_evidence: tuple[NoticeEvidence, ...] = (),
        competitor_evidence: tuple[CompetitorResearchEvidence, ...] = (),
        scope_review_ref: str | None = None,
        revision: int = 0,
    ) -> ProductComplianceDecision:
        snapshot = ProductComplianceSnapshot(
            project_id=project_id,
            revision=revision,
            dependencies=dependencies,
            resolved_inventory=resolved_inventory,
            obligation_evidence=obligation_evidence,
            notice_evidence=notice_evidence,
            competitor_evidence=competitor_evidence,
            scope_review_ref=scope_review_ref,
        )
        return self.evaluate_snapshot(snapshot)

    def evaluate_snapshot(
        self,
        snapshot: ProductComplianceSnapshot,
    ) -> ProductComplianceDecision:
        if not isinstance(snapshot, ProductComplianceSnapshot):
            raise ProductComplianceError("evaluate_snapshot requires ProductComplianceSnapshot")
        project_id = snapshot.project_id
        findings: list[str] = []
        evidence_refs: list[str] = []
        trusted_scope_refs: set[str] = set()
        component_ids: set[str] = set()
        dependency_identities: set[tuple[str, str, str]] = set()

        scope_review_ref = snapshot.scope_review_ref
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

        inventory = snapshot.resolved_inventory
        resolved_by_component: dict[str, ResolvedDependency] = {}
        if snapshot.dependencies and inventory is None:
            findings.append("dependency-inventory:missing")
        elif inventory is not None:
            evidence_refs.append(inventory.inventory_ref)
            if inventory.project_id != project_id:
                findings.append("cross-project:dependency-inventory")
            else:
                for resolved in inventory.dependencies:
                    if resolved.component_id in resolved_by_component:
                        findings.append(
                            f"duplicate:resolved-dependency-component:{resolved.component_id}"
                        )
                        continue
                    resolved_by_component[resolved.component_id] = resolved

        obligations: dict[tuple[str, str], DistributionObligationEvidence] = {}
        for item in snapshot.obligation_evidence:
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

        notices: dict[tuple[str, str], NoticeEvidence] = {}
        for item in snapshot.notice_evidence:
            evidence_refs.extend((item.notice_ref, item.artifact_ref))
            if item.project_id != project_id:
                findings.append("cross-project:notice-evidence")
                continue
            key = (item.component_id, item.notice_ref)
            if key in notices:
                findings.append(f"duplicate:notice:{item.component_id}:{item.notice_ref}")
                continue
            notices[key] = item

        for dependency in snapshot.dependencies:
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

            if _NON_EXACT_VERSION_RE.search(dependency.version):
                findings.append(f"version:not-exact:{dependency.component_id}")
            if dependency.artifact_sha256 is None:
                findings.append(f"source:mutable-or-unverified:{dependency.component_id}")

            identity = (
                _normalize_package_name(dependency.package_name),
                dependency.version.casefold(),
                (dependency.artifact_sha256 or "").casefold(),
            )
            if identity in dependency_identities:
                findings.append(f"duplicate:dependency-identity:{dependency.component_id}")
            else:
                dependency_identities.add(identity)

            resolved = resolved_by_component.get(dependency.component_id)
            if inventory is not None and inventory.project_id == project_id:
                if resolved is None:
                    findings.append(f"orphan:dependency-adoption:{dependency.component_id}")
                elif (
                    _normalize_package_name(resolved.package_name)
                    != _normalize_package_name(dependency.package_name)
                    or resolved.version != dependency.version
                ):
                    findings.append(f"dependency-inventory:mismatch:{dependency.component_id}")

            if dependency.license_expression.strip().casefold() in _UNKNOWN_LICENSES:
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
            if dependency.notice_required and not dependency.notice_refs:
                findings.append(f"notice:missing:{dependency.component_id}")
            for notice_ref in dependency.notice_refs:
                if (dependency.component_id, notice_ref) not in notices:
                    findings.append(f"notice:unproven:{dependency.component_id}:{notice_ref}")

        if inventory is not None and inventory.project_id == project_id:
            for component_id, resolved in resolved_by_component.items():
                if component_id not in component_ids:
                    findings.append(f"dependency-adoption:missing:{component_id}")
                for parent_id in resolved.parent_component_ids:
                    if parent_id not in resolved_by_component:
                        findings.append(
                            f"dependency-inventory:missing-parent:{component_id}:{parent_id}"
                        )

        for component_id, obligation in obligations:
            if component_id not in component_ids:
                findings.append(f"orphan:distribution-obligation:{component_id}:{obligation}")

        declared_notice_keys = {
            (dependency.component_id, notice_ref)
            for dependency in snapshot.dependencies
            if dependency.project_id == project_id
            for notice_ref in dependency.notice_refs
        }
        for component_id, notice_ref in notices:
            if component_id not in component_ids or (component_id, notice_ref) not in declared_notice_keys:
                findings.append(f"orphan:notice:{component_id}:{notice_ref}")

        evidence_ids: set[str] = set()
        for evidence in snapshot.competitor_evidence:
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

        snapshot_state_finding = self._current_snapshot_finding(snapshot)
        if snapshot_state_finding is not None:
            findings.append(snapshot_state_finding)

        normalized_findings = tuple(dict.fromkeys(findings))
        normalized_evidence = tuple(dict.fromkeys(evidence_refs))
        return _issue_decision(
            project_id=project_id,
            allowed=not normalized_findings,
            findings=normalized_findings,
            evidence_refs=normalized_evidence,
            snapshot_revision=snapshot.revision,
            snapshot_digest=snapshot.digest,
            release_gate=self,
        )

    def require_release_allowed(self, decision: ProductComplianceDecision) -> None:
        if not isinstance(decision, ProductComplianceDecision):
            raise ProductComplianceError("release requires ProductComplianceDecision")
        if not decision.allowed:
            findings = decision.findings
            raw_allowed = object.__getattribute__(decision, "allowed")
            if raw_allowed and not _valid_decision_proof(decision):
                findings = (*findings, "decision:untrusted-origin")
            elif raw_allowed:
                findings = (*findings, "decision:stale-or-revoked")
            joined = ", ".join(dict.fromkeys(findings))
            raise ProductComplianceError(f"release blocked by PF10 compliance gate: {joined}")

    def _decision_still_current(self, decision: ProductComplianceDecision) -> bool:
        gate = object.__getattribute__(decision, "_release_gate")
        if gate is not self:
            return False
        authority = self._state_authority
        if authority is None:
            return False
        try:
            current = authority.current_snapshot(project_id=decision.project_id)
        except (LookupError, PermissionError, RuntimeError, TypeError, ValueError):
            return False
        if current is None:
            return False
        if (
            current.project_id != decision.project_id
            or current.revision != decision.snapshot_revision
            or current.digest != decision.snapshot_digest
        ):
            return False
        current_decision = self.evaluate_snapshot(current)
        return object.__getattribute__(current_decision, "allowed") is True

    def _current_snapshot_finding(self, snapshot: ProductComplianceSnapshot) -> str | None:
        authority = self._state_authority
        if authority is None:
            return "current-state-authority:missing"
        try:
            current = authority.current_snapshot(project_id=snapshot.project_id)
        except (LookupError, PermissionError, RuntimeError, TypeError, ValueError):
            return "current-state-authority:failed"
        if current is None:
            return "current-compliance-state:missing"
        if current.project_id != snapshot.project_id:
            return "current-compliance-state:project-mismatch"
        if current.revision != snapshot.revision or current.digest != snapshot.digest:
            return "current-compliance-state:stale"
        return None

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


def compliance_snapshot_digest(snapshot: ProductComplianceSnapshot) -> str:
    payload = _snapshot_payload(snapshot)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def dump_compliance_snapshot(snapshot: ProductComplianceSnapshot) -> str:
    if not isinstance(snapshot, ProductComplianceSnapshot):
        raise ProductComplianceError("dump requires ProductComplianceSnapshot")
    payload = {
        "schema": "nika-pf10-compliance-snapshot-v1",
        "snapshot": _snapshot_payload(snapshot),
        "digest": snapshot.digest,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_compliance_snapshot(payload: str) -> ProductComplianceSnapshot:
    _require_text(payload, "compliance snapshot payload")
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProductComplianceError("invalid compliance snapshot JSON") from exc
    if not isinstance(raw, dict) or raw.get("schema") != "nika-pf10-compliance-snapshot-v1":
        raise ProductComplianceError("unsupported compliance snapshot schema")
    snapshot_raw = raw.get("snapshot")
    if not isinstance(snapshot_raw, dict):
        raise ProductComplianceError("compliance snapshot body is invalid")
    snapshot = _snapshot_from_payload(snapshot_raw)
    if raw.get("digest") != snapshot.digest:
        raise ProductComplianceError("compliance snapshot digest mismatch")
    return snapshot


def _issue_decision(
    *,
    project_id: str,
    allowed: bool,
    findings: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    snapshot_revision: int,
    snapshot_digest: str,
    release_gate: ProductComplianceGate,
) -> ProductComplianceDecision:
    proof = _decision_proof(
        project_id=project_id,
        allowed=allowed,
        findings=findings,
        evidence_refs=evidence_refs,
        snapshot_revision=snapshot_revision,
        snapshot_digest=snapshot_digest,
        release_gate_identity=id(release_gate),
    )
    return ProductComplianceDecision(
        project_id=project_id,
        allowed=allowed,
        findings=findings,
        evidence_refs=evidence_refs,
        snapshot_revision=snapshot_revision,
        snapshot_digest=snapshot_digest,
        _authority_proof=proof,
        _release_gate=release_gate,
    )


def _decision_proof(
    *,
    project_id: str,
    allowed: bool,
    findings: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    snapshot_revision: int,
    snapshot_digest: str,
    release_gate_identity: int,
) -> str:
    payload = json.dumps(
        {
            "schema": "nika-pf10-decision-authority-v2",
            "project_id": project_id,
            "allowed": allowed,
            "findings": list(findings),
            "evidence_refs": list(evidence_refs),
            "snapshot_revision": snapshot_revision,
            "snapshot_digest": snapshot_digest,
            "release_gate_identity": release_gate_identity,
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
        snapshot_revision=object.__getattribute__(decision, "snapshot_revision"),
        snapshot_digest=object.__getattribute__(decision, "snapshot_digest"),
        release_gate_identity=id(object.__getattribute__(decision, "_release_gate")),
    )
    return hmac.compare_digest(proof, expected)


def _snapshot_payload(snapshot: ProductComplianceSnapshot) -> dict[str, object]:
    inventory = snapshot.resolved_inventory
    return {
        "project_id": snapshot.project_id,
        "revision": snapshot.revision,
        "dependencies": [
            {
                "project_id": item.project_id,
                "component_id": item.component_id,
                "package_name": item.package_name,
                "version": item.version,
                "source_ref": item.source_ref,
                "provenance_ref": item.provenance_ref,
                "license_expression": item.license_expression,
                "license_disposition": item.license_disposition.value,
                "artifact_sha256": item.artifact_sha256,
                "distribution_obligations": list(item.distribution_obligations),
                "notice_required": item.notice_required,
                "notice_refs": list(item.notice_refs),
                "review_ref": item.review_ref,
            }
            for item in sorted(snapshot.dependencies, key=lambda value: value.component_id)
        ],
        "resolved_inventory": (
            None
            if inventory is None
            else {
                "project_id": inventory.project_id,
                "inventory_ref": inventory.inventory_ref,
                "dependencies": [
                    {
                        "component_id": item.component_id,
                        "package_name": item.package_name,
                        "version": item.version,
                        "parent_component_ids": list(item.parent_component_ids),
                    }
                    for item in sorted(inventory.dependencies, key=lambda value: value.component_id)
                ],
            }
        ),
        "obligation_evidence": [
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
        ],
        "notice_evidence": [
            {
                "project_id": item.project_id,
                "component_id": item.component_id,
                "notice_ref": item.notice_ref,
                "artifact_ref": item.artifact_ref,
            }
            for item in sorted(
                snapshot.notice_evidence,
                key=lambda value: (value.component_id, value.notice_ref, value.artifact_ref),
            )
        ],
        "competitor_evidence": [
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
        ],
        "scope_review_ref": snapshot.scope_review_ref,
    }


def _snapshot_from_payload(raw: dict[str, object]) -> ProductComplianceSnapshot:
    project_id = _mapping_text(raw, "project_id", "snapshot")
    revision = raw.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise ProductComplianceError("snapshot revision is invalid")

    dependencies_raw = _mapping_list(raw, "dependencies", "snapshot")
    dependencies = tuple(
        DependencyAdoption(
            project_id=_mapping_text(item, "project_id", "dependency"),
            component_id=_mapping_text(item, "component_id", "dependency"),
            package_name=_mapping_text(item, "package_name", "dependency"),
            version=_mapping_text(item, "version", "dependency"),
            source_ref=_mapping_text(item, "source_ref", "dependency"),
            provenance_ref=_mapping_text(item, "provenance_ref", "dependency"),
            license_expression=_mapping_text(item, "license_expression", "dependency"),
            license_disposition=LicenseDisposition(
                _mapping_text(item, "license_disposition", "dependency")
            ),
            artifact_sha256=_mapping_optional_text(item, "artifact_sha256"),
            distribution_obligations=_mapping_text_tuple(
                item, "distribution_obligations", "dependency"
            ),
            notice_required=_mapping_bool(item, "notice_required", "dependency"),
            notice_refs=_mapping_text_tuple(item, "notice_refs", "dependency"),
            review_ref=_mapping_optional_text(item, "review_ref"),
        )
        for item in dependencies_raw
    )

    inventory_raw = raw.get("resolved_inventory")
    inventory: ResolvedDependencyInventory | None
    if inventory_raw is None:
        inventory = None
    elif isinstance(inventory_raw, dict):
        inventory = ResolvedDependencyInventory(
            project_id=_mapping_text(inventory_raw, "project_id", "resolved inventory"),
            inventory_ref=_mapping_text(inventory_raw, "inventory_ref", "resolved inventory"),
            dependencies=tuple(
                ResolvedDependency(
                    component_id=_mapping_text(item, "component_id", "resolved dependency"),
                    package_name=_mapping_text(item, "package_name", "resolved dependency"),
                    version=_mapping_text(item, "version", "resolved dependency"),
                    parent_component_ids=_mapping_text_tuple(
                        item, "parent_component_ids", "resolved dependency"
                    ),
                )
                for item in _mapping_list(inventory_raw, "dependencies", "resolved inventory")
            ),
        )
    else:
        raise ProductComplianceError("resolved inventory payload is invalid")

    obligations = tuple(
        DistributionObligationEvidence(
            project_id=_mapping_text(item, "project_id", "obligation"),
            component_id=_mapping_text(item, "component_id", "obligation"),
            obligation=_mapping_text(item, "obligation", "obligation"),
            fulfillment_ref=_mapping_text(item, "fulfillment_ref", "obligation"),
        )
        for item in _mapping_list(raw, "obligation_evidence", "snapshot")
    )
    notices = tuple(
        NoticeEvidence(
            project_id=_mapping_text(item, "project_id", "notice"),
            component_id=_mapping_text(item, "component_id", "notice"),
            notice_ref=_mapping_text(item, "notice_ref", "notice"),
            artifact_ref=_mapping_text(item, "artifact_ref", "notice"),
        )
        for item in _mapping_list(raw, "notice_evidence", "snapshot")
    )
    competitor = tuple(
        CompetitorResearchEvidence(
            project_id=_mapping_text(item, "project_id", "competitor evidence"),
            evidence_id=_mapping_text(item, "evidence_id", "competitor evidence"),
            source_ref=_mapping_text(item, "source_ref", "competitor evidence"),
            provenance_ref=_mapping_text(item, "provenance_ref", "competitor evidence"),
            permitted_public_evidence=_mapping_bool(
                item, "permitted_public_evidence", "competitor evidence"
            ),
            proprietary_material=_mapping_bool(item, "proprietary_material", "competitor evidence"),
            permission_basis_ref=_mapping_optional_text(item, "permission_basis_ref"),
            legal_basis_ref=_mapping_optional_text(item, "legal_basis_ref"),
            reuse_authorization_ref=_mapping_optional_text(item, "reuse_authorization_ref"),
        )
        for item in _mapping_list(raw, "competitor_evidence", "snapshot")
    )
    scope_review_ref = raw.get("scope_review_ref")
    if scope_review_ref is not None and not isinstance(scope_review_ref, str):
        raise ProductComplianceError("scope_review_ref payload is invalid")
    return ProductComplianceSnapshot(
        project_id=project_id,
        revision=revision,
        dependencies=dependencies,
        resolved_inventory=inventory,
        obligation_evidence=obligations,
        notice_evidence=notices,
        competitor_evidence=competitor,
        scope_review_ref=scope_review_ref,
    )


def _normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


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


def _mapping_text(mapping: dict[str, object], field: str, label: str) -> str:
    value = mapping.get(field)
    _require_text(value, f"{label} {field}")
    return str(value)


def _mapping_optional_text(mapping: dict[str, object], field: str) -> str | None:
    value = mapping.get(field)
    if value is None:
        return None
    _require_text(value, field)
    return str(value)


def _mapping_bool(mapping: dict[str, object], field: str, label: str) -> bool:
    value = mapping.get(field)
    if not isinstance(value, bool):
        raise ProductComplianceError(f"{label} {field} must be a boolean")
    return value


def _mapping_list(
    mapping: dict[str, object],
    field: str,
    label: str,
) -> list[dict[str, object]]:
    value = mapping.get(field)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ProductComplianceError(f"{label} {field} must be a list of objects")
    return value


def _mapping_text_tuple(
    mapping: dict[str, object],
    field: str,
    label: str,
) -> tuple[str, ...]:
    value = mapping.get(field)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ProductComplianceError(f"{label} {field} must be a list of strings")
    return tuple(value)
