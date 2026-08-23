import base64
import pickle
import subprocess
import sys
from dataclasses import replace

import pytest

from nika_core.packaging.compliance import build_pf10_notice_evidence
from nika_core.packaging.notices import distribution_notice_record
from nika_core.product_compliance import (
    DependencyAdoption,
    DistributionObligationEvidence,
    LicenseDisposition,
    PackagedDependencyEvidence,
    PackagingNoticeEvidence,
    ProductComplianceError,
    ProductComplianceGate,
)

SHA_A = "a" * 64
SHA_B = "b" * 64


class Authority:
    def verify(self, *, project_id: str, evidence_ref: str, purpose: str) -> bool:
        return (project_id, evidence_ref, purpose) in {
            ("p", "review:a", "license-disposition:a"),
            ("p", "review:b", "license-disposition:b"),
            ("p", "scope", "compliance-scope"),
        }


def dep(
    component="a",
    version="1.0.0",
    sha=SHA_A,
    parents=(),
    notice_refs=("notice:a",),
    review="review:a",
):
    return DependencyAdoption(
        project_id="p",
        component_id=component,
        package_name=f"pkg-{component}",
        version=version,
        source_ref=f"https://example.invalid/{component}/main.zip",
        provenance_ref=f"prov:{component}",
        license_expression="MIT",
        license_disposition=LicenseDisposition.APPROVED,
        source_sha256=sha,
        parent_component_ids=parents,
        distribution_obligations=("retain-license",),
        notice_required=True,
        notice_refs=notice_refs,
        review_ref=review,
    )


def pkg(d):
    return PackagedDependencyEvidence(
        project_id=d.project_id,
        component_id=d.component_id,
        package_name=d.package_name,
        version=d.version,
        source_sha256=d.source_sha256,
        parent_component_ids=d.parent_component_ids,
    )


def obl(d):
    return DistributionObligationEvidence(
        project_id="p",
        component_id=d.component_id,
        obligation="retain-license",
        fulfillment_ref=f"fulfill:{d.component_id}",
    )


def notice(d):
    return PackagingNoticeEvidence(
        project_id="p",
        component_id=d.component_id,
        package_name=d.package_name,
        version=d.version,
        notice_ref=d.notice_refs[0],
    )


def evaluate(*deps):
    return ProductComplianceGate(review_authority=Authority()).evaluate(
        project_id="p",
        dependencies=deps,
        packaged_dependencies=tuple(pkg(item) for item in deps),
        obligation_evidence=tuple(obl(item) for item in deps),
        notice_evidence=tuple(notice(item) for item in deps),
        scope_review_ref="scope",
    )


def test_complete_release_path_allows_exact_evidence():
    d = dep()
    decision = evaluate(d)
    assert decision.allowed is True
    assert decision.input_fingerprint is not None
    assert any(ref.startswith("compliance-input:sha256:") for ref in decision.evidence_refs)


def test_missing_source_digest_blocks():
    d = replace(dep(), source_sha256=None)
    decision = ProductComplianceGate(review_authority=Authority()).evaluate(
        project_id="p",
        dependencies=(d,),
        packaged_dependencies=(),
        obligation_evidence=(obl(d),),
        notice_evidence=(notice(d),),
        scope_review_ref="scope",
    )
    assert decision.allowed is False
    assert "source:missing-commitment:a" in decision.findings


def test_transitive_omission_and_unreviewed_package_block():
    a = dep()
    b = dep("b", sha=SHA_B, parents=("a",), notice_refs=("notice:b",), review="review:b")
    decision = ProductComplianceGate(review_authority=Authority()).evaluate(
        project_id="p",
        dependencies=(a, b),
        packaged_dependencies=(pkg(a),),
        obligation_evidence=(obl(a), obl(b)),
        notice_evidence=(notice(a), notice(b)),
        scope_review_ref="scope",
    )
    assert decision.allowed is False
    assert "packaged-dependency:missing:b" in decision.findings

    extra = PackagedDependencyEvidence(
        project_id="p",
        component_id="c",
        package_name="pkg-c",
        version="1.0.0",
        source_sha256="c" * 64,
        parent_component_ids=("a",),
    )
    extra_decision = ProductComplianceGate(review_authority=Authority()).evaluate(
        project_id="p",
        dependencies=(a,),
        packaged_dependencies=(pkg(a), extra),
        obligation_evidence=(obl(a),),
        notice_evidence=(notice(a),),
        scope_review_ref="scope",
    )
    assert "unreviewed:packaged-dependency:c" in extra_decision.findings


def test_orphan_notice_blocks():
    d = dep()
    orphan = PackagingNoticeEvidence(
        project_id="p",
        component_id="ghost",
        package_name="ghost",
        version="1.0.0",
        notice_ref="notice:ghost",
    )
    decision = ProductComplianceGate(review_authority=Authority()).evaluate(
        project_id="p",
        dependencies=(d,),
        packaged_dependencies=(pkg(d),),
        obligation_evidence=(obl(d),),
        notice_evidence=(notice(d), orphan),
        scope_review_ref="scope",
    )
    assert "orphan:notice:ghost:notice:ghost" in decision.findings


def test_unknown_license_and_version_range_rejected():
    with pytest.raises(ProductComplianceError, match="unknown or unresolved"):
        replace(dep(), license_expression="NOASSERTION")
    with pytest.raises(ProductComplianceError, match="range or mutable"):
        replace(dep(), version=">=1,<2")


def test_duplicate_dependency_identity_blocks():
    first = dep("a")
    second = DependencyAdoption(
        project_id="p",
        component_id="copy",
        package_name="pkg_a",
        version="1.0.0",
        source_ref="source:copy",
        provenance_ref="prov:copy",
        license_expression="MIT",
        license_disposition=LicenseDisposition.APPROVED,
        source_sha256=SHA_A,
        review_ref="review:a",
    )
    # Normalize the first package name to the same PEP-503-style identity.
    first = replace(
        first,
        package_name="pkg-a",
        notice_required=False,
        notice_refs=(),
        distribution_obligations=(),
    )
    second = replace(second, package_name="pkg_a")
    decision = ProductComplianceGate(review_authority=Authority()).evaluate(
        project_id="p",
        dependencies=(first, second),
        scope_review_ref="scope",
    )
    assert any(item.startswith("duplicate:dependency-identity:") for item in decision.findings)


def test_new_evaluation_invalidates_old_decision_after_dependency_change():
    old_dep = dep(version="1.0.0")
    old = evaluate(old_dep)
    assert old.allowed is True

    new_dep = dep(version="1.0.1", sha=SHA_B)
    new = evaluate(new_dep)
    assert new.allowed is True
    assert old.allowed is False
    with pytest.raises(ProductComplianceError, match="decision:untrusted-origin"):
        ProductComplianceGate().require_release_allowed(old)


def test_input_fingerprint_tamper_invalidates_decision():
    decision = evaluate(dep())
    assert decision.allowed is True
    forged = replace(decision, input_fingerprint=SHA_B)
    assert forged.allowed is False


def test_positive_decision_fails_closed_in_fresh_process():
    decision = evaluate(dep())
    assert decision.allowed is True
    payload = base64.b64encode(pickle.dumps(decision)).decode("ascii")
    code = (
        "import base64,pickle,sys; "
        "decision=pickle.loads(base64.b64decode(sys.argv[1])); "
        "raise SystemExit(0 if decision.allowed is False else 9)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code, payload],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_packaging_compliance_reuses_canonical_notice_record():
    record = distribution_notice_record("platformdirs")
    evidence = build_pf10_notice_evidence(
        project_id="p",
        component_id="platformdirs",
        distribution_name="platformdirs",
    )
    assert evidence.package_name == record.package_name
    assert evidence.version == record.version
    assert evidence.notice_ref == record.notice_ref
