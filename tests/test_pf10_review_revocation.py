from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import nika_core.product_release_compliance as release_module
from nika_core.product_compliance import (
    DependencyAdoption,
    LicenseDisposition,
    ProductComplianceError,
)
from nika_core.product_release_compliance import (
    ProductReleaseComplianceGate,
    ReleaseComplianceSnapshot,
    ReleaseDependency,
)

_PROJECT_ID = "project-review-revocation"
_CLOSURE_REF = "review:dependency-closure:revocation"
_SCOPE_REF = "review:compliance-scope:revocation"
_LICENSE_REF = "review:license:revocation"


class _RevocableReviewAuthority:
    def __init__(self) -> None:
        self.active = True

    def verify(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool:
        if not self.active or project_id != _PROJECT_ID:
            return False
        if evidence_ref == _CLOSURE_REF:
            return purpose.startswith("dependency-closure:")
        if evidence_ref == _SCOPE_REF:
            return purpose.startswith("compliance-scope:")
        if evidence_ref == _LICENSE_REF:
            return purpose.startswith("license-disposition:component-revocation:")
        return False


def _snapshot(bundle_dir: Path) -> ReleaseComplianceSnapshot:
    notices = bundle_dir / "THIRD_PARTY_NOTICES.txt"
    notices.write_text("revocation fixture notices\n", encoding="utf-8")
    notice_digest = hashlib.sha256(notices.read_bytes()).hexdigest()
    dependency = DependencyAdoption(
        project_id=_PROJECT_ID,
        component_id="component-revocation",
        package_name="example-package",
        version="1.0.0",
        source_ref="registry:pypi:example-package:1.0.0",
        provenance_ref="hash:sha256:" + "a" * 64,
        license_expression="MIT",
        license_disposition=LicenseDisposition.APPROVED,
        review_ref=_LICENSE_REF,
    )
    return ReleaseComplianceSnapshot(
        project_id=_PROJECT_ID,
        release_id="release-revocation-1",
        project_source_ref="git:Oleksii-debug/Nika-Core@exact",
        project_source_sha256="b" * 64,
        artifact_ref="artifact:release-revocation-1",
        artifact_sha256="c" * 64,
        notice_bundle_sha256=notice_digest,
        dependencies=(
            ReleaseDependency(
                adoption=dependency,
                source_sha256="d" * 64,
            ),
        ),
        dependency_closure_ref=_CLOSURE_REF,
        scope_review_ref=_SCOPE_REF,
    )


def test_release_grant_revalidates_current_review_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_module, "verify_third_party_notices", lambda _path: ())
    authority = _RevocableReviewAuthority()
    snapshot = _snapshot(tmp_path)
    gate = ProductReleaseComplianceGate(review_authority=authority)

    decision = gate.evaluate(snapshot, bundle_dir=tmp_path)
    assert decision.allowed is True

    authority.active = False
    with pytest.raises(ProductComplianceError, match="untrusted-review-authority"):
        gate.require_release_allowed(decision, snapshot, bundle_dir=tmp_path)
