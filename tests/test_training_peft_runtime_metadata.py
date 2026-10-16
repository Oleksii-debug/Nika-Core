from __future__ import annotations

import pytest

from nika_core.training_peft_worker import build_training_runtime_metadata


def _versions() -> dict[str, str]:
    return {
        "torch": "2.14.1",
        "transformers": "5.18.2",
        "peft": "0.21.2",
        "accelerate": "1.15.0",
        "gguf": "0.19.0",
        "safetensors": "0.8.1",
    }


def test_build_training_runtime_metadata_is_complete_and_deterministic() -> None:
    versions = _versions()

    metadata = build_training_runtime_metadata(versions)

    assert metadata == {
        "nika.training.runtime.accelerate.version": "1.15.0",
        "nika.training.runtime.gguf.version": "0.19.0",
        "nika.training.runtime.peft.version": "0.21.2",
        "nika.training.runtime.safetensors.version": "0.8.1",
        "nika.training.runtime.torch.version": "2.14.1",
        "nika.training.runtime.transformers.version": "5.18.2",
    }
    assert versions == _versions()


def test_build_training_runtime_metadata_rejects_missing_distribution() -> None:
    versions = _versions()
    del versions["gguf"]

    with pytest.raises(ValueError, match="distribution keys"):
        build_training_runtime_metadata(versions)


def test_build_training_runtime_metadata_rejects_ambiguous_distribution() -> None:
    versions = _versions()
    versions["unexpected"] = "1.0"

    with pytest.raises(ValueError, match="distribution keys"):
        build_training_runtime_metadata(versions)


def test_build_training_runtime_metadata_requires_exact_dict() -> None:
    with pytest.raises(TypeError, match="exact dict"):
        build_training_runtime_metadata(dict(_versions()).items())  # type: ignore[arg-type]
