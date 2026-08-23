from __future__ import annotations

import hashlib
import re
from importlib import metadata
from pathlib import Path

from nika_core.packaging.notices import (
    RUNTIME_DISTRIBUTIONS,
    _distribution_section,
    _sections,
    verify_third_party_notices,
)
from nika_core.product_compliance import PackagedDependencyEvidence

_PACKAGE_SEPARATOR_RE = re.compile(r"[-_.]+")


class RuntimeNoticesComplianceAuthority:
    """PF10 adapter over the canonical generated THIRD_PARTY_NOTICES.txt evidence."""

    def __init__(self, *, project_id: str, bundle_dir: Path) -> None:
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id must be non-empty text")
        self._project_id = project_id
        self._bundle_dir = Path(bundle_dir)

    def inventory(self, *, project_id: str) -> tuple[PackagedDependencyEvidence, ...]:
        self._require_project(project_id)
        findings = verify_third_party_notices(self._bundle_dir)
        if findings:
            raise RuntimeError(
                "third-party notices failed canonical verification: " + ", ".join(findings)
            )
        notice_file = self._bundle_dir / "THIRD_PARTY_NOTICES.txt"
        sections, duplicates = _sections(notice_file.read_text(encoding="utf-8"))
        if duplicates:
            raise RuntimeError("third-party notices contain duplicate sections")

        result: list[PackagedDependencyEvidence] = []
        for distribution_name in RUNTIME_DISTRIBUTIONS:
            try:
                dist = metadata.distribution(distribution_name)
            except metadata.PackageNotFoundError as exc:
                raise RuntimeError(
                    f"required runtime distribution is missing: {distribution_name}"
                ) from exc
            title, expected_body = _distribution_section(distribution_name, dist)
            body = sections.get(title)
            if body != expected_body:
                raise RuntimeError(f"notice section does not match installed metadata: {title}")
            package_name = dist.metadata.get("Name") or distribution_name
            canonical_name = _canonical_package_name(package_name)
            notice_ref = (
                "artifact:THIRD_PARTY_NOTICES.txt#"
                f"{canonical_name}@{dist.version}"
            )
            section_sha256 = hashlib.sha256(
                f"===== {title} =====\n{body}\n".encode("utf-8")
            ).hexdigest()
            result.append(
                PackagedDependencyEvidence(
                    package_name=package_name,
                    version=dist.version,
                    notice_ref=notice_ref,
                    notice_sha256=section_sha256,
                )
            )
        return tuple(result)

    def verify_notice(
        self,
        *,
        project_id: str,
        package: PackagedDependencyEvidence,
    ) -> bool:
        if project_id != self._project_id:
            return False
        try:
            inventory = self.inventory(project_id=project_id)
        except (LookupError, PermissionError, RuntimeError, TypeError, ValueError):
            return False
        return package in inventory

    def _require_project(self, project_id: str) -> None:
        if project_id != self._project_id:
            raise PermissionError("packaging compliance evidence belongs to another project")


def _canonical_package_name(value: str) -> str:
    return _PACKAGE_SEPARATOR_RE.sub("-", value).casefold()
