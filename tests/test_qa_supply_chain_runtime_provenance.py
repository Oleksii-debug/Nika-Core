from __future__ import annotations

from nika_core.packaging.notices import supply_chain_findings


def test_base_declared_dependency_cannot_disappear_from_runtime_inventory() -> None:
    """A required runtime dependency must not silently vanish from notices/SBOM scope."""
    payload: dict[str, object] = {
        "release_critical_declarations": [],
        "declared_dependency_surface": [
            {
                "group": "base",
                "role": "base-declared",
                "name": "httpx",
                "listed_in_bundle_runtime": False,
            }
        ],
        "bundle_runtime_distributions": [],
    }

    findings = supply_chain_findings(payload)

    assert findings, (
        "base-declared dependency omitted from bundle runtime inventory was accepted; "
        "release notices/SBOM can therefore be incomplete by construction"
    )


def test_runtime_component_requires_immutable_distribution_source_identity() -> None:
    """Mutable project URLs plus installed RECORD hashes are not source-artifact provenance."""
    payload: dict[str, object] = {
        "release_critical_declarations": [],
        "declared_dependency_surface": [],
        "bundle_runtime_distributions": [
            {
                "name": "example-runtime",
                "resolved_version": "1.2.3",
                "license": "MIT",
                "license_risk": "no-known-restrictive-token",
                "project_urls": ["https://pypi.org/project/example-runtime/"],
                "record_sha256": "a" * 64,
            }
        ],
    }

    findings = supply_chain_findings(payload)

    assert findings, (
        "runtime component with only mutable project URL metadata and an installed RECORD digest "
        "was accepted without immutable wheel/sdist/source-artifact provenance"
    )
