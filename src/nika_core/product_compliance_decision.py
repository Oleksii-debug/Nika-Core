from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from threading import RLock
from typing import Iterable

from .product_compliance_models import (
    CompetitorResearchEvidence,
    DependencyAdoption,
    DistributionObligationEvidence,
    PackagedDependencyEvidence,
    PackagingNoticeEvidence,
    ProductComplianceError,
    _require_sha256,
    _require_text,
    _require_unique_text,
)

_DECISION_AUTHORITY_KEY = secrets.token_bytes(32)
_DECISION_LOCK = RLock()
_LATEST_DECISION_PROOFS: dict[str, str] = {}


@dataclass(frozen=True, slots=True)
class ProductComplianceDecision:
    """PF10 result whose positive authority is issued only by this process's current gate."""

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
            _require_sha256(self.input_fingerprint, "compliance decision input_fingerprint")
        if self._authority_proof is not None:
            _require_text(self._authority_proof, "compliance decision authority proof")

    def __getattribute__(self, name: str):
        if name == "allowed":
            raw_allowed = object.__getattribute__(self, "allowed")
            if not raw_allowed:
                return False
            return _valid_decision_proof(self)
        return object.__getattribute__(self, name)


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
    with _DECISION_LOCK:
        _LATEST_DECISION_PROOFS[project_id] = proof
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
    input_fingerprint: str,
) -> str:
    payload = json.dumps(
        {
            "schema": "nika-pf10-decision-authority-v2",
            "project_id": project_id,
            "allowed": allowed,
            "findings": list(findings),
            "evidence_refs": list(evidence_refs),
            "input_fingerprint": input_fingerprint,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_DECISION_AUTHORITY_KEY, payload, hashlib.sha256).hexdigest()


def _valid_decision_proof(decision: ProductComplianceDecision) -> bool:
    proof = object.__getattribute__(decision, "_authority_proof")
    input_fingerprint = object.__getattribute__(decision, "input_fingerprint")
    if not isinstance(proof, str) or not proof or not isinstance(input_fingerprint, str):
        return False
    expected = _decision_proof(
        project_id=object.__getattribute__(decision, "project_id"),
        allowed=object.__getattribute__(decision, "allowed"),
        findings=object.__getattribute__(decision, "findings"),
        evidence_refs=object.__getattribute__(decision, "evidence_refs"),
        input_fingerprint=input_fingerprint,
    )
    if not hmac.compare_digest(proof, expected):
        return False
    with _DECISION_LOCK:
        current = _LATEST_DECISION_PROOFS.get(object.__getattribute__(decision, "project_id"))
    return current is not None and hmac.compare_digest(proof, current)


def _compliance_input_fingerprint(
    *,
    project_id: str,
    dependencies: tuple[DependencyAdoption, ...],
    packaged_dependencies: tuple[PackagedDependencyEvidence, ...],
    obligation_evidence: tuple[DistributionObligationEvidence, ...],
    notice_evidence: tuple[PackagingNoticeEvidence, ...],
    competitor_evidence: tuple[CompetitorResearchEvidence, ...],
    scope_review_ref: str | None,
) -> str:
    payload = {
        "schema": "nika-pf10-compliance-input-v2",
        "project_id": project_id,
        "dependencies": _canonical_records(_dependency_payload(item) for item in dependencies),
        "packaged_dependencies": _canonical_records(
            _packaged_dependency_payload(item) for item in packaged_dependencies
        ),
        "obligation_evidence": _canonical_records(
            _obligation_payload(item) for item in obligation_evidence
        ),
        "notice_evidence": _canonical_records(_notice_payload(item) for item in notice_evidence),
        "competitor_evidence": _canonical_records(
            _competitor_payload(item) for item in competitor_evidence
        ),
        "scope_review_ref": scope_review_ref,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_records(records: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    values = list(records)
    return sorted(values, key=_canonical_record_json)


def _canonical_record_json(item: dict[str, object]) -> str:
    return json.dumps(
        item,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _dependency_payload(item: DependencyAdoption) -> dict[str, object]:
    return {
        "project_id": item.project_id,
        "component_id": item.component_id,
        "package_name": item.package_name,
        "version": item.version,
        "source_ref": item.source_ref,
        "source_sha256": item.source_sha256,
        "provenance_ref": item.provenance_ref,
        "license_expression": item.license_expression,
        "license_disposition": item.license_disposition.value,
        "parent_component_ids": list(item.parent_component_ids),
        "distribution_obligations": list(item.distribution_obligations),
        "notice_required": item.notice_required,
        "notice_refs": list(item.notice_refs),
        "review_ref": item.review_ref,
    }


def _packaged_dependency_payload(item: PackagedDependencyEvidence) -> dict[str, object]:
    return {
        "project_id": item.project_id,
        "component_id": item.component_id,
        "package_name": item.package_name,
        "version": item.version,
        "source_sha256": item.source_sha256,
        "parent_component_ids": list(item.parent_component_ids),
    }


def _obligation_payload(item: DistributionObligationEvidence) -> dict[str, object]:
    return {
        "project_id": item.project_id,
        "component_id": item.component_id,
        "obligation": item.obligation,
        "fulfillment_ref": item.fulfillment_ref,
    }


def _notice_payload(item: PackagingNoticeEvidence) -> dict[str, object]:
    return {
        "project_id": item.project_id,
        "component_id": item.component_id,
        "package_name": item.package_name,
        "version": item.version,
        "notice_ref": item.notice_ref,
    }


def _competitor_payload(item: CompetitorResearchEvidence) -> dict[str, object]:
    return {
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
