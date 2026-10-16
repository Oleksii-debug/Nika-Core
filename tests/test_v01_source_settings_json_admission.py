"""Reject ambiguous or corrupt stored source selection before binding a task."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.v01_source_settings import (
    SourceSelection,
    SourceSetupError,
    V01SourceSettings,
)


def _configured(tmp_path: Path) -> tuple[SQLiteStore, V01SourceSettings, SourceSelection]:
    store = SQLiteStore(tmp_path / "Ніка" / "стан.db")
    store.initialize()
    root = tmp_path / "Джерела з пробілами"
    root.mkdir()
    (root / "перший.txt").write_text("один", encoding="utf-8")
    (root / "другий.txt").write_text("два", encoding="utf-8")
    settings = V01SourceSettings(store, AppConfig(database_path=store.path))
    assert settings.configure(
        {"root": str(root), "source_a": "перший.txt", "source_b": "другий.txt", "revision": 0}
    ).status == "completed"
    selection = SourceSelection(
        root=str(root),
        source_a=str(root / "перший.txt"),
        source_b=str(root / "другий.txt"),
    )
    return store, settings, selection


@pytest.mark.parametrize(
    "bad",
    [
        lambda body: body[:-1] + ',"source_a":"other.txt"}',
        lambda body: body[:-1] + ',"ignored":{"id":1,"id":2}}',
        lambda body: body[:-1] + ',"ignored":1e400}',
        lambda body: body[:-1] + ',"ignored":NaN}',
        lambda body: body[:-1] + ',"ignored":Infinity}',
        lambda body: body + chr(0xD800),
        lambda body: body + " " * (1024 * 1024 + 1),
    ],
)
def test_corrupt_stored_source_json_is_never_accepted(
    tmp_path: Path, bad
) -> None:
    _, _, selection = _configured(tmp_path)
    body = bad(selection.model_dump_json())
    with pytest.raises(SourceSetupError, match="пошкоджені"):
        SourceSelection.from_stored(body)


def test_json_escaped_surrogate_in_source_path_is_rejected(tmp_path: Path) -> None:
    _, _, selection = _configured(tmp_path)
    body = json.dumps(
        {**selection.model_dump(), "source_a": chr(0xD800)}, ensure_ascii=True
    )
    with pytest.raises(SourceSetupError, match="пошкоджені"):
        SourceSelection.from_stored(body)


def test_legacy_formatted_ukrainian_paths_keep_original_meaning(tmp_path: Path) -> None:
    _, _, selection = _configured(tmp_path)
    body = json.dumps(selection.model_dump(), indent=2, ensure_ascii=False)
    assert SourceSelection.from_stored(body) == selection


def test_corrupt_singleton_does_not_replace_user_paths(tmp_path: Path) -> None:
    store, settings, selection = _configured(tmp_path)
    body = selection.model_dump_json()[:-1] + ',"source_a":"other.txt"}'
    with store.connection() as conn:
        conn.execute(
            "UPDATE v01_source_settings SET selection_json = ? WHERE singleton = 1",
            (body,),
        )
    assert settings.snapshot() == {"status": "invalid"}
    assert settings.configure(
        {
            "root": selection.root,
            "source_a": selection.source_a,
            "source_b": selection.source_b,
            "revision": 1,
        }
    ).status == "rejected"
    with pytest.raises(SourceSetupError, match="пошкоджені"):
        settings.prepare_task_payload({"command": "Порівняй"})
    with store.connection() as conn:
        assert conn.execute(
            "SELECT selection_json FROM v01_source_settings WHERE singleton = 1"
        ).fetchone()[0] == body


def test_matching_raw_digest_of_ambiguous_selection_cannot_bind_task(
    tmp_path: Path,
) -> None:
    store, settings, selection = _configured(tmp_path)
    body = selection.model_dump_json()[:-1] + ',"source_a":"other.txt"}'
    selection_id = hashlib.sha256(body.encode("utf-8")).hexdigest()
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO v01_source_selections VALUES (?, ?)", (selection_id, body)
        )
    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "Порівняй", "v01_source_selection": selection_id},
    )
    with pytest.raises(SourceSetupError, match="пошкоджені"):
        settings.for_task(task.task_id)
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM v01_task_source_bindings WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()[0] == 0


def test_mismatched_source_digest_preserves_existing_identity_error(
    tmp_path: Path,
) -> None:
    store, settings, _selection = _configured(tmp_path)
    selection_id = settings.prepare_task_payload({"command": "Порівняй"})[
        "v01_source_selection"
    ]
    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "Порівняй", "v01_source_selection": selection_id},
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE v01_source_selections SET selection_json = ? WHERE selection_id = ?",
            ('{"schema_version":1}', selection_id),
        )
    with pytest.raises(SourceSetupError, match="не вдалося перевірити"):
        settings.for_task(task.task_id)
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM v01_task_source_bindings WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()[0] == 0


def test_valid_legacy_json_preserves_raw_digest_and_task_binding(tmp_path: Path) -> None:
    store, settings, selection = _configured(tmp_path)
    body = json.dumps(selection.model_dump(), indent=2, ensure_ascii=False)
    selection_id = hashlib.sha256(body.encode("utf-8")).hexdigest()
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO v01_source_selections VALUES (?, ?)", (selection_id, body)
        )
    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "Порівняй", "v01_source_selection": selection_id},
    )
    assert settings.for_task(task.task_id) == selection
