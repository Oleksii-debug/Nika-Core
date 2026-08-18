from __future__ import annotations

from pathlib import Path

from nika_core.packaging.release import build_release_manifest, verify_release_manifest
from nika_core.packaging.windows import default_windows_plan
from nika_core.qa.release_gate import ReleaseGateEvidence, evaluate_release_gate


def test_release_manifest_is_deterministic_and_detects_tampering(tmp_path: Path) -> None:
    bundle = tmp_path / "NikaCore"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"binary")
    assets = bundle / "nika_core" / "ui" / "web"
    assets.mkdir(parents=True)
    (assets / "index.html").write_text("<main>Nika</main>", encoding="utf-8")

    first = build_release_manifest(bundle, product="NikaCore", version="1.0.0")
    second = build_release_manifest(bundle, product="NikaCore", version="1.0.0")
    assert first == second
    assert verify_release_manifest(bundle, first) == ()

    (assets / "index.html").write_text("tampered", encoding="utf-8")
    assert verify_release_manifest(bundle, first) == ("size:nika_core/ui/web/index.html",)


def test_release_manifest_detects_unexpected_file(tmp_path: Path) -> None:
    bundle = tmp_path / "NikaCore"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"binary")
    manifest = build_release_manifest(bundle, product="NikaCore", version="1.0.0")
    (bundle / "unexpected.dll").write_bytes(b"extra")
    assert verify_release_manifest(bundle, manifest) == ("unexpected:unexpected.dll",)


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
        recovery_drill_passed=True,
        packaged_uia_passed=True,
    )
    result = evaluate_release_gate(automated)
    assert result.release_candidate_ready is True
    assert result.production_release_ready is False
    assert result.stage == "PACKAGED"
    assert "NVDA verification by a human tester is missing" in result.blockers


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
