from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.packaging import notices
from nika_core.product_compliance import (
    DependencyAdoption,
    DistributionObligationEvidence,
    LicenseDisposition,
    ProductComplianceError,
)
from nika_core.product_compliance_release import (
    ProductComplianceInventory,
    ProductComplianceReleaseService,
    ProductComplianceRepository,
    VerifiedPackagingNoticeAuthority,
    verified_packaging_notice_authority,
)


class _ReviewAuthority:
    def __init__(self, grants: tuple[tuple[str, str, str], ...]) -> None:
        self._grants = frozenset(grants)

    def verify(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool:
        return (project_id, evidence_ref, purpose) in self._grants


def _packaging_authority(
    tmp_path,
    monkeypatch,
    project_id: str = "project-1",
) -> VerifiedPackagingNoticeAuthority:
    monkeypatch.setattr(notices, "RUNTIME_DISTRIBUTIONS", ("packaging",))
    monkeypatch.setattr(notices, "_python_license", lambda: "Python test license")
    notices.build_third_party_notices(tmp_path)
    return verified_packaging_notice_authority(
        project_id=project_id,
        bundle_dir=tmp_path,
    )


def _inventory(authority: VerifiedPackagingNoticeAuthority) -> ProductComplianceInventory:
    entry = authority.entries[0]
    component = "runtime-packaging"
    dependency = DependencyAdoption(
        project_id="project-1",
        component_id=component,
        package_name=entry.package_name,
        version=entry.version,
        source_ref=f"registry:pypi:{entry.package_name}:{entry.version}",
        provenance_ref="hash:sha256:fixture-release-digest",
        license_expression="MIT",
        license_disposition=LicenseDisposition.APPROVED,
        distribution_obligations=("retain-license",),
        notice_required=True,
        notice_refs=(entry.notice_ref,),
        review_ref="review:license:packaging",
    )
    return ProductComplianceInventory(
        project_id="project-1",
        dependencies=(dependency,),
        obligation_evidence=(
            DistributionObligationEvidence(
                project_id="project-1",
                component_id=component,
                obligation="retain-license",
                fulfillment_ref=entry.notice_ref,
            ),
        ),
    )


def _service(
    tmp_path,
    *,
    extra_grants: tuple[tuple[str, str, str], ...] = (),
) -> tuple[ProductComplianceReleaseService, ProductComplianceRepository, SQLiteStore]:
    store = SQLiteStore(tmp_path / "nika.sqlite")
    store.initialize()
    repository = ProductComplianceRepository(store)
    review = _ReviewAuthority(
        (
            (
                "project-1",
                "review:license:packaging",
                "license-disposition:runtime-packaging",
            ),
            *extra_grants,
        )
    )
    return (
        ProductComplianceReleaseService(repository, review_authority=review),
        repository,
        store,
    )


def test_packaging_notice_generator_binds_exact_release_and_restart(
    tmp_path,
    monkeypatch,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    authority = _packaging_authority(bundle, monkeypatch)
    service, _, _ = _service(tmp_path)
    inventory = _inventory(authority)

    state = service.record_inventory(inventory)
    assert state.revision == 1
    decision = service.evaluate_current(
        project_id="project-1",
        packaging_notices=authority,
    )
    assert decision.allowed is True
    service.require_delivery_allowed(project_id="project-1", decision=decision)

    restarted, repository, _ = _service(tmp_path)
    restored = repository.get("project-1")
    assert restored.inventory == inventory
    assert restored.assessment_fingerprint == decision.inventory_fingerprint
    restarted.require_delivery_allowed(project_id="project-1", decision=decision)


def test_dependency_change_invalidates_old_delivery_decision(
    tmp_path,
    monkeypatch,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    authority = _packaging_authority(bundle, monkeypatch)
    service, repository, _ = _service(tmp_path)
    inventory = _inventory(authority)
    service.record_inventory(inventory)
    decision = service.evaluate_current(
        project_id="project-1",
        packaging_notices=authority,
    )
    assert decision.allowed is True

    dependency = inventory.dependencies[0]
    changed = replace(
        inventory,
        dependencies=(replace(dependency, version=dependency.version + ".post1"),),
    )
    updated = service.record_inventory(changed)
    assert updated.revision == 2
    assert updated.assessment_fingerprint is None
    with pytest.raises(ProductComplianceError, match="no-current-assessment"):
        service.require_delivery_allowed(project_id="project-1", decision=decision)
    assert repository.get("project-1").inventory == changed


def test_packaged_transitive_dependency_cannot_be_omitted(tmp_path, monkeypatch) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    authority = _packaging_authority(bundle, monkeypatch)
    service, _, _ = _service(
        tmp_path,
        extra_grants=(("project-1", "review:scope:1", "compliance-scope"),),
    )
    service.record_inventory(
        ProductComplianceInventory(
            project_id="project-1",
            scope_review_ref="review:scope:1",
        )
    )
    decision = service.evaluate_current(
        project_id="project-1",
        packaging_notices=authority,
    )
    assert decision.allowed is False
    assert any(
        item.startswith("transitive-dependency:unrecorded:packaging:")
        for item in decision.findings
    )


def test_fabricated_or_cross_project_packaging_authority_cannot_release(
    tmp_path,
    monkeypatch,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    authority = _packaging_authority(bundle, monkeypatch)
    service, _, _ = _service(tmp_path)
    inventory = _inventory(authority)
    service.record_inventory(inventory)

    forged = VerifiedPackagingNoticeAuthority(
        project_id="project-1",
        bundle_digest=authority.bundle_digest,
        entries=authority.entries,
    )
    assert forged.verified is False
    forged_decision = service.evaluate_current(
        project_id="project-1",
        packaging_notices=forged,
    )
    assert forged_decision.allowed is False
    assert "packaging-notices:untrusted-or-cross-project" in forged_decision.findings

    other_project = _packaging_authority(bundle, monkeypatch, project_id="project-2")
    cross = service.evaluate_current(
        project_id="project-1",
        packaging_notices=other_project,
    )
    assert cross.allowed is False
    assert "packaging-notices:untrusted-or-cross-project" in cross.findings


def test_durable_inventory_tamper_is_rejected(tmp_path, monkeypatch) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    authority = _packaging_authority(bundle, monkeypatch)
    service, repository, store = _service(tmp_path)
    service.record_inventory(_inventory(authority))

    with store.connection() as conn:
        conn.execute(
            "UPDATE product_compliance_state SET inventory_json = ? WHERE project_id = ?",
            (
                '{"schema":"nika-pf10-compliance-state-v1","project_id":"attacker"}',
                "project-1",
            ),
        )
    with pytest.raises(ProductComplianceError):
        repository.get("project-1")
