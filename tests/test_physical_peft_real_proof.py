from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _proof_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "physical_peft_real_proof.py"
    spec = importlib.util.spec_from_file_location("physical_peft_real_proof_test_target", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stable_file_bytes_round_trip(tmp_path: Path) -> None:
    proof = _proof_module()
    target = tmp_path / "candidate.bin"
    target.write_bytes(b"candidate-bytes")

    assert proof._stable_file_bytes(
        target,
        max_bytes=1024,
        name="candidate",
    ) == b"candidate-bytes"


def test_stable_file_bytes_rejects_bound(tmp_path: Path) -> None:
    proof = _proof_module()
    target = tmp_path / "candidate.bin"
    target.write_bytes(b"candidate-bytes")

    with pytest.raises(proof.ProofError, match="size or file type is invalid"):
        proof._stable_file_bytes(target, max_bytes=4, name="candidate")


def test_stable_file_bytes_rejects_post_read_identity_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = _proof_module()
    target = tmp_path / "candidate.bin"
    target.write_bytes(b"candidate-bytes")
    original_lstat = proof.os.lstat
    initial = original_lstat(target)
    calls = 0

    def changed_lstat(path: object):
        nonlocal calls
        value = original_lstat(path)
        if Path(path) != target:
            return value
        calls += 1
        if calls == 1:
            return value
        return SimpleNamespace(
            st_mode=initial.st_mode,
            st_dev=initial.st_dev,
            st_ino=initial.st_ino,
            st_size=initial.st_size,
            st_mtime_ns=initial.st_mtime_ns + 1,
            st_file_attributes=getattr(initial, "st_file_attributes", 0),
        )

    monkeypatch.setattr(proof.os, "lstat", changed_lstat)

    with pytest.raises(proof.ProofError, match="changed while it was being snapshotted"):
        proof._stable_file_bytes(target, max_bytes=1024, name="candidate")


def _minimal_asset_manifest(proof, root: Path) -> dict[str, object]:
    model_dir = root / "model"
    model_dir.mkdir()
    model_payload = b"config-bytes"
    gguf_payload = b"gguf-bytes"
    (model_dir / "config.json").write_bytes(model_payload)
    (root / "base.gguf").write_bytes(gguf_payload)
    return {
        "license": proof._MODEL_LICENSE,
        "license_reference": proof._MODEL_LICENSE_REFERENCE,
        "model_files": [
            {
                "path": "config.json",
                "sha256": proof._sha256_bytes(model_payload),
                "size_bytes": len(model_payload),
            },
            {
                "path": proof._GGUF_FILE,
                "sha256": proof._sha256_bytes(gguf_payload),
                "size_bytes": len(gguf_payload),
            },
        ],
        "repository": proof._MODEL_REPOSITORY,
        "revision": proof._MODEL_REVISION,
        "runtime_versions": {"runtime": "1.0"},
        "source_reference": proof._MODEL_SOURCE_REFERENCE,
    }


def test_verified_asset_manifest_binds_staged_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = _proof_module()
    monkeypatch.setattr(proof, "_MODEL_FILES", ("config.json",))
    monkeypatch.setattr(proof, "_RUNTIME_PACKAGES", ("runtime",))
    monkeypatch.setattr(proof, "_runtime_versions", lambda: {"runtime": "1.0"})
    manifest = _minimal_asset_manifest(proof, tmp_path)
    raw = proof._canonical_json(manifest).encode("utf-8")

    observed = proof._verified_asset_manifest(tmp_path, raw)

    assert observed == manifest
    (tmp_path / "model" / "config.json").write_bytes(b"changed")
    with pytest.raises(proof.ProofError, match="identity changed"):
        proof._verified_asset_manifest(tmp_path, raw)


def test_verified_asset_manifest_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    proof = _proof_module()
    raw = b'{"license":"a","license":"b"}'

    with pytest.raises(proof.ProofError, match="invalid JSON"):
        proof._verified_asset_manifest(tmp_path, raw)


def test_candidate_tokenization_evidence_is_required() -> None:
    proof = _proof_module()
    digest = "ab" * 32

    assert (
        proof._require_candidate_tokenization_sha256(
            {"tokenization_sha256": digest}
        )
        == digest
    )

    for manifest in (
        {},
        {"tokenization_sha256": "A" * 64},
        {"tokenization_sha256": "0" * 63},
        {"tokenization_sha256": 7},
    ):
        with pytest.raises(
            proof.ProofError,
            match="lacks canonical tokenization evidence",
        ):
            proof._require_candidate_tokenization_sha256(manifest)


def test_candidate_evidence_snapshot_rejects_manifest_path_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = _proof_module()
    candidate = tmp_path / "adapter_model.safetensors"
    candidate_bytes = b"exact-physical-proof-candidate"
    candidate.write_bytes(candidate_bytes)

    def mutating_manifest(path: Path) -> dict[str, object]:
        Path(path).write_bytes(b"mutated-during-manifest-read")
        return {}

    monkeypatch.setattr(proof, "candidate_adapter_manifest", mutating_manifest)

    with pytest.raises(proof.ProofError):
        proof._verified_candidate_evidence_from_snapshot(
            candidate,
            candidate_bytes,
        )


def test_candidate_evidence_snapshot_rejects_tensor_path_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = _proof_module()
    candidate = tmp_path / "adapter_model.safetensors"
    candidate_bytes = b"exact-physical-proof-candidate"
    candidate.write_bytes(candidate_bytes)
    monkeypatch.setattr(proof, "candidate_adapter_manifest", lambda _path: {})

    import safetensors

    class MutatingSafeOpen:
        def __enter__(self):
            candidate.write_bytes(b"mutated-during-tensor-read")
            return SimpleNamespace(
                keys=lambda: ("tensor",),
                get_tensor=lambda _name: SimpleNamespace(numel=lambda: 1),
            )

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

    monkeypatch.setattr(
        safetensors,
        "safe_open",
        lambda *_args, **_kwargs: MutatingSafeOpen(),
    )

    with pytest.raises(proof.ProofError):
        proof._verified_candidate_evidence_from_snapshot(
            candidate,
            candidate_bytes,
        )


def test_write_new_file_never_replaces_existing_evidence(tmp_path: Path) -> None:
    proof = _proof_module()
    target = tmp_path / "physical-proof-summary.json"
    target.write_bytes(b"existing-evidence")

    with pytest.raises(proof.ProofError, match="could not be published"):
        proof._write_new_file(target, b"replacement")

    assert target.read_bytes() == b"existing-evidence"

