from __future__ import annotations

from pathlib import Path

from nika_core.packaging.notices import (
    third_party_notice_section_refs,
    verify_third_party_notices,
)
from nika_core.product_compliance import NoticeEvidence, ProductComplianceSnapshot


def packaging_notice_evidence(
    bundle_dir: Path,
    *,
    snapshot: ProductComplianceSnapshot,
) -> tuple[NoticeEvidence, ...]:
    """Bind PF10 declared notice refs to the exact generated M11 notices artifact."""

    findings = verify_third_party_notices(bundle_dir)
    if findings:
        raise RuntimeError(f"third-party notice verification failed: {findings}")

    available = frozenset(third_party_notice_section_refs(bundle_dir))
    evidence: list[NoticeEvidence] = []
    for dependency in snapshot.dependencies:
        if dependency.project_id != snapshot.project_id:
            continue
        for notice_ref in dependency.notice_refs:
            if notice_ref not in available:
                raise RuntimeError(
                    "PF10 dependency notice_ref is absent from generated notices: "
                    f"{dependency.component_id}:{notice_ref}"
                )
            evidence.append(
                NoticeEvidence(
                    project_id=snapshot.project_id,
                    component_id=dependency.component_id,
                    notice_ref=notice_ref,
                    artifact_ref="artifact:THIRD_PARTY_NOTICES.txt",
                )
            )
    return tuple(evidence)


def verify_pf10_packaging_snapshot(
    bundle_dir: Path,
    *,
    snapshot: ProductComplianceSnapshot,
) -> tuple[str, ...]:
    """Verify exact PF10 notice evidence against M11's generated notice file."""

    try:
        generated = packaging_notice_evidence(bundle_dir, snapshot=snapshot)
    except RuntimeError as exc:
        return (f"pf10-packaging:{exc}",)

    expected = {
        (item.component_id, item.notice_ref, item.artifact_ref)
        for item in snapshot.notice_evidence
    }
    actual = {
        (item.component_id, item.notice_ref, item.artifact_ref)
        for item in generated
    }
    findings: list[str] = []
    for missing in sorted(actual - expected):
        findings.append(
            f"pf10-packaging:missing-notice-evidence:{missing[0]}:{missing[1]}"
        )
    for orphan in sorted(expected - actual):
        findings.append(
            f"pf10-packaging:orphan-notice-evidence:{orphan[0]}:{orphan[1]}"
        )
    return tuple(findings)
