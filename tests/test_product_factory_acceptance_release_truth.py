from __future__ import annotations

from pathlib import Path

from nika_core.packaging.notices import (
    RUNTIME_DISTRIBUTIONS,
    verify_third_party_notices,
)

ROOT = Path(__file__).resolve().parents[1]


def test_pf10_notices_verifier_requires_license_evidence_not_only_package_names(
    tmp_path,
) -> None:
    """A names-only notice file must not be accepted as license provenance."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    names_only = ["Nika Core third-party notices", "Python runtime"]
    names_only.extend(RUNTIME_DISTRIBUTIONS)
    (bundle / "THIRD_PARTY_NOTICES.txt").write_text(
        "\n".join(names_only) + "\n",
        encoding="utf-8",
    )

    findings = verify_third_party_notices(bundle)
    assert findings


def test_pf11_windows_composition_root_routes_product_commands_to_product_factory() -> None:
    """The packaged command field must actually reach ProductProject routing."""
    source = (ROOT / "scripts" / "nika_windows.py").read_text(encoding="utf-8")

    assert "route_command" in source
    assert "ProductProjectCommandService" in source
    assert '"task.create": backend.create_task' not in source


def test_pf11_m12_records_digest_of_the_actual_distributable_zip() -> None:
    """The evidence JSON must bind the exact outer ZIP that is uploaded."""
    workflow = (
        ROOT / ".github" / "workflows" / "m12-prehuman-release-gate.yml"
    ).read_text(encoding="utf-8")
    lowered = workflow.casefold()

    compress_index = lowered.index("compress-archive")
    hash_index = lowered.index("get-filehash")
    evidence_index = lowered.index("record automated pre-human evidence")
    upload_index = lowered.index("upload exact pre-human candidate evidence")

    assert compress_index < hash_index < evidence_index < upload_index
    assert "id: distributable" in lowered
    assert "-algorithm sha256" in lowered
    assert "^[0-9a-f]{64}$" in lowered
    assert "zip_path=$zippath" in lowered
    assert "zip_sha256=$zipsha256" in lowered
    assert "distributable_zip_path" in lowered
    assert "steps.distributable.outputs.zip_path" in lowered
    assert "distributable_zip_sha256" in lowered
    assert "steps.distributable.outputs.zip_sha256" in lowered


def test_pf11_pre_human_evidence_cannot_claim_production_release_readiness() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "m12-prehuman-release-gate.yml"
    ).read_text(encoding="utf-8")
    compact = "".join(workflow.casefold().split())

    assert "human_tested=$false" in compact
    assert "nvda_verified=$false" in compact
    assert "production_release_ready=$false" in compact
