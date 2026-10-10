from __future__ import annotations

from copy import deepcopy

from nika_core.packaging.notices import build_cyclonedx_sbom, supply_chain_findings


def _supply_chain() -> dict[str, object]:
    return {
        "artifact": "NikaCore Windows base runtime",
        "policy": {"model_licenses_separate_from_engine": True},
        "release_critical_declarations": [],
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
        "bundle_native_artifacts": [],
    }


def test_runtime_distribution_requires_declared_root_lineage() -> None:
    """A clean-looking runtime package cannot be accepted without dependency lineage.

    A transitive runtime dependency does not have to be a direct pyproject declaration,
    but release evidence still has to bind it to an allowed declared root.  Otherwise an
    undeclared package can be inserted while carrying plausible license/URL/RECORD data.
    """
    payload = _supply_chain()
    payload["declared_dependency_surface"] = []

    findings = supply_chain_findings(payload)
    assert findings, (
        "runtime inventory accepted a package with no declared/root dependency lineage"
    )


def test_packaged_native_binary_requires_provenance_lineage() -> None:
    """Hashing a native binary is integrity evidence, not source provenance."""
    payload = _supply_chain()
    payload["bundle_native_artifacts"] = [
        {
            "path": "rogue-native.dll",
            "sha256": "b" * 64,
            "size": 4096,
            "origin_class": "packaged-native-runtime",
        }
    ]

    findings = supply_chain_findings(payload)
    assert findings, (
        "packaged native binary was accepted with only bytes/coarse origin and no "
        "distribution/tool/source lineage"
    )


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
