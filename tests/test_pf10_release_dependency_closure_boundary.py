from __future__ import annotations

import hashlib

import nika_core.product_release_compliance as release_module
from nika_core.product_compliance import DependencyAdoption, LicenseDisposition
from nika_core.product_release_compliance import (
    ProductReleaseComplianceGate,
    ReleaseComplianceSnapshot,
    ReleaseDependency,
)


class _ExactReviewAuthority:
    def verify(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool:
        return project_id == "project-closure" and evidence_ref.startswith("review:") and bool(purpose)


def test_release_snapshot_carries_trusted_dependency_closure_to_base_pf10(
    tmp_path,
    monkeypatch,
) -> None:
    notice_file = tmp_path / "THIRD_PARTY_NOTICES.txt"
    notice_file.write_text("verified notice bundle\n", encoding="utf-8")
    notice_sha = hashlib.sha256(notice_file.read_bytes()).hexdigest()
    monkeypatch.setattr(release_module, "verify_third_party_notices", lambda _path: ())

    dependency = ReleaseDependency(
        adoption=DependencyAdoption(
            project_id="project-closure",
            component_id="component-a",
            package_name="component-a",
            version="1.0.0",
            source_ref="registry:pypi:component-a:1.0.0",
            provenance_ref="sha256:" + "1" * 64,
            license_expression="MIT",
            license_disposition=LicenseDisposition.APPROVED,
            review_ref="review:license:component-a:1.0.0",
        ),
        source_sha256="2" * 64,
    )
    snapshot = ReleaseComplianceSnapshot(
        project_id="project-closure",
        release_id="release-1",
        project_source_ref="git:Oleksii-debug/Nika-Core@exact",
        project_source_sha256="3" * 64,
        artifact_ref="artifact:release-1",
        artifact_sha256="4" * 64,
        notice_bundle_sha256=notice_sha,
        dependencies=(dependency,),
        dependency_closure_ref="review:dependency-closure:project-closure",
        scope_review_ref="review:scope:project-closure",
    )

    decision = ProductReleaseComplianceGate(
        review_authority=_ExactReviewAuthority(),
    ).evaluate(snapshot, bundle_dir=tmp_path)

    assert decision.allowed is True
    assert "dependency-closure:unverified" not in decision.findings
    assert "review:dependency-closure:project-closure" in decision.evidence_refs
