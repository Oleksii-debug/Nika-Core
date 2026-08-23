from __future__ import annotations

import secrets

import pytest

import nika_core.packaging.notices as notices
import nika_core.product_compliance as compliance_module
from nika_core.packaging.notices import (
    ThirdPartyNoticeAuthority,
    build_third_party_notices,
    third_party_notice_ref,
)
from nika_core.product_compliance import (
    DependencyAdoption,
    LicenseDisposition,
    ProductComplianceError,
    ProductComplianceGate,
)


class _ReviewAuthority:
    def __init__(self, component_ids: tuple[str, ...]) -> None:
        self._grants = frozenset(
            (
                "project-1",
                f"review:license:{component_id}",
                f"license-disposition:{component_id}",
            )
            for component_id in component_ids
        )

    def verify(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool:
        return (project_id, evidence_ref, purpose) in self._grants


def _dependency(
    component_id: str,
    *,
    package_name: str = "example-package",
    version: str = "1.2.3",
    source_ref: str = "registry:pypi:example-package:1.2.3",
    provenance_ref: str = "hash:sha256:fixture",
    license_expression: str = "MIT",
    notice_required: bool = False,
    notice_refs: tuple[str, ...] = (),
) -> DependencyAdoption:
    return DependencyAdoption(
        project_id="project-1",
        component_id=component_id,
        package_name=package_name,
        version=version,
        source_ref=source_ref,
        provenance_ref=provenance_ref,
        license_expression=license_expression,
        license_disposition=LicenseDisposition.APPROVED,
        notice_required=notice_required,
        notice_refs=notice_refs,
        review_ref=f"review:license:{component_id}",
    )


def _gate(*component_ids: str) -> ProductComplianceGate:
    return ProductComplianceGate(review_authority=_ReviewAuthority(component_ids))


def test_duplicate_physical_dependency_cannot_hide_behind_component_aliases() -> None:
    decision = _gate("desktop", "transport").evaluate(
        project_id="project-1",
        dependencies=(_dependency("desktop"), _dependency("transport")),
    )

    assert decision.allowed is False
    assert any(item.startswith("duplicate:dependency-physical-identity") for item in decision.findings)


def test_unknown_license_expression_cannot_be_review_laundered() -> None:
    decision = _gate("desktop").evaluate(
        project_id="project-1",
        dependencies=(_dependency("desktop", license_expression="UNKNOWN"),),
    )

    assert decision.allowed is False
    assert "license:unknown:desktop" in decision.findings


def test_mutable_source_and_locator_only_provenance_fail_closed() -> None:
    decision = _gate("desktop").evaluate(
        project_id="project-1",
        dependencies=(
            _dependency(
                "desktop",
                source_ref="https://example.test/example-package/latest.whl",
                provenance_ref="locator:https://example.test/example-package/latest.whl",
            ),
        ),
    )

    assert decision.allowed is False
    assert "dependency-source:mutable:desktop" in decision.findings
    assert "dependency-provenance:not-content-addressed:desktop" in decision.findings


def test_non_exact_version_range_fails_closed() -> None:
    decision = _gate("desktop").evaluate(
        project_id="project-1",
        dependencies=(_dependency("desktop", version=">=1.2,<2"),),
    )

    assert decision.allowed is False
    assert "dependency-version:not-exact:desktop" in decision.findings


def test_orphan_notice_evidence_is_rejected_before_packaging_authority() -> None:
    decision = _gate("desktop").evaluate(
        project_id="project-1",
        dependencies=(
            _dependency(
                "desktop",
                notice_required=False,
                notice_refs=("artifact:THIRD_PARTY_NOTICES.txt#unexpected",),
            ),
        ),
    )

    assert decision.allowed is False
    assert "orphan:notice:desktop" in decision.findings


def test_dependency_change_changes_snapshot_and_release_identity() -> None:
    gate = _gate("desktop")
    first = gate.evaluate(
        project_id="project-1",
        dependencies=(_dependency("desktop"),),
    )
    second = gate.evaluate(
        project_id="project-1",
        dependencies=(
            _dependency(
                "desktop",
                version="1.2.4",
                source_ref="registry:pypi:example-package:1.2.4",
            ),
        ),
    )

    assert first.allowed is True
    assert second.allowed is True
    assert first.snapshot_sha256 != second.snapshot_sha256
    assert first.evidence_refs != second.evidence_refs
    with pytest.raises(ProductComplianceError, match="decision:stale-snapshot"):
        ProductComplianceGate().require_release_allowed(
            first,
            expected_snapshot_sha256=second.snapshot_sha256,
        )


def test_snapshot_is_deterministic_across_inventory_order() -> None:
    gate = _gate("desktop", "worker")
    desktop = _dependency("desktop")
    worker = _dependency(
        "worker",
        package_name="worker-package",
        source_ref="registry:pypi:worker-package:1.2.3",
        provenance_ref="hash:sha256:worker-fixture",
    )
    first = gate.evaluate(project_id="project-1", dependencies=(desktop, worker))
    second = gate.evaluate(project_id="project-1", dependencies=(worker, desktop))

    assert first.allowed is True
    assert second.allowed is True
    assert first.snapshot_sha256 == second.snapshot_sha256


def test_process_restart_invalidates_process_local_positive_decision(monkeypatch) -> None:
    decision = _gate("desktop").evaluate(
        project_id="project-1",
        dependencies=(_dependency("desktop"),),
    )
    assert decision.allowed is True

    monkeypatch.setattr(compliance_module, "_DECISION_AUTHORITY_KEY", secrets.token_bytes(32))

    assert decision.allowed is False
    with pytest.raises(ProductComplianceError, match="decision:untrusted-origin"):
        ProductComplianceGate().require_release_allowed(decision)


class _FakeMetadata(dict[str, str]):
    def get_all(self, key: str, default=None):
        del key
        return [] if default is None else default


class _FakeDistribution:
    version = "1.2.3"
    metadata = _FakeMetadata(
        {
            "Name": "example-package",
            "License-Expression": "MIT",
        }
    )
    files: tuple[object, ...] = ()


def test_generated_notice_authority_reuses_exact_notice_generator(monkeypatch, tmp_path) -> None:
    fake_dist = _FakeDistribution()
    monkeypatch.setattr(notices, "RUNTIME_DISTRIBUTIONS", ("example-package",))
    monkeypatch.setattr(notices, "_python_license", lambda: "Python test license")
    monkeypatch.setattr(notices.metadata, "distribution", lambda name: fake_dist)

    target = build_third_party_notices(tmp_path)
    ref = third_party_notice_ref("example-package 1.2.3")
    authority = ThirdPartyNoticeAuthority(project_id="project-1", bundle_dir=tmp_path)

    assert authority.verify(
        project_id="project-1",
        package_name="example-package",
        version="1.2.3",
        notice_ref=ref,
    )
    assert not authority.verify(
        project_id="other-project",
        package_name="example-package",
        version="1.2.3",
        notice_ref=ref,
    )
    assert not authority.verify(
        project_id="project-1",
        package_name="example-package",
        version="1.2.3",
        notice_ref="artifact:THIRD_PARTY_NOTICES.txt#made-up",
    )

    target.write_text(
        target.read_text(encoding="utf-8").replace("Declared license: MIT", "tampered"),
        encoding="utf-8",
    )
    assert not authority.verify(
        project_id="project-1",
        package_name="example-package",
        version="1.2.3",
        notice_ref=ref,
    )
