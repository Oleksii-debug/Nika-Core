from __future__ import annotations

from copy import deepcopy

from nika_core.packaging.notices import build_cyclonedx_sbom


def _supply_chain() -> dict[str, object]:
    return {
        "artifact": "NikaCore Windows base runtime",
        "policy": {"model_licenses_separate_from_engine": True},
        "declared_dependency_surface": [
            {
                "group": "base",
                "role": "base-declared",
                "name": "example-runtime",
                "requirement": "example-runtime>=1,<2",
                "specifier": ">=1,<2",
                "marker": None,
                "resolved_version": "1.2.3",
                "present_in_build_environment": True,
                "listed_in_bundle_runtime": True,
            }
        ],
        "bundle_runtime_distributions": [
            {
                "name": "example-runtime",
                "resolved_version": "1.2.3",
                "license": "MIT",
                "license_risk": "no-known-restrictive-token",
                "license_source": "package-metadata",
                "installer": "pip",
                "record_sha256": "a" * 64,
                "project_urls": ["https://example.invalid/runtime"],
            }
        ],
    }


def test_cyclonedx_sbom_binds_dependency_scope_move() -> None:
    """A dependency authority move must invalidate a previously generated SBOM.

    Moving the same resolved package from the base dependency surface into an allowed
    bundled extra changes release authority even though the package name/version and
    bytes are unchanged.  CycloneDX evidence must therefore carry enough Nika scope
    provenance for deterministic recomputation to reject the stale pre-move SBOM.
    """
    base = _supply_chain()
    moved = deepcopy(base)
    moved_entry = moved["declared_dependency_surface"][0]  # type: ignore[index]
    moved_entry["group"] = "gui"
    moved_entry["role"] = "base-bundle-extra"

    assert base["declared_dependency_surface"] != moved["declared_dependency_surface"]
    assert build_cyclonedx_sbom(base) != build_cyclonedx_sbom(moved), (
        "CycloneDX projection ignored dependency group/role; a stale SBOM survives "
        "an allowed dependency authority move"
    )
