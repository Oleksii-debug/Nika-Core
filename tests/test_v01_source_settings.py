from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.contracts import RuntimeOutcome, RuntimeRequest, RuntimeResumeRequest
from nika_core.ui.desktop_backend import DesktopBackend
from nika_core.v01_packaged_team_runtime import V01PackagedThreeAgentRuntime
from nika_core.v01_source_settings import SourceSetupError, V01SourceSettings
from scripts.nika_windows import build_windows_bridge


def _files(root: Path) -> dict[str, str]:
    root.mkdir()
    (root / "джерело А.txt").write_text("Спільне контрольоване свідчення.", encoding="utf-8")
    (root / "джерело Б.txt").write_text("Спільне контрольоване свідчення.", encoding="utf-8")
    return {"root": str(root), "source_a": "джерело А.txt", "source_b": "джерело Б.txt"}


def _settings(tmp_path: Path) -> tuple[SQLiteStore, AppConfig, V01SourceSettings]:
    config = AppConfig(database_path=tmp_path / "Дані програми" / "ніка.db")
    store = SQLiteStore(config.database_path)
    store.initialize()
    return store, config, V01SourceSettings(store, config)


def _dispatch(bridge, action: str, payload: dict[str, object]) -> dict[str, object]:
    return bridge.dispatch(
        {"request_id": "source-setup-test", "action_id": action, "payload": payload}
    )


def test_clean_bridge_rejects_unconfigured_task_before_enqueue(tmp_path: Path) -> None:
    store, config, _ = _settings(tmp_path)
    bridge, _ = build_windows_bridge(config)
    assert bridge.get_state()["state"]["v01_sources"]["status"] == "missing"
    result = _dispatch(bridge, "task.create", {"command": "Порівняй джерела"})
    assert result["status"] == "rejected"
    assert "налаштуйте джерела" in result["message"]
    assert TaskQueue(store).list_recent() == ()


def test_saved_unicode_setup_survives_restart_and_audit_omits_paths(tmp_path: Path) -> None:
    store, config, settings = _settings(tmp_path)
    paths = _files(tmp_path / "Приватні джерела CANARY")
    result = settings.configure({**paths, "revision": 0})
    assert result.status == "completed"
    assert result.focus_id == "command-input"
    restarted = V01SourceSettings(
        SQLiteStore(config.database_path), AppConfig(database_path=config.database_path)
    )
    state = restarted.snapshot()
    assert state["revision"] == 1
    assert state["root"] == paths["root"]
    assert state["source_a"] == str(Path(paths["root"]) / paths["source_a"])
    with store.connection() as conn:
        audit = conn.execute("SELECT payload_json FROM audit_events").fetchall()
    assert audit
    assert "CANARY" not in json.dumps([tuple(row) for row in audit], ensure_ascii=False)


@pytest.mark.parametrize(
    "change",
    [
        {"root": "relative-root"},
        {"source_b": "missing.txt"},
        {"source_b": "джерело А.txt"},
        {"source_b": "../outside.txt"},
        {"source_a": "payload.exe"},
        {"source_a": "invalid\x00.txt"},
        {"source_a": ""},
        {"source_b": 123},
        {"revision": True},
        {"schema_version": True},
        {"permission": "unrestricted"},
    ],
)
def test_invalid_setup_does_not_overwrite_valid_selection(tmp_path: Path, change: dict) -> None:
    _, _, settings = _settings(tmp_path)
    paths = _files(tmp_path / "Файли")
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    (Path(paths["root"]) / "payload.exe").write_bytes(b"not an allowed document")
    assert settings.configure({**paths, "revision": 0}).status == "completed"
    before = settings.snapshot()
    result = settings.configure({**paths, "revision": 1, **change})
    assert result.status == "rejected"
    assert settings.snapshot() == before


def test_oversized_source_is_rejected_before_any_selection_is_saved(tmp_path: Path) -> None:
    _, _, settings = _settings(tmp_path)
    paths = _files(tmp_path / "Файли")
    with (Path(paths["root"]) / paths["source_b"]).open("wb") as handle:
        handle.truncate(16 * 1024 * 1024 + 1)
    assert settings.configure({**paths, "revision": 0}).status == "rejected"
    assert settings.snapshot()["status"] == "missing"


@pytest.mark.parametrize("corrupt_revision", ["PRIVATE_REVISION_CANARY", 1.5, 1 << 53])
def test_corrupt_revision_keeps_other_state_readable_and_blocks_new_tasks(
    tmp_path: Path, corrupt_revision: object
) -> None:
    store, config, settings = _settings(tmp_path)
    paths = _files(tmp_path / "Файли")
    assert settings.configure({**paths, "revision": 0}).status == "completed"
    # SQLite's numeric affinity and revision > 0 check alone also accept text
    # or a positive non-integral number. The service must validate stored types.
    with store.connection() as conn:
        conn.execute("UPDATE v01_source_settings SET revision = ?", (corrupt_revision,))
    bridge, _ = build_windows_bridge(config)
    state = bridge.get_state()
    assert state["ok"] is True
    assert state["state"]["v01_sources"] == {"status": "invalid"}
    result = _dispatch(bridge, "task.create", {"command": "Порівняй джерела"})
    assert result["status"] == "rejected"
    assert "PRIVATE_REVISION_CANARY" not in json.dumps([state, result])
    assert TaskQueue(store).list_recent() == ()
    assert settings.configure({**paths, "revision": 1}).status == "rejected"


def test_symlink_cannot_escape_declared_root(tmp_path: Path) -> None:
    _, _, settings = _settings(tmp_path)
    paths = _files(tmp_path / "Файли")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = Path(paths["root"]) / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("This Windows account cannot create symlinks")
    assert settings.configure({**paths, "source_b": str(link), "revision": 0}).status == "rejected"


def test_concurrent_settings_writers_cannot_silently_replace_each_other(tmp_path: Path) -> None:
    store, config, settings = _settings(tmp_path)
    a = _files(tmp_path / "Перша папка")
    b = _files(tmp_path / "Друга папка")
    second = V01SourceSettings(store, config)
    barrier = Barrier(2)

    def save(client, paths):
        barrier.wait(timeout=5)
        return client.configure({**paths, "revision": 0})

    with ThreadPoolExecutor(max_workers=2) as executor:
        left = executor.submit(save, settings, a)
        right = executor.submit(save, second, b)
        results = (left.result(timeout=10), right.result(timeout=10))
    assert sorted(result.status for result in results) == ["completed", "rejected"]
    assert settings.snapshot()["revision"] == 1


def test_accepted_task_keeps_sources_if_settings_change_before_runtime_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, config, settings = _settings(tmp_path)
    a = _files(tmp_path / "Первинні джерела")
    b = _files(tmp_path / "Наступні джерела")
    pending = []
    original_start = DesktopBackend._schedule_start
    monkeypatch.setattr(
        DesktopBackend,
        "_schedule_start",
        lambda self, task, command: pending.append((self, task, command)),
    )
    bridge, _ = build_windows_bridge(config)
    assert (
        _dispatch(bridge, "team.sources.configure", {**a, "revision": 0})["status"] == "completed"
    )
    assert (
        _dispatch(bridge, "task.create", {"command": "Порівняй два джерела"})["status"]
        == "accepted"
    )
    assert (
        _dispatch(bridge, "team.sources.configure", {**b, "revision": 1})["status"] == "completed"
    )
    backend, task_id, command = pending.pop()
    accepted = TaskQueue(store).get(task_id)
    assert len(accepted.payload["v01_source_selection"]) == 64
    assert a["root"] not in json.dumps(accepted.payload)
    original_start(backend, task_id, command)
    backend.close()
    assert TaskQueue(store).get(task_id).state is TaskState.COMPLETED
    assert settings.for_task(task_id).root == a["root"]
    state = bridge.get_state()["state"]
    assert state["v01_sources"]["root"] == b["root"]
    assert state["v01_team_task"]["team"]["member_count"] == 3
    proof = Path(__file__).resolve().parents[1] / "scripts" / "m5_uia_proof.ps1"
    final = state["v01_team_task"]["final_result"]
    assert final["status"] == "completed"
    assert final["summary"] in proof.read_text(encoding="utf-8")
    with store.connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM multi_agent_results").fetchone()[0]
    for path in Path(a["root"]).iterdir():
        path.unlink()
    runtime = V01PackagedThreeAgentRuntime(
        store=store, config=AppConfig(database_path=config.database_path)
    )
    thread_id = f"desktop-{task_id}"
    result = asyncio.run(
        runtime.resume(
            RuntimeResumeRequest(
                task_id=task_id,
                thread_id=thread_id,
                resume_token=runtime.initial_resume_token(task_id=task_id, thread_id=thread_id),
            )
        )
    )
    assert result.outcome is RuntimeOutcome.COMPLETED
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM multi_agent_results").fetchone()[0] == count == 3


def test_corrupt_or_newer_settings_fail_closed_without_replacement(tmp_path: Path) -> None:
    store, config, settings = _settings(tmp_path)
    paths = _files(tmp_path / "Файли")
    assert settings.configure({**paths, "revision": 0}).status == "completed"
    with store.connection() as conn:
        conn.execute(
            "UPDATE v01_source_settings SET selection_json = ?", ('{"schema_version": 99}',)
        )
    assert settings.snapshot() == {"status": "invalid"}
    assert settings.configure({**paths, "revision": 1}).status == "rejected"
    with pytest.raises(SourceSetupError):
        settings.prepare_task_payload({"command": "Compare"})
    with store.connection() as conn:
        conn.execute("INSERT INTO v01_source_settings_schema VALUES (2, 'future')")
    with pytest.raises(SourceSetupError):
        V01SourceSettings(store, config)


def test_task_selection_reference_rejects_changed_persisted_contents(tmp_path: Path) -> None:
    store, config, settings = _settings(tmp_path)
    paths = _files(tmp_path / "Джерела")
    build_windows_bridge(config)  # Canonical default workspace and agent registration.
    assert settings.configure({**paths, "revision": 0}).status == "completed"
    payload = settings.prepare_task_payload({"command": "Порівняй"})
    task = TaskQueue(store).create(workspace_id="default", agent_id="nika.default", payload=payload)
    with store.connection() as conn:
        conn.execute(
            "UPDATE v01_source_selections SET selection_json = ?", ('{"schema_version":99}',)
        )
    with pytest.raises(SourceSetupError):
        settings.for_task(task.task_id)
    runtime = V01PackagedThreeAgentRuntime(store=store, config=config)
    result = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id=task.task_id,
                thread_id=f"desktop-{task.task_id}",
                payload={"command": "Порівняй"},
            )
        )
    )
    assert result.outcome is RuntimeOutcome.FAILED
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM multi_agent_teams").fetchone()[0] == 0


def test_legacy_team_is_never_rebound_to_replacement_setup(tmp_path: Path) -> None:
    store, config, settings = _settings(tmp_path)
    a = _files(tmp_path / "Первинні джерела")
    b = _files(tmp_path / "Наступні джерела")
    assert settings.configure({**a, "revision": 0}).status == "completed"
    runtime = V01PackagedThreeAgentRuntime(store=store, config=config)
    request = RuntimeRequest(
        task_id="legacy-task", thread_id="desktop-legacy-task", payload={"command": "Порівняй"}
    )
    assert asyncio.run(runtime.run(request)).outcome is RuntimeOutcome.COMPLETED
    with store.connection() as conn:
        # A #630-era team has canonical handoffs but no setup-extension binding.
        conn.execute("DELETE FROM v01_task_source_bindings WHERE task_id = ?", (request.task_id,))
    assert settings.configure({**b, "revision": 1}).status == "completed"
    assert asyncio.run(runtime.run(request)).outcome is RuntimeOutcome.FAILED
    assert settings.configure({**a, "revision": 2}).status == "completed"
    assert asyncio.run(runtime.run(request)).outcome is RuntimeOutcome.COMPLETED
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM multi_agent_results").fetchone()[0] == 3
