from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import pytest

from nika_core.packaging.release import verify_distributable_evidence

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
PRODUCT_VERSION = "0.0.2"
ARTIFACT_REFERENCE = "./dist/NikaCore-0.0.2-windows-x64.zip"
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
    "machine_readable_sbom_verified",
    "supply_chain_provenance_verified",
    "packaged_uia_keyboard_focus",
)
REQUIRED_FALSE_FIELDS = (
    "physical_windows_foundry_inference_proven",
    "human_tested",
    "nvda_verified",
    "production_release_ready",
)


def _write_bound_evidence(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    artifact = tmp_path / "NikaCore-0.0.2-windows-x64.zip"
    artifact.write_bytes(b"controlled exact distributable bytes")
    payload: dict[str, object] = {
        "schema_version": 4,
        "product_version": PRODUCT_VERSION,
        "commit_sha": SOURCE_SHA,
        "distributable_zip_path": ARTIFACT_REFERENCE,
        "distributable_zip_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "distributable_zip_size": artifact.stat().st_size,
        **{field: True for field in REQUIRED_TRUE_FIELDS},
        **{field: False for field in REQUIRED_FALSE_FIELDS},
    }
    evidence = tmp_path / "m12-prehuman-evidence.json"
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    return artifact, evidence, payload


def _verify(
    artifact: Path,
    evidence: Path,
    *,
    expected_product_version: str = PRODUCT_VERSION,
) -> tuple[str, ...]:
    return verify_distributable_evidence(
        artifact,
        evidence,
        source_sha=SOURCE_SHA,
        artifact_reference=ARTIFACT_REFERENCE,
        expected_product_version=expected_product_version,
    )


def test_current_m12_prehuman_evidence_contract_is_accepted(tmp_path: Path) -> None:
    artifact, evidence, _payload = _write_bound_evidence(tmp_path)

    assert _verify(artifact, evidence) == ()


@pytest.mark.parametrize("schema_value", (None, 2, 3, 5, "4", True))
def test_prehuman_verifier_requires_exact_schema_version(
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

    assert "distributable:schema-version" in findings


@pytest.mark.parametrize("field", REQUIRED_TRUE_FIELDS)
@pytest.mark.parametrize("unsafe_value", (False, 1, "true", None))
def test_prehuman_verifier_requires_each_automated_gate_exact_true(
    tmp_path: Path,
    field: str,
    unsafe_value: object,
) -> None:
    artifact, evidence, payload = _write_bound_evidence(tmp_path)
    if unsafe_value is None:
        payload.pop(field)
    else:
        payload[field] = unsafe_value
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    findings = _verify(artifact, evidence)

    assert f"distributable:required-true:{field}" in findings


@pytest.mark.parametrize("field", REQUIRED_FALSE_FIELDS)
@pytest.mark.parametrize("unsafe_value", (True, 0, "false", None))
def test_prehuman_verifier_requires_automation_only_truth_exact_false(
    tmp_path: Path,
    field: str,
    unsafe_value: object,
) -> None:
    artifact, evidence, payload = _write_bound_evidence(tmp_path)
    if unsafe_value is None:
        payload.pop(field)
    else:
        payload[field] = unsafe_value
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    findings = _verify(artifact, evidence)

    assert f"distributable:required-false:{field}" in findings


@pytest.mark.parametrize("unsafe_value", ("", " 0.0.2", "0.0.2\n", "x" * 129, True, None))
def test_prehuman_verifier_requires_bounded_product_version(
    tmp_path: Path,
    unsafe_value: object,
) -> None:
    artifact, evidence, payload = _write_bound_evidence(tmp_path)
    if unsafe_value is None:
        payload.pop("product_version")
    else:
        payload["product_version"] = unsafe_value
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    findings = _verify(artifact, evidence)

    assert "distributable:product-version" in findings


def test_prehuman_verifier_binds_product_version_to_trusted_release_identity(
    tmp_path: Path,
) -> None:
    artifact, evidence, payload = _write_bound_evidence(tmp_path)
    payload["product_version"] = "999.0"
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    findings = _verify(artifact, evidence)

    assert "distributable:product-version" in findings


@pytest.mark.parametrize(
    "unsafe_expected_version",
    ("", " 0.0.2", "0.0.2\n", "x" * 129, True, None),
)
def test_prehuman_verifier_rejects_noncanonical_trusted_product_version(
    tmp_path: Path,
    unsafe_expected_version: object,
) -> None:
    artifact, evidence, _payload = _write_bound_evidence(tmp_path)

    findings = _verify(
        artifact,
        evidence,
        expected_product_version=cast(str, unsafe_expected_version),
    )

    assert findings == ("distributable:expected-product-version-format",)


def test_v3_evidence_with_v4_sbom_claims_is_rejected(tmp_path: Path) -> None:
    artifact, evidence, payload = _write_bound_evidence(tmp_path)
    payload["schema_version"] = 3
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    findings = _verify(artifact, evidence)

    assert "distributable:schema-version" in findings


def test_prehuman_schema_does_not_silently_accept_unknown_fields(tmp_path: Path) -> None:
    artifact, evidence, payload = _write_bound_evidence(tmp_path)
    payload["future_unversioned_gate"] = True
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    findings = _verify(artifact, evidence)

    assert "distributable:evidence-keys" in findings
