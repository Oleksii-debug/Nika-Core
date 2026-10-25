from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts.m11_release import (
    project_version,
    prove_packaged_product_journey,
    resolve_source_sha,
)

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"


def _valid_proof() -> dict[str, object]:
    return {
        "route": "product_project",
        "spec_version": 1,
        "project_id": "project-1",
        "command_center_state_proven": True,
        "current_command_proven": True,
        "current_command_focus_proven": True,
        "restart_selection_integrity_proven": True,
        "bounded_projection_proven": True,
        "bridge_state_project_id": "project-1",
        "bridge_state_spec_version": 1,
        "bridge_state_status_count": 1,
        "bridge_state_decision_count": 0,
        "state": "active",
        "human_tested": False,
        "nvda_verified": False,
        "production_release_ready": False,
    }


def _run_proofs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
    second: str,
    existing_evidence: bytes | None = None,
) -> tuple[Path, int]:
    bundle = tmp_path / "NikaCore"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"stand-in: process is mocked")
    if existing_evidence is not None:
        (bundle / "pf11-packaged-product-journey.json").write_bytes(existing_evidence)
    evidence = iter((first, second))
    attempts: list[int] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["check"] is False
        assert kwargs["timeout"] == 60
        output = Path(argv[argv.index("--pf11-proof-output") + 1])
        output.write_text(next(evidence), encoding="utf-8")
        attempts.append(1)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)
    path = prove_packaged_product_journey(bundle, source_sha=SOURCE_SHA)
    return path, len(attempts)


def test_valid_packaged_proof_preserves_restart_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    valid = _valid_proof()
    first = json.dumps(valid)
    second = json.dumps(dict(reversed(list(valid.items()))))
    path, attempts = _run_proofs(tmp_path, monkeypatch, first, second)
    assert attempts == 2
    proof = json.loads(path.read_text(encoding="utf-8"))
    assert proof["source_sha"] == SOURCE_SHA
    assert proof["restart_replay_proven"] is True
    assert proof["human_tested"] is False
    assert proof["nvda_verified"] is False


def test_bool_int_drift_between_restart_proofs_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _valid_proof()
    second = {**first, "state": True}
    first["state"] = 1
    assert first == second  # The original Python dict equality silently accepted the drift.
    with pytest.raises(RuntimeError, match="restart replay changed"):
        _run_proofs(tmp_path, monkeypatch, json.dumps(first), json.dumps(second))


@pytest.mark.parametrize("field", ["spec_version", "bridge_state_spec_version"])
def test_boolean_spec_version_is_not_the_integer_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    invalid = {**_valid_proof(), field: True}
    encoded = json.dumps(invalid)
    with pytest.raises(RuntimeError, match="invalid route evidence"):
        _run_proofs(tmp_path, monkeypatch, encoded, encoded)


@pytest.mark.parametrize(
    "bad_evidence",
    [
        '{"route":"product_project","route":"product_project"}',
        json.dumps({**_valid_proof(), "state": float("nan")}),
        json.dumps({**_valid_proof(), "padding": "x" * (1024 * 1024)}),
    ],
    ids=["duplicate-key", "nonfinite", "oversized"],
)
def test_ambiguous_or_oversized_proof_fails_before_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_evidence: str
) -> None:
    with pytest.raises(RuntimeError, match="valid JSON evidence"):
        _run_proofs(tmp_path, monkeypatch, bad_evidence, bad_evidence)
    assert not (tmp_path / "NikaCore" / "pf11-packaged-product-journey.json").exists()


def test_explicit_empty_sha_does_not_fall_back_to_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NIKA_SOURCE_SHA", SOURCE_SHA)
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)
    with pytest.raises(ValueError, match="exact 40-character source SHA"):
        resolve_source_sha("")


def test_explicit_empty_source_environment_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NIKA_SOURCE_SHA", "")
    monkeypatch.setenv("GITHUB_SHA", SOURCE_SHA)
    with pytest.raises(ValueError, match="exact 40-character source SHA"):
        resolve_source_sha(None)


@pytest.mark.parametrize("toml_version", ["42", '" 1.2.3 "'])
def test_project_version_rejects_invalid_toml_carrier(
    tmp_path: Path, toml_version: str
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "nika-core"\nversion = ' + toml_version + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="must be a nonempty, unpadded string"):
        project_version(tmp_path)


def test_failed_fsync_preserves_previous_pf11_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    valid = json.dumps(_valid_proof())
    previous = b"previous validated artifact"

    def fail_fsync(_: int) -> None:
        raise OSError("simulated proof write interruption")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="simulated proof write interruption"):
        _run_proofs(tmp_path, monkeypatch, valid, valid, existing_evidence=previous)
    bundle = tmp_path / "NikaCore"
    assert (bundle / "pf11-packaged-product-journey.json").read_bytes() == previous
    assert not tuple(bundle.glob(".pf11-proof-*.tmp"))


@pytest.mark.parametrize(
    "field",
    [
        "current_command_proven",
        "current_command_focus_proven",
        "restart_selection_integrity_proven",
    ],
)
@pytest.mark.parametrize("value", [False, None])
def test_missing_packaged_command_or_restart_evidence_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: bool | None,
) -> None:
    invalid = {**_valid_proof(), field: value}
    payload = json.dumps(invalid)
    with pytest.raises(RuntimeError, match="invalid route evidence"):
        _run_proofs(tmp_path, monkeypatch, payload, payload)


@pytest.mark.parametrize("state", [None, False, 1, "", "   "])
def test_nontext_or_blank_product_state_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: object
) -> None:
    invalid = {**_valid_proof(), "state": state}
    payload = json.dumps(invalid)
    with pytest.raises(RuntimeError, match="invalid route evidence"):
        _run_proofs(tmp_path, monkeypatch, payload, payload)
