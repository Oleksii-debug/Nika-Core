"""Configured local-model routes must meet the incumbent health probe's identity limits."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.v01_model_settings import ModelSelection, ModelSetupError, V01ModelSettings


def _local(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "route_kind": "ollama",
        "provider_id": "ollama",
        "model": "qwen3:8b",
        "base_url": "http://localhost:11434",
        "timeout_seconds": 60,
        "revision": 0,
    }
    payload.update(changes)
    return payload


@pytest.mark.parametrize(
    "model",
    [
        "qwen3:8b\u202e",
        "qwen3:8b\u200b",
        "qwen3:8b\x7f",
        "qwen3:8b\u0085",
        "м" * 257,
    ],
)
def test_unusable_model_identity_is_rejected_before_persistence(
    model: str, tmp_path: Path
) -> None:
    store = SQLiteStore(tmp_path / "Дані програми" / "ніка.db")
    store.initialize()
    settings = V01ModelSettings(store)
    assert settings.configure(_local(model=model)).status == "rejected"
    assert settings.snapshot() == {"status": "missing"}
    with store.connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM v01_model_selections").fetchone()[0]
    assert count == 0


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:11434/?",
        "http://localhost:11434/#",
        "http://localhost:11434?",
        "http://localhost:11434#",
    ],
)
def test_empty_url_delimiters_rejected_as_health_probe_rejects_them(
    base_url: str, tmp_path: Path
) -> None:
    store = SQLiteStore(tmp_path / "ніка.db")
    store.initialize()
    settings = V01ModelSettings(store)
    assert settings.configure(_local(base_url=base_url)).status == "rejected"
    assert settings.snapshot() == {"status": "missing"}


def test_oversized_timeout_is_user_safe_and_does_not_replace_route(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "ніка.db")
    store.initialize()
    settings = V01ModelSettings(store)
    assert settings.configure(_local()).status == "completed"
    previous = settings.snapshot()
    assert settings.configure(_local(revision=1, timeout_seconds=10**400)).status == (
        "rejected"
    )
    assert settings.snapshot() == previous


def test_printable_cyrillic_and_loopback_ipv6_remain_supported(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "Дані програми" / "ніка.db")
    store.initialize()
    settings = V01ModelSettings(store)
    assert settings.configure(
        _local(model="Модель-укр:1", base_url="http://[::1]:11434/")
    ).status == "completed"
    snapshot = settings.snapshot()
    assert snapshot["model"] == "Модель-укр:1"
    assert snapshot["base_url"] == "http://[::1]:11434/"


def test_old_invisible_persisted_model_is_rejected_on_recovery() -> None:
    body = ModelSelection(route_kind="deterministic").model_dump()
    body.update(route_kind="ollama", provider_id="ollama", model="qwen3:8b\u202e")
    body.update(base_url="http://localhost:11434")
    with pytest.raises(ModelSetupError, match="пошкоджені"):
        ModelSelection.from_stored(json.dumps(body, ensure_ascii=False))
