from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from nika_core.packaging.release import verify_distributable_evidence

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
ARTIFACT_REFERENCE = "./dist/NikaCore-0.0.2-windows-x64.zip"


def _write_bound_evidence(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    artifact = tmp_path / "NikaCore-0.0.2-windows-x64.zip"
    artifact.write_bytes(b"controlled exact distributable bytes")
    payload: dict[str, object] = {
        "schema_version": 3,
        "product_version": "0.0.2",
        "commit_sha": SOURCE_SHA,
        "distributable_zip_path": ARTIFACT_REFERENCE,
        "distributable_zip_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "distributable_zip_size": artifact.stat().st_size,
        "release_manifest_source_sha_bound": True,
        "exact_checkout_sha_verified": True,
        "core_ci_equivalent": True,
        "full_test_suite": True,
        "runtime_restart_recovery": True,
        "memory_scheduler_resource_regressions": True,
        "model_mock_nollm_regressions": True,
        "deterministic_brain_regressions": True,
        "foundry_local_adapter_regressions": True,
        "plugin_workspace_regressions": True,
        "security_sandbox_regressions": True,
        "integrated_ubuntu": True,
        "integrated_windows": True,
        "browser_semantic_proof": True,
        "windows_uia_semantic_proof": True,
        "windows_package_built": True,
        "manifest_verified": True,
        "third_party_notices_verified": True,
        "packaged_uia_keyboard_focus": True,
        "physical_windows_foundry_inference_proven": False,
        "human_tested": False,
        "nvda_verified": False,
        "production_release_ready": False,
    }
    evidence = tmp_path / "m12-prehuman-evidence.json"
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    return artifact, evidence, payload


def _verify(artifact: Path, evidence: Path) -> tuple[str, ...]:
    return verify_distributable_evidence(
        artifact,
        evidence,
        source_sha=SOURCE_SHA,
        artifact_reference=ARTIFACT_REFERENCE,
    )


def test_controlled_current_m12_prehuman_evidence_contract_is_accepted(tmp_path: Path) -> None:
    artifact, evidence, _payload = _write_bound_evidence(tmp_path)

    assert _verify(artifact, evidence) == ()


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    (
        ("human_tested", True),
        ("nvda_verified", True),
        ("production_release_ready", True),
    ),
)
def test_prehuman_verifier_rejects_forbidden_automated_human_release_truth(
    tmp_path: Path,
    field: str,
    unsafe_value: object,
) -> None:
    artifact, evidence, payload = _write_bound_evidence(tmp_path)
    payload[field] = unsafe_value
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    findings = _verify(artifact, evidence)

    assert findings, f"pre-human evidence must fail closed when {field}=true"


@pytest.mark.parametrize("schema_value", (None, 2, 4, "3", True))
def test_prehuman_verifier_rejects_missing_or_wrong_schema_version(
    tmp_path: Path,
    schema_value: object,
) -> None:
    artifact, evidence, payload = _write_bound_evidence(tmp_path)
    if schema_value is None:
        payload.pop("schema_version")
    else:
        payload["schema_version"] = schema_value
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    findings = _verify(artifact, evidence)

    assert findings, "pre-human evidence schema must be exact and fail closed"
