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

_PROJECT_ID = "aud-pf10-project"


class _TrustedReviewAuthority:
    def verify(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool:
        del evidence_ref, purpose
        return project_id == _PROJECT_ID


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_release_grant_rejects_artifact_bytes_changed_after_allowed_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "release-artifact.bin"
    artifact.write_bytes(b"artifact-v1")
    notices = tmp_path / "THIRD_PARTY_NOTICES.txt"
    notices.write_text("verified notices\n", encoding="utf-8")
    monkeypatch.setattr(release_module, "verify_third_party_notices", lambda _path: ())

    dependency = DependencyAdoption(
        project_id=_PROJECT_ID,
        component_id="dependency-1",
        package_name="example-package",
        version="1.0.0",
        source_ref="registry:pypi:example-package:1.0.0",
        provenance_ref="sha256:" + hashlib.sha256(b"dependency-source").hexdigest(),
        license_expression="MIT",
        license_disposition=LicenseDisposition.APPROVED,
        review_ref="review:license:dependency-1",
    )
    snapshot = ReleaseComplianceSnapshot(
        project_id=_PROJECT_ID,
        release_id="release-1",
        project_source_ref="git:example/project@exact",
        project_source_sha256=hashlib.sha256(b"project-source").hexdigest(),
        artifact_ref=str(artifact),
        artifact_sha256=_sha256(artifact),
        notice_bundle_sha256=_sha256(notices),
        dependencies=(
            ReleaseDependency(
                adoption=dependency,
                source_sha256=hashlib.sha256(b"dependency-source").hexdigest(),
            ),
        ),
        dependency_closure_ref="review:dependency-closure:release-1",
        scope_review_ref="review:scope:release-1",
    )
    gate = ProductReleaseComplianceGate(review_authority=_TrustedReviewAuthority())
    decision = gate.evaluate(snapshot, bundle_dir=tmp_path)
    assert decision.allowed is True

    artifact.write_bytes(b"artifact-v2-after-review")

    with pytest.raises(ProductComplianceError, match="artifact"):
        gate.require_release_allowed(decision, snapshot, bundle_dir=tmp_path)
