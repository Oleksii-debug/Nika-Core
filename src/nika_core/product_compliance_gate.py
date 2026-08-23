from __future__ import annotations

from .product_compliance_decision import (
    ProductComplianceDecision,
    _compliance_input_fingerprint,
    _issue_decision,
    _valid_decision_proof,
)
from .product_compliance_models import (
    ComplianceReviewAuthorityPort,
    CompetitorResearchEvidence,
    DependencyAdoption,
    DistributionObligationEvidence,
    LicenseDisposition,
    PackagedDependencyEvidence,
    PackagingNoticeEvidence,
    ProductComplianceError,
    _normalize_package_name,
    _require_text,
    _require_tuple,
)


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
        packaged_dependencies: tuple[PackagedDependencyEvidence, ...] = (),
        obligation_evidence: tuple[DistributionObligationEvidence, ...] = (),
        notice_evidence: tuple[PackagingNoticeEvidence, ...] = (),
        competitor_evidence: tuple[CompetitorResearchEvidence, ...] = (),
        scope_review_ref: str | None = None,
    ) -> ProductComplianceDecision:
        _require_text(project_id, "project_id")
        _require_tuple(dependencies, "dependencies")
        _require_tuple(packaged_dependencies, "packaged_dependencies")
        _require_tuple(obligation_evidence, "obligation_evidence")
        _require_tuple(notice_evidence, "notice_evidence")
        _require_tuple(competitor_evidence, "competitor_evidence")
        if scope_review_ref is not None:
            _require_text(scope_review_ref, "scope_review_ref")

        input_fingerprint = _compliance_input_fingerprint(
            project_id=project_id,
            dependencies=dependencies,
            packaged_dependencies=packaged_dependencies,
            obligation_evidence=obligation_evidence,
            notice_evidence=notice_evidence,
            competitor_evidence=competitor_evidence,
            scope_review_ref=scope_review_ref,
        )
        findings: list[str] = []
        evidence_refs: list[str] = [f"compliance-input:sha256:{input_fingerprint}"]
        trusted_scope_refs: set[str] = set()

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

        obligations = _collect_obligation_evidence(
            project_id,
            obligation_evidence,
            evidence_refs,
            findings,
        )
        dependency_by_component = _collect_dependencies(
            project_id,
            dependencies,
            evidence_refs,
            findings,
        )
        _validate_dependency_graph(dependency_by_component, findings)
        _validate_packaged_dependencies(
            project_id,
            dependency_by_component,
            packaged_dependencies,
            findings,
        )
        notices = _collect_notice_evidence(
            project_id,
            dependency_by_component,
            notice_evidence,
            evidence_refs,
            findings,
        )

        for dependency in dependency_by_component.values():
            self._evaluate_dependency(
                project_id,
                dependency,
                obligations,
                notices,
                trusted_scope_refs,
                findings,
            )

        for component_id, obligation in obligations:
            if component_id not in dependency_by_component:
                findings.append(f"orphan:distribution-obligation:{component_id}:{obligation}")

        _evaluate_competitor_evidence(
            gate=self,
            project_id=project_id,
            competitor_evidence=competitor_evidence,
            evidence_refs=evidence_refs,
            trusted_scope_refs=trusted_scope_refs,
            findings=findings,
        )

        if not trusted_scope_refs:
            findings.append("compliance-scope:unreviewed")

        normalized_findings = tuple(dict.fromkeys(findings))
        normalized_evidence = tuple(dict.fromkeys(evidence_refs))
        return _issue_decision(
            project_id=project_id,
            allowed=not normalized_findings,
            findings=normalized_findings,
            evidence_refs=normalized_evidence,
            input_fingerprint=input_fingerprint,
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

    def _evaluate_dependency(
        self,
        project_id: str,
        dependency: DependencyAdoption,
        obligations: dict[tuple[str, str], DistributionObligationEvidence],
        notices: dict[str, PackagingNoticeEvidence],
        trusted_scope_refs: set[str],
        findings: list[str],
    ) -> None:
        component_id = dependency.component_id
        if dependency.source_sha256 is None:
            findings.append(f"source:missing-commitment:{component_id}")

        if dependency.license_disposition is LicenseDisposition.BLOCKED:
            findings.append(f"license:blocked:{component_id}")
        elif dependency.license_disposition is LicenseDisposition.REVIEW_REQUIRED:
            findings.append(f"license:review-required:{component_id}")
        elif dependency.review_ref is not None:
            purpose = f"license-disposition:{component_id}"
            if self._review_allowed(
                project_id=project_id,
                evidence_ref=dependency.review_ref,
                purpose=purpose,
            ):
                trusted_scope_refs.add(dependency.review_ref)
            else:
                findings.append(f"license:untrusted-review-authority:{component_id}")

        for obligation in dependency.distribution_obligations:
            if (component_id, obligation) not in obligations:
                findings.append(
                    f"distribution-obligation:unfulfilled:{component_id}:{obligation}"
                )
        for notice_ref in dependency.notice_refs:
            item = notices.get(notice_ref)
            if item is None:
                findings.append(f"notice:unresolved:{component_id}:{notice_ref}")
            elif item.component_id != component_id:
                findings.append(f"notice:component-mismatch:{component_id}:{notice_ref}")
        if dependency.notice_required and not dependency.notice_refs:
            findings.append(f"notice:missing:{component_id}")

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


def _collect_obligation_evidence(
    project_id: str,
    items: tuple[DistributionObligationEvidence, ...],
    evidence_refs: list[str],
    findings: list[str],
) -> dict[tuple[str, str], DistributionObligationEvidence]:
    obligations: dict[tuple[str, str], DistributionObligationEvidence] = {}
    for item in items:
        evidence_refs.append(item.fulfillment_ref)
        if item.project_id != project_id:
            findings.append("cross-project:distribution-obligation")
            continue
        key = (item.component_id, item.obligation)
        if key in obligations:
            findings.append(
                "duplicate:distribution-obligation:"
                f"{item.component_id}:{item.obligation}"
            )
            continue
        obligations[key] = item
    return obligations


def _collect_dependencies(
    project_id: str,
    dependencies: tuple[DependencyAdoption, ...],
    evidence_refs: list[str],
    findings: list[str],
) -> dict[str, DependencyAdoption]:
    by_component: dict[str, DependencyAdoption] = {}
    package_coordinates: dict[tuple[str, str], str] = {}
    for dependency in dependencies:
        if dependency.project_id != project_id:
            findings.append(f"cross-project:dependency:{dependency.component_id}")
            continue
        if dependency.component_id in by_component:
            findings.append(f"duplicate:dependency-component:{dependency.component_id}")
            continue
        coordinate = (_normalize_package_name(dependency.package_name), dependency.version)
        if coordinate in package_coordinates:
            findings.append(
                f"duplicate:dependency-identity:{dependency.package_name}:{dependency.version}"
            )
        else:
            package_coordinates[coordinate] = dependency.component_id
        by_component[dependency.component_id] = dependency
        evidence_refs.extend((dependency.source_ref, dependency.provenance_ref))
        if dependency.source_sha256 is not None:
            evidence_refs.append(f"source-sha256:{dependency.source_sha256}")
        if dependency.review_ref:
            evidence_refs.append(dependency.review_ref)
        evidence_refs.extend(dependency.notice_refs)
    return by_component


def _validate_dependency_graph(
    dependencies: dict[str, DependencyAdoption],
    findings: list[str],
) -> None:
    for dependency in dependencies.values():
        for parent_id in dependency.parent_component_ids:
            if parent_id not in dependencies:
                findings.append(
                    f"dependency:missing-parent:{dependency.component_id}:{parent_id}"
                )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(component_id: str) -> None:
        if component_id in visited:
            return
        if component_id in visiting:
            findings.append(f"dependency-graph:cycle:{component_id}")
            return
        visiting.add(component_id)
        for parent_id in dependencies[component_id].parent_component_ids:
            if parent_id in dependencies:
                visit(parent_id)
        visiting.remove(component_id)
        visited.add(component_id)

    for component_id in dependencies:
        visit(component_id)


def _validate_packaged_dependencies(
    project_id: str,
    dependencies: dict[str, DependencyAdoption],
    packaged: tuple[PackagedDependencyEvidence, ...],
    findings: list[str],
) -> None:
    if dependencies and not packaged:
        findings.append("dependency-inventory:missing")
        return

    packaged_by_component: dict[str, PackagedDependencyEvidence] = {}
    for item in packaged:
        if item.project_id != project_id:
            findings.append(f"cross-project:packaged-dependency:{item.component_id}")
            continue
        if item.component_id in packaged_by_component:
            findings.append(f"duplicate:packaged-dependency:{item.component_id}")
            continue
        packaged_by_component[item.component_id] = item
        dependency = dependencies.get(item.component_id)
        if dependency is None:
            findings.append(f"unreviewed:packaged-dependency:{item.component_id}")
            continue
        if _normalize_package_name(item.package_name) != _normalize_package_name(
            dependency.package_name
        ):
            findings.append(f"packaged-dependency:package-mismatch:{item.component_id}")
        if item.version != dependency.version:
            findings.append(f"packaged-dependency:version-mismatch:{item.component_id}")
        if dependency.source_sha256 is None or item.source_sha256 != dependency.source_sha256:
            findings.append(f"packaged-dependency:source-mismatch:{item.component_id}")
        if item.parent_component_ids != dependency.parent_component_ids:
            findings.append(f"packaged-dependency:parent-mismatch:{item.component_id}")

    for component_id in dependencies:
        if component_id not in packaged_by_component:
            findings.append(f"packaged-dependency:missing:{component_id}")


def _collect_notice_evidence(
    project_id: str,
    dependencies: dict[str, DependencyAdoption],
    items: tuple[PackagingNoticeEvidence, ...],
    evidence_refs: list[str],
    findings: list[str],
) -> dict[str, PackagingNoticeEvidence]:
    notices: dict[str, PackagingNoticeEvidence] = {}
    declared_refs = {
        notice_ref
        for dependency in dependencies.values()
        for notice_ref in dependency.notice_refs
    }
    for item in items:
        evidence_refs.append(item.notice_ref)
        if item.project_id != project_id:
            findings.append(f"cross-project:notice:{item.component_id}")
            continue
        if item.notice_ref in notices:
            findings.append(f"duplicate:notice-ref:{item.notice_ref}")
            continue
        notices[item.notice_ref] = item
        dependency = dependencies.get(item.component_id)
        if dependency is None:
            findings.append(f"orphan:notice:{item.component_id}:{item.notice_ref}")
            continue
        if item.notice_ref not in declared_refs:
            findings.append(f"orphan:notice-ref:{item.component_id}:{item.notice_ref}")
        if _normalize_package_name(item.package_name) != _normalize_package_name(
            dependency.package_name
        ):
            findings.append(f"notice:package-mismatch:{item.component_id}:{item.notice_ref}")
        if item.version != dependency.version:
            findings.append(f"notice:version-mismatch:{item.component_id}:{item.notice_ref}")
    return notices


def _evaluate_competitor_evidence(
    *,
    gate: ProductComplianceGate,
    project_id: str,
    competitor_evidence: tuple[CompetitorResearchEvidence, ...],
    evidence_refs: list[str],
    trusted_scope_refs: set[str],
    findings: list[str],
) -> None:
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
            _evaluate_proprietary_evidence(
                gate,
                project_id,
                evidence,
                trusted_scope_refs,
                findings,
            )
        elif evidence.permitted_public_evidence:
            permission_ref = evidence.permission_basis_ref
            if permission_ref is None or not gate._review_allowed(
                project_id=project_id,
                evidence_ref=permission_ref or "",
                purpose=f"public-source-permission:{evidence.evidence_id}",
            ):
                findings.append(
                    f"research-source:untrusted-permission-authority:{evidence.evidence_id}"
                )
            else:
                trusted_scope_refs.add(permission_ref)
        else:
            findings.append(f"research-source:not-permitted:{evidence.evidence_id}")


def _evaluate_proprietary_evidence(
    gate: ProductComplianceGate,
    project_id: str,
    evidence: CompetitorResearchEvidence,
    trusted_scope_refs: set[str],
    findings: list[str],
) -> None:
    if not evidence.legal_basis_ref or not evidence.reuse_authorization_ref:
        findings.append(f"proprietary-reuse:not-authorized:{evidence.evidence_id}")
        return
    legal_ok = gate._review_allowed(
        project_id=project_id,
        evidence_ref=evidence.legal_basis_ref,
        purpose=f"proprietary-legal-basis:{evidence.evidence_id}",
    )
    reuse_ok = gate._review_allowed(
        project_id=project_id,
        evidence_ref=evidence.reuse_authorization_ref,
        purpose=f"proprietary-reuse-authorization:{evidence.evidence_id}",
    )
    if legal_ok and reuse_ok:
        trusted_scope_refs.update((evidence.legal_basis_ref, evidence.reuse_authorization_ref))
    else:
        findings.append(f"proprietary-reuse:untrusted-authority:{evidence.evidence_id}")
