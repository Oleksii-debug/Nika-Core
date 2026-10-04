from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts.m11_release import prove_packaged_voice_runtime

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"


def _valid_voice_proof() -> dict[str, object]:
    return {
        "schema": "nika.packaged-voice-runtime-proof:v1",
        "numpy_imported": True,
        "sherpa_onnx_imported": True,
        "sherpa_native_imported": True,
        "sounddevice_imported": True,
        "sounddevice_data_proven": True,
        "microphone_opened": False,
        "model_loaded": False,
        "human_tested": False,
        "nvda_verified": False,
        "production_release_ready": False,
    }


def _execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_bytes: bytes | None,
    *,
    previous_evidence: bytes | None = None,
) -> tuple[Path, list[list[str]]]:
    bundle = tmp_path / "NikaCore"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"mocked executable")
    target = bundle / "packaged-voice-runtime-proof.json"
    if previous_evidence is not None:
        target.write_bytes(previous_evidence)
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert kwargs["check"] is False
        assert kwargs["timeout"] == 30
        if output_bytes is not None:
            Path(argv[argv.index("--voice-runtime-proof-output") + 1]).write_bytes(
                output_bytes
            )
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)
    return bundle, calls


def test_valid_voice_proof_publishes_source_bound_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    encoded = json.dumps(_valid_voice_proof()).encode("utf-8")
    bundle, calls = _execute(tmp_path, monkeypatch, encoded)
    target = prove_packaged_voice_runtime(bundle, source_sha=SOURCE_SHA)
    proof = json.loads(target.read_text(encoding="utf-8"))

    assert len(calls) == 1
    assert proof["source_sha"] == SOURCE_SHA
    assert proof["packaged_executable_proven"] is True
    assert proof["sounddevice_data_proven"] is True
    assert proof["human_tested"] is False
    assert proof["nvda_verified"] is False
    assert not tuple(bundle.glob(".voice-proof-*.tmp"))


@pytest.mark.parametrize(
    ("corruption", "encoded"),
    [
        (
            "duplicate",
            (json.dumps(_valid_voice_proof())[:-1] + ', "schema": '
             '"nika.packaged-voice-runtime-proof:v1"}').encode("utf-8"),
        ),
        (
            "nonfinite",
            json.dumps({**_valid_voice_proof(), "extra": float("nan")}).encode("utf-8"),
        ),
        ("oversized", json.dumps(
            {**_valid_voice_proof(), "extra": "x" * (1024 * 1024)}
        ).encode("utf-8")),
        ("invalid-utf8", bytes([255])),
        ("truncated", b'{"schema":'),
        ("recursive", (("[" * 1100) + "0" + ("]" * 1100)).encode("utf-8")),
    ],
)
def test_invalid_voice_json_preserves_prior_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
    encoded: bytes,
) -> None:
    previous = b"previous valid release evidence"
    bundle, calls = _execute(
        tmp_path, monkeypatch, encoded, previous_evidence=previous
    )
    with pytest.raises(RuntimeError, match="valid JSON evidence"):
        prove_packaged_voice_runtime(bundle, source_sha=SOURCE_SHA)

    assert len(calls) == 1, corruption
    assert (bundle / "packaged-voice-runtime-proof.json").read_bytes() == previous
    assert not tuple(bundle.glob(".voice-proof-*.tmp"))


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("numpy_imported", 1),
        ("sherpa_native_imported", False),
        ("sounddevice_data_proven", None),
        ("microphone_opened", True),
        ("model_loaded", True),
        ("human_tested", True),
        ("nvda_verified", True),
        ("production_release_ready", True),
    ],
)
def test_invalid_voice_claim_is_not_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    invalid: object,
) -> None:
    payload = {**_valid_voice_proof(), field: invalid}
    bundle, _ = _execute(tmp_path, monkeypatch, json.dumps(payload).encode("utf-8"))
    with pytest.raises(RuntimeError, match="(invalid evidence|may not set)"):
        prove_packaged_voice_runtime(bundle, source_sha=SOURCE_SHA)
    assert not (bundle / "packaged-voice-runtime-proof.json").exists()


def test_missing_voice_output_is_not_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, _ = _execute(tmp_path, monkeypatch, None)
    with pytest.raises(RuntimeError, match="valid JSON evidence"):
        prove_packaged_voice_runtime(bundle, source_sha=SOURCE_SHA)
    assert not (bundle / "packaged-voice-runtime-proof.json").exists()


def test_interrupted_voice_evidence_write_preserves_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = b"previous validated evidence"
    encoded = json.dumps(_valid_voice_proof()).encode("utf-8")
    bundle, _ = _execute(
        tmp_path, monkeypatch, encoded, previous_evidence=previous
    )

    def interrupted_fsync(_fd: int) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "fsync", interrupted_fsync)
    with pytest.raises(OSError, match="simulated disk failure"):
        prove_packaged_voice_runtime(bundle, source_sha=SOURCE_SHA)

    assert (bundle / "packaged-voice-runtime-proof.json").read_bytes() == previous
    assert not tuple(bundle.glob(".voice-proof-*.tmp"))
