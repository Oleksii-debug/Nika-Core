from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from nika_core.packaging.notices import (
    SBOM_FILE,
    SUPPLY_CHAIN_FILE,
    build_cyclonedx_sbom,
    supply_chain_findings,
    verify_third_party_notices,
)
from nika_core.packaging.release import (
    build_release_manifest,
    verify_release_manifest,
    write_release_manifest,
)
from nika_core.packaging.windows import default_windows_plan
from nika_core.qa.release_gate import ReleaseGateEvidence, evaluate_release_gate
from scripts.m11_release import project_version, resolve_release_version, resolve_source_sha

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"


def test_release_manifest_is_deterministic_and_detects_tampering(tmp_path: Path) -> None:
    bundle = tmp_path / "NikaCore"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"binary")
    assets = bundle / "nika_core" / "ui" / "web"
    assets.mkdir(parents=True)
    (assets / "index.html").write_text("<main>Nika</main>", encoding="utf-8")

    first = build_release_manifest(
        bundle,
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
    )
    second = build_release_manifest(
        bundle,
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
    )
    assert first == second
    assert first.manifest_version == 2
    assert first.source_sha == SOURCE_SHA
    assert verify_release_manifest(bundle, first) == ()

    (assets / "index.html").write_text("tampered", encoding="utf-8")
    assert verify_release_manifest(bundle, first) == ("size:nika_core/ui/web/index.html",)


def test_written_release_manifest_records_source_identity(tmp_path: Path) -> None:
    bundle = tmp_path / "NikaCore"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"binary")
    manifest = build_release_manifest(
        bundle,
        product="NikaCore",
        version="1.2.3",
        source_sha=SOURCE_SHA,
    )
    target = write_release_manifest(bundle, manifest)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["manifest_version"] == 2
    assert payload["product"] == "NikaCore"
    assert payload["version"] == "1.2.3"
    assert payload["source_sha"] == SOURCE_SHA


def test_release_manifest_detects_unexpected_file(tmp_path: Path) -> None:
    bundle = tmp_path / "NikaCore"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"binary")
    manifest = build_release_manifest(
        bundle,
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
    )
    (bundle / "unexpected.dll").write_bytes(b"extra")
    assert verify_release_manifest(bundle, manifest) == ("unexpected:unexpected.dll",)


def test_release_version_comes_from_pyproject_and_mismatch_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "nika-core"\nversion = "9.8.7"\n',
        encoding="utf-8",
    )
    assert project_version(tmp_path) == "9.8.7"
    assert resolve_release_version(tmp_path, None) == "9.8.7"
    assert resolve_release_version(tmp_path, "9.8.7") == "9.8.7"
    with pytest.raises(ValueError, match="does not match pyproject version"):
        resolve_release_version(tmp_path, "0.0.2")


def test_release_source_sha_requires_exact_full_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NIKA_SOURCE_SHA", raising=False)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    assert resolve_source_sha(SOURCE_SHA.upper()) == SOURCE_SHA
    with pytest.raises(ValueError, match="exact 40-character source SHA"):
        resolve_source_sha("deadbeef")
    with pytest.raises(ValueError, match="exact 40-character source SHA"):
        resolve_source_sha(None)


def test_release_source_sha_can_come_from_explicit_release_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NIKA_SOURCE_SHA", SOURCE_SHA)
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)
    assert resolve_source_sha(None) == SOURCE_SHA


def test_third_party_notice_verification_fails_closed(tmp_path: Path) -> None:
    assert verify_third_party_notices(tmp_path) == ("missing:THIRD_PARTY_NOTICES.txt",)

    notices = tmp_path / "THIRD_PARTY_NOTICES.txt"
    notices.write_text("Python runtime\n", encoding="utf-8")
    findings = verify_third_party_notices(tmp_path)
    assert "notices:pywebview" in findings
    assert "notices:pythonnet" in findings
    assert f"missing:{SUPPLY_CHAIN_FILE}" in findings


def test_supply_chain_policy_fails_closed_on_unpinned_optional_and_license_risk() -> None:
    payload = {
        "release_critical_declarations": [
            {"name": "pyinstaller", "exact_pin": False},
        ],
        "declared_dependency_surface": [
            {
                "group": "browser",
                "role": "optional-not-bundled",
                "name": "playwright",
                "listed_in_bundle_runtime": True,
            }
        ],
        "bundle_runtime_distributions": [
            {
                "name": "example",
                "license_risk": "review-required",
                "project_urls": [],
                "record_sha256": None,
            }
        ],
    }
    findings = supply_chain_findings(payload)
    assert "supply-chain:unpinned-release-tool:pyinstaller" in findings
    assert "supply-chain:optional-bundled:browser:playwright" in findings
    assert "supply-chain:license-review:example" in findings
    assert "supply-chain:source-provenance:example" in findings
    assert "supply-chain:installed-record:example" in findings


def test_cyclonedx_sbom_records_exact_runtime_components_and_model_license_boundary() -> None:
    supply_chain = {
        "artifact": "NikaCore Windows base runtime",
        "policy": {"model_licenses_separate_from_engine": True},
        "bundle_runtime_distributions": [
            {
                "name": "example-runtime",
                "resolved_version": "1.2.3",
                "license": "MIT",
                "license_risk": "no-known-restrictive-token",
                "installer": "pip",
                "record_sha256": "a" * 64,
                "project_urls": ["https://example.invalid/runtime"],
            }
        ],
    }
    sbom = build_cyclonedx_sbom(supply_chain)
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.6"
    assert sbom["components"] == [
        {
            "type": "library",
            "name": "example-runtime",
            "version": "1.2.3",
            "purl": "pkg:pypi/example-runtime@1.2.3",
            "licenses": [{"license": {"name": "MIT"}}],
            "properties": [
                {"name": "nika:installer", "value": "pip"},
                {"name": "nika:record_sha256", "value": "a" * 64},
                {"name": "nika:license_risk", "value": "no-known-restrictive-token"},
                {"name": "nika:project_url", "value": "https://example.invalid/runtime"},
            ],
        }
    ]
    assert sbom["metadata"]["properties"] == [
        {"name": "nika:model_licenses_separate_from_engine", "value": "true"}
    ]


def test_release_critical_build_dependencies_are_exact_pinned() -> None:
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    assert data["build-system"]["requires"] == ["setuptools==84.0.0", "wheel==0.48.0"]
    assert data["project"]["optional-dependencies"]["qa"] == [
        "pip-audit==2.10.1",
        "pyinstaller==6.22.2",
    ]


def test_m11_publishes_machine_readable_supply_chain_evidence_and_sbom() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "m11-windows-release.yml").read_text(
        encoding="utf-8"
    )
    assert f"dist/NikaCore/{SUPPLY_CHAIN_FILE}" in workflow
    assert f"dist/NikaCore/{SBOM_FILE}" in workflow


def test_windows_plan_is_onedir_windowed_and_bundles_web_assets(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "nika_windows.py").write_text("pass\n", encoding="utf-8")
    web = tmp_path / "src" / "nika_core" / "ui" / "web"
    web.mkdir(parents=True)
    (web / "index.html").write_text("<main></main>", encoding="utf-8")
    plan = default_windows_plan(tmp_path)
    args = plan.pyinstaller_args()
    assert "--onedir" in args
    assert "--windowed" in args
    assert "--onefile" not in args
    assert "--add-data" in args
    assert str(tmp_path / "scripts" / "nika_windows.py") == args[0]


def test_release_gate_never_self_claims_human_nvda_verification() -> None:
    automated = ReleaseGateEvidence(
        core_ci_green=True,
        windows_package_built=True,
        package_smoke_passed=True,
        manifest_verified=True,
        third_party_notices_verified=True,
        recovery_drill_passed=True,
        packaged_uia_passed=True,
    )
    result = evaluate_release_gate(automated)
    assert result.release_candidate_ready is True
    assert result.production_release_ready is False
    assert result.stage == "PACKAGED"
    assert "NVDA verification by a human tester is missing" in result.blockers


def test_release_gate_requires_third_party_notices() -> None:
    incomplete = ReleaseGateEvidence(
        core_ci_green=True,
        windows_package_built=True,
        package_smoke_passed=True,
        manifest_verified=True,
        recovery_drill_passed=True,
        packaged_uia_passed=True,
    )
    result = evaluate_release_gate(incomplete)
    assert result.release_candidate_ready is False
    assert "Third-party release notices/license evidence is missing" in result.blockers


def test_release_gate_requires_human_test_before_nvda_verified() -> None:
    invalid = ReleaseGateEvidence(nvda_verified=True)
    result = evaluate_release_gate(invalid)
    assert result.production_release_ready is False
    assert "NVDA_VERIFIED cannot precede HUMAN_TESTED" in result.blockers


def test_release_gate_allows_final_release_only_with_complete_evidence() -> None:
    complete = ReleaseGateEvidence(
        core_ci_green=True,
        windows_package_built=True,
        package_smoke_passed=True,
        manifest_verified=True,
        third_party_notices_verified=True,
        recovery_drill_passed=True,
        packaged_uia_passed=True,
        human_tested=True,
        nvda_verified=True,
    )
    result = evaluate_release_gate(complete)
    assert result.release_candidate_ready is True
    assert result.production_release_ready is True
    assert result.stage == "NVDA_VERIFIED"
    assert result.blockers == ()
