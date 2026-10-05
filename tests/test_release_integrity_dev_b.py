from __future__ import annotations

import hashlib
import json
from pathlib import Path

from nika_core.packaging.notices import RUNTIME_DISTRIBUTIONS, verify_third_party_notices
from nika_core.packaging.release import verify_distributable_evidence

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
PRODUCT_VERSION = "1.0.0"
REQUIRED_TRUE_FIELDS = (
    "release_manifest_source_sha_bound",
    "exact_checkout_sha_verified",
    "core_ci_equivalent",
    "full_test_suite",
    "runtime_restart_recovery",
    "memory_scheduler_resource_regressions",
    "model_mock_nollm_regressions",
    "deterministic_brain_regressions",
    "foundry_local_adapter_regressions",
    "plugin_workspace_regressions",
    "security_sandbox_regressions",
    "integrated_ubuntu",
    "integrated_windows",
    "browser_semantic_proof",
    "windows_uia_semantic_proof",
    "windows_package_built",
    "manifest_verified",
    "third_party_notices_verified",
    "packaged_uia_keyboard_focus",
)
REQUIRED_FALSE_FIELDS = (
    "physical_windows_foundry_inference_proven",
    "human_tested",
    "nvda_verified",
    "production_release_ready",
)


def test_names_only_notices_fail_closed(tmp_path: Path) -> None:
    names_only = ["Nika Core third-party notices", "Python runtime", *RUNTIME_DISTRIBUTIONS]
    (tmp_path / "THIRD_PARTY_NOTICES.txt").write_text(
        "\n".join(names_only) + "\n", encoding="utf-8"
    )
    assert verify_third_party_notices(tmp_path)


def _write_evidence(path: Path, artifact: Path, *, source_sha: str = SOURCE_SHA) -> None:
    payload = {
        "schema_version": 3,
        "product_version": PRODUCT_VERSION,
        "commit_sha": source_sha,
        "distributable_zip_path": "./dist/NikaCore-1.0.0-windows-x64.zip",
        "distributable_zip_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "distributable_zip_size": artifact.stat().st_size,
        **{field: True for field in REQUIRED_TRUE_FIELDS},
        **{field: False for field in REQUIRED_FALSE_FIELDS},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_distributable_evidence_exact_binding_and_tamper(tmp_path: Path) -> None:
    artifact = tmp_path / "NikaCore-1.0.0-windows-x64.zip"
    artifact.write_bytes(b"candidate")
    evidence = tmp_path / "evidence.json"
    reference = "./dist/NikaCore-1.0.0-windows-x64.zip"
    _write_evidence(evidence, artifact)
    assert verify_distributable_evidence(
        artifact,
        evidence,
        source_sha=SOURCE_SHA,
        artifact_reference=reference,
        expected_product_version=PRODUCT_VERSION,
    ) == ()

    artifact.write_bytes(b"tampered-candidate")
    findings = verify_distributable_evidence(
        artifact,
        evidence,
        source_sha=SOURCE_SHA,
        artifact_reference=reference,
        expected_product_version=PRODUCT_VERSION,
    )
    assert "distributable:size" in findings
    assert "distributable:sha256" in findings


def test_distributable_evidence_rejects_stale_source_and_path(tmp_path: Path) -> None:
    artifact = tmp_path / "NikaCore-1.0.0-windows-x64.zip"
    artifact.write_bytes(b"candidate")
    evidence = tmp_path / "evidence.json"
    _write_evidence(evidence, artifact, source_sha="f" * 40)
    findings = verify_distributable_evidence(
        artifact,
        evidence,
        source_sha=SOURCE_SHA,
        artifact_reference="./dist/NikaCore-1.0.0-windows-x64.zip",
        expected_product_version=PRODUCT_VERSION,
    )
    assert findings == ("distributable:source-sha",)

    _write_evidence(evidence, artifact)
    findings = verify_distributable_evidence(
        artifact,
        evidence,
        source_sha=SOURCE_SHA,
        artifact_reference="./dist/superseding.zip",
        expected_product_version=PRODUCT_VERSION,
    )
    assert findings == ("distributable:path",)
