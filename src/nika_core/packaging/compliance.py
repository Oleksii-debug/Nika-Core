from __future__ import annotations

from nika_core.product_compliance import PackagingNoticeEvidence

from .notices import distribution_notice_record


def build_pf10_notice_evidence(
    *,
    project_id: str,
    component_id: str,
    distribution_name: str,
) -> PackagingNoticeEvidence:
    """Bind PF10 notice evidence to the canonical generated notice section."""
    record = distribution_notice_record(distribution_name)
    return PackagingNoticeEvidence(
        project_id=project_id,
        component_id=component_id,
        package_name=record.package_name,
        version=record.version,
        notice_ref=record.notice_ref,
    )
