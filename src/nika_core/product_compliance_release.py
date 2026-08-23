from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from nika_core.data.sqlite import SQLiteStore
from nika_core.packaging.notices import verified_third_party_notice_inventory
from nika_core.product_compliance import (
    CompetitorResearchEvidence,
    ComplianceReviewAuthorityPort,
    DependencyAdoption,
    DistributionObligationEvidence,
    LicenseDisposition,
    ProductComplianceDecision,
    ProductComplianceError,
    ProductComplianceGate,
    canonical_dependency_name,
    compliance_inventory_fingerprint,
)

_NOTICE_AUTHORITY_KEY = secrets.token_bytes(32)
_STATE_SCHEMA = "nika-pf10-compliance-state-v1"


@dataclass(frozen=True, slots=True)
class PackagingNoticeEntry:
    package_name: str
    version: str
    notice_ref: str


@dataclass(frozen=True, slots=True)
class VerifiedPackagingNoticeAuthority:
    project_id: str
    bundle_digest: str
    entries: tuple[PackagingNoticeEntry, ...]
    _proof: str | None = None

    @property
    def verified(self) -> bool:
        return _valid_notice_authority(self)

    def verify_notice(
        self,
        *,
        project_id: str,
        component_id: str,
        package_name: str,
        version: str,
        notice_ref: str,
    ) -> bool:
        if not component_id.strip() or not self.verified or project_id != self.project_id:
            return False
        wanted = (canonical_dependency_name(package_name), version, notice_ref)
        return any(
            (
                canonical_dependency_name(entry.package_name),
                entry.version,
                entry.notice_ref,
            )
            == wanted
            for entry in self.entries
        )


@dataclass(frozen=True, slots=True)
class ProductComplianceInventory:
    project_id: str
    dependencies: tuple[DependencyAdoption, ...] = ()
    obligation_evidence: tuple[DistributionObligationEvidence, ...] = ()
    competitor_evidence: tuple[CompetitorResearchEvidence, ...] = ()
    scope_review_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise ProductComplianceError("compliance inventory project_id must be non-empty text")
        for dependency in self.dependencies:
            if dependency.project_id != self.project_id:
                raise ProductComplianceError(
                    "compliance inventory contains cross-project dependency"
                )
        for evidence in self.obligation_evidence:
            if evidence.project_id != self.project_id:
                raise ProductComplianceError(
                    "compliance inventory contains cross-project obligation evidence"
                )
        for evidence in self.competitor_evidence:
            if evidence.project_id != self.project_id:
                raise ProductComplianceError(
                    "compliance inventory contains cross-project competitor evidence"
                )

    @property
    def fingerprint(self) -> str:
        return compliance_inventory_fingerprint(
            project_id=self.project_id,
            dependencies=self.dependencies,
            obligation_evidence=self.obligation_evidence,
            competitor_evidence=self.competitor_evidence,
            scope_review_ref=self.scope_review_ref,
        )


@dataclass(frozen=True, slots=True)
class ProductComplianceState:
    project_id: str
    revision: int
    inventory: ProductComplianceInventory
    inventory_fingerprint: str
    assessment_fingerprint: str | None


class ProductComplianceRepository:
    """Durable PF10 inventory authority using the canonical SQLiteStore boundary."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store
        self._ensure_schema()

    def put_inventory(self, inventory: ProductComplianceInventory) -> ProductComplianceState:
        payload = _encode_inventory(inventory)
        fingerprint = inventory.fingerprint
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT revision, inventory_json, inventory_fingerprint, assessment_fingerprint "
                "FROM product_compliance_state WHERE project_id = ?",
                (inventory.project_id,),
            ).fetchone()
            if row is None:
                revision = 1
                conn.execute(
                    "INSERT INTO product_compliance_state("
                    "project_id, revision, inventory_json, inventory_fingerprint, "
                    "assessment_fingerprint) VALUES (?, ?, ?, ?, NULL)",
                    (inventory.project_id, revision, payload, fingerprint),
                )
                assessment = None
            elif row["inventory_fingerprint"] == fingerprint:
                revision = int(row["revision"])
                assessment = row["assessment_fingerprint"]
            else:
                revision = int(row["revision"]) + 1
                conn.execute(
                    "UPDATE product_compliance_state SET revision = ?, inventory_json = ?, "
                    "inventory_fingerprint = ?, assessment_fingerprint = NULL "
                    "WHERE project_id = ?",
                    (revision, payload, fingerprint, inventory.project_id),
                )
                assessment = None
        return ProductComplianceState(
            project_id=inventory.project_id,
            revision=revision,
            inventory=inventory,
            inventory_fingerprint=fingerprint,
            assessment_fingerprint=assessment,
        )

    def get(self, project_id: str) -> ProductComplianceState:
        if not isinstance(project_id, str) or not project_id.strip():
            raise ProductComplianceError("project_id must be non-empty text")
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT revision, inventory_json, inventory_fingerprint, assessment_fingerprint "
                "FROM product_compliance_state WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        inventory = _decode_inventory(row["inventory_json"])
        fingerprint = inventory.fingerprint
        if inventory.project_id != project_id or fingerprint != row["inventory_fingerprint"]:
            raise ProductComplianceError("durable compliance inventory failed integrity validation")
        revision = row["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ProductComplianceError("durable compliance revision is invalid")
        assessment = row["assessment_fingerprint"]
        if assessment is not None and (
            not isinstance(assessment, str) or not assessment.startswith("sha256:")
        ):
            raise ProductComplianceError("durable compliance assessment fingerprint is invalid")
        return ProductComplianceState(
            project_id=project_id,
            revision=revision,
            inventory=inventory,
            inventory_fingerprint=fingerprint,
            assessment_fingerprint=assessment,
        )

    def bind_assessment(
        self,
        *,
        project_id: str,
        revision: int,
        inventory_fingerprint: str,
        assessment_fingerprint: str,
    ) -> ProductComplianceState:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE product_compliance_state SET assessment_fingerprint = ? "
                "WHERE project_id = ? AND revision = ? AND inventory_fingerprint = ?",
                (
                    assessment_fingerprint,
                    project_id,
                    revision,
                    inventory_fingerprint,
                ),
            )
            if cursor.rowcount != 1:
                raise ProductComplianceError(
                    "compliance inventory changed while assessment was being bound"
                )
        return self.get(project_id)

    def _ensure_schema(self) -> None:
        with self._store.connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS product_compliance_state ("
                "project_id TEXT PRIMARY KEY, "
                "revision INTEGER NOT NULL CHECK(revision >= 1), "
                "inventory_json TEXT NOT NULL, "
                "inventory_fingerprint TEXT NOT NULL, "
                "assessment_fingerprint TEXT NULL)"
            )


class ProductComplianceReleaseService:
    """Exact-inventory PF10 decision and release/delivery gate."""

    def __init__(
        self,
        repository: ProductComplianceRepository,
        *,
        review_authority: ComplianceReviewAuthorityPort | None = None,
    ) -> None:
        self._repository = repository
        self._review_authority = review_authority

    def record_inventory(self, inventory: ProductComplianceInventory) -> ProductComplianceState:
        return self._repository.put_inventory(inventory)

    def evaluate_current(
        self,
        *,
        project_id: str,
        packaging_notices: VerifiedPackagingNoticeAuthority | None = None,
    ) -> ProductComplianceDecision:
        state = self._repository.get(project_id)
        extra_findings: list[str] = []
        extra_evidence: list[str] = []
        notice_authority = None
        if packaging_notices is not None:
            if not packaging_notices.verified or packaging_notices.project_id != project_id:
                extra_findings.append("packaging-notices:untrusted-or-cross-project")
            else:
                notice_authority = packaging_notices
                extra_evidence.append(packaging_notices.bundle_digest)
                extra_findings.extend(
                    _packaging_inventory_findings(state.inventory, packaging_notices)
                )

        gate = ProductComplianceGate(
            review_authority=self._review_authority,
            notice_authority=notice_authority,
        )
        decision = gate.evaluate(
            project_id=project_id,
            dependencies=state.inventory.dependencies,
            obligation_evidence=state.inventory.obligation_evidence,
            competitor_evidence=state.inventory.competitor_evidence,
            scope_review_ref=state.inventory.scope_review_ref,
            extra_findings=tuple(dict.fromkeys(extra_findings)),
            extra_evidence_refs=tuple(dict.fromkeys(extra_evidence)),
        )
        self._repository.bind_assessment(
            project_id=project_id,
            revision=state.revision,
            inventory_fingerprint=state.inventory_fingerprint,
            assessment_fingerprint=decision.inventory_fingerprint,
        )
        return decision

    def require_delivery_allowed(
        self,
        *,
        project_id: str,
        decision: ProductComplianceDecision,
    ) -> None:
        state = self._repository.get(project_id)
        if decision.project_id != project_id:
            raise ProductComplianceError(
                "release blocked by PF10 compliance gate: decision:cross-project"
            )
        if state.assessment_fingerprint is None:
            raise ProductComplianceError(
                "release blocked by PF10 compliance gate: decision:no-current-assessment"
            )
        ProductComplianceGate().require_release_allowed(
            decision,
            expected_inventory_fingerprint=state.assessment_fingerprint,
        )

    def verify_release(
        self,
        *,
        project_id: str,
        decision: ProductComplianceDecision,
    ) -> bool:
        try:
            self.require_delivery_allowed(project_id=project_id, decision=decision)
        except (KeyError, ProductComplianceError):
            return False
        return True


def verified_packaging_notice_authority(
    *,
    project_id: str,
    bundle_dir: Path,
) -> VerifiedPackagingNoticeAuthority:
    if not isinstance(project_id, str) or not project_id.strip():
        raise ProductComplianceError("project_id must be non-empty text")
    target = bundle_dir / "THIRD_PARTY_NOTICES.txt"
    inventory = verified_third_party_notice_inventory(bundle_dir)
    digest = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
    entries = tuple(
        PackagingNoticeEntry(
            package_name=package_name,
            version=version,
            notice_ref=_notice_ref(digest, title),
        )
        for package_name, version, title in inventory
    )
    proof = _notice_authority_proof(project_id, digest, entries)
    return VerifiedPackagingNoticeAuthority(
        project_id=project_id,
        bundle_digest=digest,
        entries=entries,
        _proof=proof,
    )


def _packaging_inventory_findings(
    inventory: ProductComplianceInventory,
    notices: VerifiedPackagingNoticeAuthority,
) -> tuple[str, ...]:
    declared: dict[str, DependencyAdoption] = {}
    findings: list[str] = []
    for dependency in inventory.dependencies:
        name = canonical_dependency_name(dependency.package_name)
        if name in declared:
            continue
        declared[name] = dependency

    packaged: dict[str, PackagingNoticeEntry] = {}
    for entry in notices.entries:
        name = canonical_dependency_name(entry.package_name)
        if name in packaged:
            findings.append(f"packaging:duplicate-distribution:{name}")
            continue
        packaged[name] = entry
        dependency = declared.get(name)
        if dependency is None:
            findings.append(f"transitive-dependency:unrecorded:{name}:{entry.version}")
            continue
        if dependency.version != entry.version:
            findings.append(f"packaging:version-mismatch:{dependency.component_id}")
        if dependency.notice_required and entry.notice_ref not in dependency.notice_refs:
            findings.append(f"notice:packaging-ref-mismatch:{dependency.component_id}")

    for name, dependency in declared.items():
        if name not in packaged:
            findings.append(f"packaging:dependency-not-shipped:{dependency.component_id}")
    return tuple(dict.fromkeys(findings))


def _notice_ref(bundle_digest: str, title: str) -> str:
    return f"artifact:THIRD_PARTY_NOTICES.txt@{bundle_digest}#{quote(title, safe='')}"


def _notice_authority_proof(
    project_id: str,
    bundle_digest: str,
    entries: tuple[PackagingNoticeEntry, ...],
) -> str:
    payload = json.dumps(
        {
            "schema": "nika-pf10-packaging-notices-v1",
            "project_id": project_id,
            "bundle_digest": bundle_digest,
            "entries": [
                [entry.package_name, entry.version, entry.notice_ref] for entry in entries
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_NOTICE_AUTHORITY_KEY, payload, hashlib.sha256).hexdigest()


def _valid_notice_authority(authority: VerifiedPackagingNoticeAuthority) -> bool:
    proof = authority._proof
    if not isinstance(proof, str) or not proof:
        return False
    expected = _notice_authority_proof(
        authority.project_id,
        authority.bundle_digest,
        authority.entries,
    )
    return hmac.compare_digest(proof, expected)


def _encode_inventory(inventory: ProductComplianceInventory) -> str:
    payload = {
        "schema": _STATE_SCHEMA,
        "project_id": inventory.project_id,
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
                "distribution_obligations": list(item.distribution_obligations),
                "notice_required": item.notice_required,
                "notice_refs": list(item.notice_refs),
                "review_ref": item.review_ref,
            }
            for item in inventory.dependencies
        ],
        "obligation_evidence": [
            {
                "project_id": item.project_id,
                "component_id": item.component_id,
                "obligation": item.obligation,
                "fulfillment_ref": item.fulfillment_ref,
            }
            for item in inventory.obligation_evidence
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
            for item in inventory.competitor_evidence
        ],
        "scope_review_ref": inventory.scope_review_ref,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_inventory(raw: object) -> ProductComplianceInventory:
    if not isinstance(raw, str):
        raise ProductComplianceError("durable compliance inventory JSON is invalid")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProductComplianceError("durable compliance inventory JSON is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema") != _STATE_SCHEMA:
        raise ProductComplianceError("durable compliance inventory schema is invalid")
    try:
        dependencies = tuple(
            DependencyAdoption(
                project_id=item["project_id"],
                component_id=item["component_id"],
                package_name=item["package_name"],
                version=item["version"],
                source_ref=item["source_ref"],
                provenance_ref=item["provenance_ref"],
                license_expression=item["license_expression"],
                license_disposition=LicenseDisposition(item["license_disposition"]),
                distribution_obligations=tuple(item["distribution_obligations"]),
                notice_required=item["notice_required"],
                notice_refs=tuple(item["notice_refs"]),
                review_ref=item["review_ref"],
            )
            for item in payload["dependencies"]
        )
        obligations = tuple(
            DistributionObligationEvidence(
                project_id=item["project_id"],
                component_id=item["component_id"],
                obligation=item["obligation"],
                fulfillment_ref=item["fulfillment_ref"],
            )
            for item in payload["obligation_evidence"]
        )
        competitors = tuple(
            CompetitorResearchEvidence(
                project_id=item["project_id"],
                evidence_id=item["evidence_id"],
                source_ref=item["source_ref"],
                provenance_ref=item["provenance_ref"],
                permitted_public_evidence=item["permitted_public_evidence"],
                proprietary_material=item["proprietary_material"],
                permission_basis_ref=item["permission_basis_ref"],
                legal_basis_ref=item["legal_basis_ref"],
                reuse_authorization_ref=item["reuse_authorization_ref"],
            )
            for item in payload["competitor_evidence"]
        )
        project_id = payload["project_id"]
        scope_review_ref = payload["scope_review_ref"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ProductComplianceError("durable compliance inventory shape is invalid") from exc
    return ProductComplianceInventory(
        project_id=project_id,
        dependencies=dependencies,
        obligation_evidence=obligations,
        competitor_evidence=competitors,
        scope_review_ref=scope_review_ref,
    )
