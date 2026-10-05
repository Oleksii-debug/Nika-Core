"""Reject ambiguous or corrupt persisted model routes before task binding."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.v01_model_settings import (
    ModelSelection,
    ModelSetupError,
    V01ModelSettings,
)


@pytest.mark.parametrize(
    "body",
    [
        '{"route_kind":"deterministic","route_kind":"ollama"}',
        '{"route_kind":"deterministic","extra":{"key":1,"key":2}}',
        '{"route_kind":"deterministic","timeout_seconds":1e400}',
        '{"route_kind":"deterministic","private_data_allowed":NaN}',
        '{"route_kind":"deterministic","private_data_allowed":Infinity}',
        " " * 65537 + '{"route_kind":"deterministic"}',
        '{"route_kind":"deterministic"}' + chr(0xD800),
    ],
)
def test_corrupt_stored_json_fails_with_safe_error(body: str) -> None:
    with pytest.raises(ModelSetupError, match="пошкоджені"):
        ModelSelection.from_stored(body)


def test_valid_legacy_whitespace_and_ukrainian_route_remain_readable() -> None:
    assert ModelSelection.from_stored(
        '  {"route_kind": "deterministic", "schema_version": 1}  '
    ).route_kind == "deterministic"
    route = ModelSelection(
        route_kind="ollama",
        provider_id="ollama",
        model="Українська-модель:1",
        base_url="http://localhost:11434",
    )
    assert ModelSelection.from_stored(route.canonical_json()) == route


def test_corrupt_singleton_cannot_be_silently_replaced(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "Дані Ніки" / "модель.db")
    store.initialize()
    settings = V01ModelSettings(store)
    assert settings.configure({"route_kind": "deterministic", "revision": 0}).status == (
        "completed"
    )
    body = '{"route_kind":"ollama","route_kind":"deterministic"}'
    with store.connection() as conn:
        conn.execute(
            "UPDATE v01_model_settings SET selection_json = ? WHERE singleton = 1",
            (body,),
        )
    assert settings.snapshot() == {"status": "invalid"}
    assert settings.configure({"route_kind": "deterministic", "revision": 1}).status == (
        "rejected"
    )
    with pytest.raises(ModelSetupError, match="пошкоджені"):
        settings.prepare_task_payload({"command": "Перевір"})
    with store.connection() as conn:
        assert conn.execute(
            "SELECT selection_json FROM v01_model_settings WHERE singleton = 1"
        ).fetchone()[0] == body


def test_matching_raw_digest_cannot_authorize_ambiguous_task_route(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "Дані Ніки" / "модель.db")
    store.initialize()
    settings = V01ModelSettings(store)
    body = '{"route_kind":"ollama","route_kind":"deterministic"}'
    selection_id = hashlib.sha256(body.encode("utf-8")).hexdigest()
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO v01_model_selections (selection_id, selection_json) VALUES (?, ?)",
            (selection_id, body),
        )
    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"v01_model_selection": selection_id, "command": "Перевір"},
    )
    with pytest.raises(ModelSetupError, match="пошкоджені"):
        settings.for_task(task.task_id)
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM v01_task_model_bindings WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()[0] == 0


def test_mismatched_model_digest_preserves_existing_identity_error(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "Дані Ніки" / "модель.db")
    store.initialize()
    settings = V01ModelSettings(store)
    assert settings.configure({"route_kind": "deterministic", "revision": 0}).status == (
        "completed"
    )
    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=settings.prepare_task_payload({"command": "Перевір"}),
    )
    selection_id = task.payload["v01_model_selection"]
    with store.connection() as conn:
        conn.execute(
            "UPDATE v01_model_selections SET selection_json = ? WHERE selection_id = ?",
            ('{"schema_version":1}', selection_id),
        )
    with pytest.raises(ModelSetupError, match="не вдалося перевірити"):
        settings.for_task(task.task_id)
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM v01_task_model_bindings WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()[0] == 0


def test_noncanonical_but_valid_legacy_json_keeps_its_raw_digest(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "Дані Ніки" / "модель.db")
    store.initialize()
    settings = V01ModelSettings(store)
    body = json.dumps({"route_kind": "deterministic"}, indent=2)
    selection_id = hashlib.sha256(body.encode("utf-8")).hexdigest()
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO v01_model_selections (selection_id, selection_json) VALUES (?, ?)",
            (selection_id, body),
        )
    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"v01_model_selection": selection_id},
    )
    assert settings.for_task(task.task_id).route_kind == "deterministic"
