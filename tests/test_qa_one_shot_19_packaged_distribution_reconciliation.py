from __future__ import annotations

from dataclasses import dataclass

import pytest

import nika_core.packaging.notices as notices_module
from nika_core.product_compliance import DependencyAdoption, LicenseDisposition
from nika_core.product_release_compliance import (
    ProductReleaseComplianceGate,
    ReleaseComplianceSnapshot,
    ReleaseDependency,
    ReleaseNoticeEvidence,
    build_verified_notice_bundle,
)


class _FakeMetadata(dict[str, str]):
    def get_all(self, key: str, default: list[str] | None = None) -> list[str]:
        return [] if default is None else default


@dataclass
class _FakeDistribution:
    name: str
    version: str = "1.0.0"

    @property
    def metadata(self) -> _FakeMetadata:
        return _FakeMetadata({"Name": self.name, "License-Expression": "MIT"})

    @property
    def files(self) -> tuple[object, ...]:
        return ()


def test_release_snapshot_must_reconcile_with_canonical_packaged_distribution_set(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_versions = {
        "declared-pkg": "1.0.0",
        "undeclared-pkg": "9.9.9",
    }
    monkeypatch.setattr(
        notices_module,
        "RUNTIME_DISTRIBUTIONS",
        tuple(runtime_versions),
    )
    monkeypatch.setattr(notices_module, "_python_license", lambda: "Python license fixture")
    monkeypatch.setattr(
        notices_module.metadata,
        "distribution",
        lambda name: _FakeDistribution(name, runtime_versions[name]),
    )

    notice_hash = build_verified_notice_bundle(tmp_path)
    assert notices_module.verify_third_party_notices(tmp_path) == ()

    notice_ref = "artifact:THIRD_PARTY_NOTICES.txt#declared"
    dependency = ReleaseDependency(
        adoption=DependencyAdoption(
            project_id="project-1",
            component_id="declared",
            package_name="declared-pkg",
            version="1.0.0",
            source_ref="registry:pypi:declared-pkg:1.0.0",
            provenance_ref="sha256:" + "1" * 64,
            license_expression="MIT",
            license_disposition=LicenseDisposition.APPROVED,
            notice_required=True,
            notice_refs=(notice_ref,),
            review_ref="review:declared",
        ),
        source_sha256="2" * 64,
    )
    snapshot = ReleaseComplianceSnapshot(
        project_id="project-1",
        release_id="release-1",
        project_source_ref="git:Oleksii-debug/Nika-Core@exact",
        project_source_sha256="3" * 64,
        artifact_ref="artifact:nika-release:release-1",
        artifact_sha256="4" * 64,
        notice_bundle_sha256=notice_hash,
        dependencies=(dependency,),
        notice_evidence=(
            ReleaseNoticeEvidence(
                project_id="project-1",
                component_id="declared",
                notice_ref=notice_ref,
                package_name="declared-pkg",
                version="1.0.0",
            ),
        ),
        dependency_closure_ref="review:closure",
        scope_review_ref="review:scope",
    )

    decision = ProductReleaseComplianceGate().evaluate(snapshot, bundle_dir=tmp_path)

    assert any(
        "undeclared-pkg" in finding
        for finding in decision.findings
    ), (
        "canonical packaged notices contain undeclared-pkg 9.9.9, but the PF10 release snapshot "
        "does not reconcile its dependency inventory against the packaged distribution set"
    )
