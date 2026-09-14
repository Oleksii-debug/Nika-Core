from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    ROOT / ".github/workflows/m11-windows-release.yml",
    ROOT / ".github/workflows/m12-prehuman-release-gate.yml",
)
ACTUAL_RUNTIME_INSTALL = (
    "python -m pip install --ignore-installed --quiet --constraint "
    "$constraints --report "
)


@pytest.mark.parametrize("workflow_path", WORKFLOWS, ids=lambda path: path.name)
def test_packaged_release_provenance_comes_from_actual_runtime_install(
    workflow_path: Path,
) -> None:
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "pip install --dry-run" not in workflow
    assert ACTUAL_RUNTIME_INSTALL in workflow
    assert workflow.index(ACTUAL_RUNTIME_INSTALL) < workflow.index(
        "python scripts/m11_release.py"
    )


def test_m12_prehuman_v4_binds_sbom_claims_and_preserves_exact_final_zip_proof() -> None:
    workflow = (ROOT / ".github/workflows/m12-prehuman-release-gate.yml").read_text(
        encoding="utf-8"
    )

    assert "schema_version = 4" in workflow
    assert "machine_readable_sbom_verified = $true" in workflow
    assert "supply_chain_provenance_verified = $true" in workflow
    assert "Re-prove runtime from exact extracted final ZIP" in workflow
    assert "--product-version '${{ steps.release.outputs.version }}'" in workflow
    assert "dist/NikaCore/THIRD_PARTY_SUPPLY_CHAIN.json" in workflow
    assert "dist/NikaCore/THIRD_PARTY_SBOM.cdx.json" in workflow
