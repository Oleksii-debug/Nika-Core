from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from nika_core.packaging.notices import verify_third_party_notices
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


def test_release_version_rejects_non_text_and_noncanonical_authority(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "nika-core"\nversion = 1\n',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="must be exact text"):
        project_version(tmp_path)

    pyproject.write_text(
        '[project]\nname = "nika-core"\nversion = " 1.0.0"\n',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="canonical text"):
        project_version(tmp_path)

    pyproject.write_text(
        '[project]\nname = "nika-core"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="canonical text"):
        resolve_release_version(tmp_path, "1.0.0 ")


def test_release_source_sha_requires_exact_full_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NIKA_SOURCE_SHA", raising=False)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    assert resolve_source_sha(SOURCE_SHA.upper()) == SOURCE_SHA
    with pytest.raises(ValueError, match="exact 40-character source SHA"):
        resolve_source_sha("deadbeef")
    with pytest.raises(ValueError, match="exact 40-character source SHA"):
        resolve_source_sha(f" {SOURCE_SHA}")
    with pytest.raises(ValueError, match="exact 40-character source SHA"):
        resolve_source_sha(None)


def test_release_source_sha_rejects_explicit_configured_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NIKA_SOURCE_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_SHA", "b" * 40)

    with pytest.raises(ValueError, match="conflicts with configured NIKA_SOURCE_SHA"):
        resolve_source_sha("c" * 40)

    assert resolve_source_sha("A" * 40) == "a" * 40


def test_release_source_sha_can_come_from_explicit_release_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NIKA_SOURCE_SHA", SOURCE_SHA)
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)
    assert resolve_source_sha(None) == SOURCE_SHA


def test_release_source_sha_falls_back_to_github_when_release_env_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NIKA_SOURCE_SHA", raising=False)
    monkeypatch.setenv("GITHUB_SHA", SOURCE_SHA.upper())

    assert resolve_source_sha(None) == SOURCE_SHA


def test_third_party_notice_verification_fails_closed(tmp_path: Path) -> None:
    assert verify_third_party_notices(tmp_path) == ("missing:THIRD_PARTY_NOTICES.txt",)

    notices = tmp_path / "THIRD_PARTY_NOTICES.txt"
    notices.write_text("Python runtime\n", encoding="utf-8")
    findings = verify_third_party_notices(tmp_path)
    assert "notices:pywebview" in findings
    assert "notices:pythonnet" in findings


def test_windows_plan_is_onedir_windowed_and_bundles_web_assets(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "nika_windows.py").write_text("pass\n", encoding="utf-8")
    web = tmp_path / "src" / "nika_core" / "ui" / "web"
    web.mkdir(parents=True)
    (web / "index.html").write_text("<main></main>", encoding="utf-8")
    (web / "app.js").write_text("console.log('Nika')", encoding="utf-8")
    (web / "styles.css").write_text("body {}", encoding="utf-8")
    plan = default_windows_plan(tmp_path)
    args = plan.pyinstaller_args()
    assert "--onedir" in args
    assert "--windowed" in args
    assert "--onefile" not in args
    assert "--add-data" in args
    assert str(tmp_path / "scripts" / "nika_windows.py") == args[0]


def test_windows_plan_rejects_path_like_reserved_and_invalid_bundle_names(
    tmp_path: Path,
) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "nika_windows.py").write_text("pass\n", encoding="utf-8")
    web = tmp_path / "src" / "nika_core" / "ui" / "web"
    web.mkdir(parents=True)
    (web / "index.html").write_text("<main></main>", encoding="utf-8")
    (web / "app.js").write_text("console.log('Nika')", encoding="utf-8")
    (web / "styles.css").write_text("body {}", encoding="utf-8")
    plan = default_windows_plan(tmp_path)

    invalid_names = (
        "../escape",
        "Nika/Core",
        r"Nika\Core",
        "CON",
        "COM1.txt",
        "LPT³.log",
        "NikaCore.",
        "bad\x01name",
        "a" * 256,
    )
    for invalid_name in invalid_names:
        invalid = replace(plan, name=invalid_name)
        with pytest.raises(ValueError):
            invalid.pyinstaller_args()
        with pytest.raises(ValueError):
            _ = invalid.bundle_dir

    with pytest.raises(TypeError, match="exact text"):
        _ = replace(plan, name=123).bundle_dir  # type: ignore[arg-type]


def test_windows_plan_accepts_unicode_single_component_bundle_name(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "nika_windows.py").write_text("pass\n", encoding="utf-8")
    web = tmp_path / "src" / "nika_core" / "ui" / "web"
    web.mkdir(parents=True)
    (web / "index.html").write_text("<main></main>", encoding="utf-8")
    (web / "app.js").write_text("console.log('Nika')", encoding="utf-8")
    (web / "styles.css").write_text("body {}", encoding="utf-8")
    plan = replace(default_windows_plan(tmp_path), name="Ніка Core")

    assert plan.bundle_dir == tmp_path / "dist" / "Ніка Core"
    args = plan.pyinstaller_args()
    assert args[args.index("--name") + 1] == "Ніка Core"


def test_windows_plan_rejects_behavioral_string_bundle_name(tmp_path: Path) -> None:
    class BehavioralName(str):
        def strip(self, *args: object, **kwargs: object) -> str:
            raise AssertionError("behavioral string must not execute")

    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "nika_windows.py").write_text("pass\n", encoding="utf-8")
    web = tmp_path / "src" / "nika_core" / "ui" / "web"
    web.mkdir(parents=True)
    (web / "index.html").write_text("<main></main>", encoding="utf-8")
    (web / "app.js").write_text("console.log('Nika')", encoding="utf-8")
    (web / "styles.css").write_text("body {}", encoding="utf-8")
    plan = replace(default_windows_plan(tmp_path), name=BehavioralName("NikaCore"))

    with pytest.raises(TypeError, match="exact text"):
        _ = plan.bundle_dir


def test_windows_plan_accepts_maximum_component_length(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "nika_windows.py").write_text("pass\n", encoding="utf-8")
    web = tmp_path / "src" / "nika_core" / "ui" / "web"
    web.mkdir(parents=True)
    (web / "index.html").write_text("<main></main>", encoding="utf-8")
    (web / "app.js").write_text("console.log('Nika')", encoding="utf-8")
    (web / "styles.css").write_text("body {}", encoding="utf-8")
    name = "a" * 255
    plan = replace(default_windows_plan(tmp_path), name=name)

    assert plan.bundle_dir == tmp_path / "dist" / name
    assert plan.pyinstaller_args()[plan.pyinstaller_args().index("--name") + 1] == name


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
