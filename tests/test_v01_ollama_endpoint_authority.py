from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.v01_model_settings import V01ModelSettings


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "profile" / "nika.db")
    store.initialize()
    return store


@pytest.mark.parametrize(
    "base_url",
    (
        "http://localhost",
        "http://localhost:",
        "http://localhost:abc",
        "http://localhost:0",
        "http://localhost:65536",
        "http://[::1]",
    ),
)
def test_ollama_selection_rejects_missing_or_invalid_explicit_port(
    tmp_path: Path,
    base_url: str,
) -> None:
    settings = V01ModelSettings(_store(tmp_path))

    result = settings.configure(
        {
            "schema_version": 1,
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": "qwen3:8b",
            "base_url": base_url,
            "credential_ref": None,
            "private_data_allowed": False,
            "timeout_seconds": 30,
            "revision": 0,
        }
    )

    assert result.status == "rejected"
    assert settings.snapshot() == {"status": "missing", "revision": 0}
