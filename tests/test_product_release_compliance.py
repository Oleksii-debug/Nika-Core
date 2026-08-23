from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

import nika_core.product_release_compliance as release_module
from nika_core.product_compliance import (
    DependencyAdoption,
    DistributionObligationEvidence,
    LicenseDisposition,
    ProductComplianceError,
)
from nika_core.product_release_compliance import (
    ProductReleaseComplianceGate,
    ReleaseComplianceDecision,
    ReleaseComplianceSnapshot,
    ReleaseDependency,
    ReleaseNoticeEvidence,
    build_verified_notice_bundle,
)

_PROJECT = "project-1"
_PROJECT_SOURCE_SHA = "c" * 64
_ARTIFACT_SHA = "b" * 64


class _ReviewAuthority:
    def verify(
        self,
        *,
        project_id: str,
        evidence_ref: str,
        purpose: str,
    ) -> bool:
        return project_id == _PROJECT and evidence_ref == f"review:{purpose}"


def _adoption(
    component_id: str,
    *,
    package_name: str | None = None,
    version: str = "1.0.0",
    license_expression: str = "MIT",
    obligations: tuple[str, ...] = ("retain-license",),
) -> DependencyAdoption:
    package = package_name or component_id
    notice_ref = f"artifact:THIRD_PARTY_NOTICES.txt#{component_id}"
    return DependencyAdoption(
        project_id=_PROJECT,
        component_id=component_id,
        package_name=package,
        version=version,
        source_ref=f"registry:pypi:{package}:{version}",
        provenance_ref=f"provenance:{component_id}:{version}",
        license_expression=license_expression,
        license_disposition=LicenseDisposition.APPROVED,
        distribution_obligations=obligations,
        notice_required=True,
        notice_refs=(notice_ref,),
        review_ref=f"review:license-disposition:{component_id}",
    )


def _dependency(
    component_id: str,
    *,
    package_name: str | None = None,
    version: str = "1.0.0",
    source_sha: str | None = None,
    license_expression: str = "MIT",
    requires: tuple[str, ...] = (),
    obligations: tuple[str, ...] = ("retain-license",),
) -> ReleaseDependency:
    return ReleaseDependency(
        adoption=_adoption(
            component_id,
            package_name=package_name,
            version=version,
            license_expression=license_expression,
            obligations=obligations,
        ),
        source_sha256=source_sha or hashlib.sha256(component_id.encode()).hexdigest(),
        requires_component_ids=requires,
    )


def _write_bundle(tmp_path: Path, text: str = "verified notice bundle\n") -> str:
    target = tmp_path / "THIRD_PARTY_NOTICES.txt"
    target.write_text(text, encoding="utf-8")
    return hashlib.sha256(target.read_bytes()).hexdigest()


def _snapshot(
    tmp_path: Path,
    *,
    dependencies: tuple[ReleaseDependency, ...] | None = None,
    obligations: tuple[DistributionObligationEvidence, ...] | None = None,
    notices: tuple[ReleaseNoticeEvidence, ...] | None = None,
    notice_hash: str | None = None,
) -> ReleaseComplianceSnapshot:
    deps = dependencies or (
        _dependency("root", requires=("leaf",)),
        _dependency("leaf"),
    )
    obligation_rows = obligations
    if obligation_rows is None:
        obligation_rows = tuple(
            DistributionObligationEvidence(
                project_id=_PROJECT,
                component_id=item.adoption.component_id,
                obligation=obligation,
                fulfillment_ref=item.adoption.notice_refs[0],
            )
            for item in deps
            for obligation in item.adoption.distribution_obligations
        )
    notice_rows = notices
    if notice_rows is None:
        notice_rows = tuple(
            ReleaseNoticeEvidence(
                project_id=_PROJECT,
                component_id=item.adoption.component_id,
                notice_ref=item.adoption.notice_refs[0],
                package_name=item.adoption.package_name,
                version=item.adoption.version,
            )
            for item in deps
        )
    return ReleaseComplianceSnapshot(
        project_id=_PROJECT,
        release_id="delivery-1",
        project_source_ref="git:Oleksii-debug/Nika-Core@exact",
        project_source_sha256=_PROJECT_SOURCE_SHA,
        artifact_ref="artifact:nika-release:delivery-1",
        artifact_sha256=_ARTIFACT_SHA,
        notice_bundle_sha256=notice_hash or _write_bundle(tmp_path),
        dependencies=deps,
        obligation_evidence=obligation_rows,
        notice_evidence=notice_rows,
    )


def _verified_packaging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release_module, "verify_third_party_notices", lambda _path: ())


def test_exact_release_gate_issues_current_snapshot_delivery_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _verified_packaging(monkeypatch)
    snapshot = _snapshot(tmp_path)
    gate = ProductReleaseComplianceGate(review_authority=_ReviewAuthority())

    decision = gate.evaluate(snapshot, bundle_dir=tmp_path)
    assert decision.allowed is True
    assert decision.findings == ()
    assert decision.snapshot_digest == snapshot.digest

    grant = gate.require_release_allowed(decision, snapshot, bundle_dir=tmp_path)
    assert grant.allowed is True
    assert grant.project_id == snapshot.project_id
    assert grant.release_id == snapshot.release_id
    assert grant.artifact_ref == snapshot.artifact_ref
    assert f"pf10:snapshot:{snapshot.digest}" in grant.evidence_refs
    assert f"artifact:sha256:{snapshot.artifact_sha256}" in grant.evidence_refs


def test_default_release_gate_cannot_turn_caller_review_strings_into_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _verified_packaging(monkeypatch)
    decision = ProductReleaseComplianceGate().evaluate(_snapshot(tmp_path), bundle_dir=tmp_path)
    assert decision.allowed is False
    assert any("untrusted-review-authority" in finding for finding in decision.findings)


def test_dependency_source_requires_exact_immutable_digest() -> None:
    with pytest.raises(ProductComplianceError, match="exact SHA-256"):
        ReleaseDependency(adoption=_adoption("component-a"), source_sha256="latest")


def test_duplicate_dependency_identity_and_package_conflict_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _verified_packaging(monkeypatch)
    same_sha = "a" * 64
    duplicate = (
        _dependency("a", package_name="Same_Pkg", source_sha=same_sha),
        _dependency("b", package_name="same-pkg", source_sha=same_sha),
    )
    decision = ProductReleaseComplianceGate(review_authority=_ReviewAuthority()).evaluate(
        _snapshot(tmp_path, dependencies=duplicate),
        bundle_dir=tmp_path,
    )
    assert decision.allowed is False
    assert any(item.startswith("duplicate:dependency-identity:same-pkg") for item in decision.findings)
    assert "duplicate:dependency-package:same-pkg" in decision.findings

    conflicting = (
        _dependency("a", package_name="same.pkg", version="1.0.0", source_sha="a" * 64),
        _dependency("b", package_name="same-pkg", version="2.0.0", source_sha="b" * 64),
    )
    conflict = ProductReleaseComplianceGate(review_authority=_ReviewAuthority()).evaluate(
        _snapshot(tmp_path, dependencies=conflicting),
        bundle_dir=tmp_path,
    )
    assert "conflict:dependency-package:same-pkg" in conflict.findings


def test_unknown_license_and_missing_transitive_dependency_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _verified_packaging(monkeypatch)
    deps = (
        _dependency(
            "root",
            license_expression="NOASSERTION",
            requires=("missing-transitive",),
        ),
    )
    decision = ProductReleaseComplianceGate(review_authority=_ReviewAuthority()).evaluate(
        _snapshot(tmp_path, dependencies=deps),
        bundle_dir=tmp_path,
    )
    assert "license:unknown:root" in decision.findings
    assert "transitive-dependency:missing:root:missing-transitive" in decision.findings


def test_transitive_distribution_obligation_omission_remains_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _verified_packaging(monkeypatch)
    deps = (
        _dependency("root", requires=("leaf",)),
        _dependency("leaf"),
    )
    only_root = (
        DistributionObligationEvidence(
            project_id=_PROJECT,
            component_id="root",
            obligation="retain-license",
            fulfillment_ref=deps[0].adoption.notice_refs[0],
        ),
    )
    decision = ProductReleaseComplianceGate(review_authority=_ReviewAuthority()).evaluate(
        _snapshot(tmp_path, dependencies=deps, obligations=only_root),
        bundle_dir=tmp_path,
    )
    assert "distribution-obligation:unfulfilled:leaf:retain-license" in decision.findings


def test_missing_or_orphan_notice_evidence_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _verified_packaging(monkeypatch)
    dep = _dependency("root")
    orphan = ReleaseNoticeEvidence(
        project_id=_PROJECT,
        component_id="ghost",
        notice_ref="artifact:THIRD_PARTY_NOTICES.txt#ghost",
        package_name="ghost",
        version="1.0.0",
    )
    decision = ProductReleaseComplianceGate(review_authority=_ReviewAuthority()).evaluate(
        _snapshot(tmp_path, dependencies=(dep,), notices=(orphan,)),
        bundle_dir=tmp_path,
    )
    assert any(item.startswith("orphan:notice:ghost") for item in decision.findings)
    assert any(item.startswith("notice:evidence-missing:root") for item in decision.findings)


def test_stale_decision_after_dependency_change_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _verified_packaging(monkeypatch)
    snapshot = _snapshot(tmp_path)
    gate = ProductReleaseComplianceGate(review_authority=_ReviewAuthority())
    decision = gate.evaluate(snapshot, bundle_dir=tmp_path)
    assert decision.allowed is True

    changed_root = replace(snapshot.dependencies[0], source_sha256="d" * 64)
    changed = replace(snapshot, dependencies=(changed_root, *snapshot.dependencies[1:]))
    assert changed.digest != snapshot.digest
    with pytest.raises(ProductComplianceError, match="stale-or-wrong-release-snapshot"):
        gate.require_release_allowed(decision, changed, bundle_dir=tmp_path)


def test_decision_tamper_cross_context_replay_and_fabrication_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _verified_packaging(monkeypatch)
    snapshot = _snapshot(tmp_path)
    gate = ProductReleaseComplianceGate(review_authority=_ReviewAuthority())
    decision = gate.evaluate(snapshot, bundle_dir=tmp_path)
    assert decision.allowed is True

    tampered = replace(decision, release_id="delivery-other")
    assert tampered.allowed is False
    with pytest.raises(ProductComplianceError, match="untrusted-origin"):
        gate.require_release_allowed(tampered, snapshot, bundle_dir=tmp_path)

    fabricated = ReleaseComplianceDecision(
        project_id=_PROJECT,
        release_id=snapshot.release_id,
        artifact_ref=snapshot.artifact_ref,
        snapshot_digest=snapshot.digest,
        allowed=True,
        findings=(),
        evidence_refs=(),
    )
    assert fabricated.allowed is False
    with pytest.raises(ProductComplianceError, match="untrusted-origin"):
        gate.require_release_allowed(fabricated, snapshot, bundle_dir=tmp_path)


def test_packaging_notice_drift_invalidates_previously_allowed_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _verified_packaging(monkeypatch)
    snapshot = _snapshot(tmp_path)
    gate = ProductReleaseComplianceGate(review_authority=_ReviewAuthority())
    decision = gate.evaluate(snapshot, bundle_dir=tmp_path)
    assert decision.allowed is True

    (tmp_path / "THIRD_PARTY_NOTICES.txt").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ProductComplianceError, match="notices-digest-mismatch"):
        gate.require_release_allowed(decision, snapshot, bundle_dir=tmp_path)


def test_restart_requires_fresh_authoritative_evaluation_not_serialized_positive_bit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _verified_packaging(monkeypatch)
    snapshot = _snapshot(tmp_path)
    first_gate = ProductReleaseComplianceGate(review_authority=_ReviewAuthority())
    first = first_gate.evaluate(snapshot, bundle_dir=tmp_path)
    assert first.allowed is True

    persisted_without_process_proof = ReleaseComplianceDecision(
        project_id=first.project_id,
        release_id=first.release_id,
        artifact_ref=first.artifact_ref,
        snapshot_digest=first.snapshot_digest,
        allowed=True,
        findings=(),
        evidence_refs=first.evidence_refs,
    )
    assert persisted_without_process_proof.allowed is False

    restarted_gate = ProductReleaseComplianceGate(review_authority=_ReviewAuthority())
    refreshed = restarted_gate.evaluate(snapshot, bundle_dir=tmp_path)
    assert refreshed.allowed is True
    assert restarted_gate.require_release_allowed(
        refreshed,
        snapshot,
        bundle_dir=tmp_path,
    ).allowed


def test_packaging_integration_reuses_canonical_notice_generator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = tmp_path / "THIRD_PARTY_NOTICES.txt"

    def _build(directory: Path) -> Path:
        assert directory == tmp_path
        generated.write_text("canonical generated notices\n", encoding="utf-8")
        return generated

    monkeypatch.setattr(release_module, "build_third_party_notices", _build)
    monkeypatch.setattr(release_module, "verify_third_party_notices", lambda _path: ())
    expected = hashlib.sha256(generated.read_bytes()).hexdigest() if generated.exists() else None
    actual = build_verified_notice_bundle(tmp_path)
    assert actual == hashlib.sha256(generated.read_bytes()).hexdigest()
    assert expected is None


def test_packaging_generator_verification_failure_blocks_release_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "THIRD_PARTY_NOTICES.txt"

    def _build(_directory: Path) -> Path:
        target.write_text("bad notices\n", encoding="utf-8")
        return target

    monkeypatch.setattr(release_module, "build_third_party_notices", _build)
    monkeypatch.setattr(
        release_module,
        "verify_third_party_notices",
        lambda _path: ("notices:runtime",),
    )
    with pytest.raises(ProductComplianceError, match="did not verify"):
        build_verified_notice_bundle(tmp_path)
