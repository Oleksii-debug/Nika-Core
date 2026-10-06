from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.multi_agent import MultiAgentStore, TeamQuota, TeamState


def _clear_database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NIKA_DB_PATH", raising=False)
    monkeypatch.delenv("NIKA_DATABASE_PATH", raising=False)


def _make_launch_directories(tmp_path: Path) -> tuple[Path, Path, Path]:
    directories = (
        tmp_path / "Program Files" / "Nika Core",
        tmp_path / "launch-b",
        tmp_path / "Запуск Ніки з пробілом",
    )
    for directory in directories:
        directory.mkdir(parents=True)
    return directories


def test_packaged_default_database_is_stable_across_launch_cwds_and_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_database_environment(monkeypatch)
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    user_data_root = tmp_path / "Користувач Ніка" / "NikaCore"
    calls: list[tuple[str, bool]] = []

    def fake_user_data_path(appname: str, *, appauthor: bool) -> Path:
        calls.append((appname, appauthor))
        return user_data_root

    monkeypatch.setattr("nika_core.config.user_data_path", fake_user_data_path)
    cwd_a, cwd_b, cwd_unicode = _make_launch_directories(tmp_path)
    expected_database = user_data_root / "nika_core.db"

    monkeypatch.chdir(cwd_a)
    first_config = AppConfig.from_environment()
    assert first_config.database_path == expected_database
    assert first_config.database_path.is_absolute()
    assert not first_config.database_path.is_relative_to(cwd_a)

    first_store = SQLiteStore(first_config.database_path)
    first_store.initialize()
    task = TaskQueue(first_store).create(
        workspace_id="workspace-v01",
        agent_id="agent-v01",
        payload={"command": "Перевір стійкий стан команди"},
    )
    teams = MultiAgentStore(first_store)
    teams.create_team(
        team_id="team-v01",
        root_member_id="root",
        root_agent_id="agent-root",
        root_agent_version=1,
        root_thread_id="thread-root",
        root_grants=(),
        quota=TeamQuota(max_total_agents=3, max_parallel=2),
    )
    teams.spawn_child(
        team_id="team-v01",
        parent_id="root",
        child_id="worker-a",
        agent_id="agent-a",
        agent_version=1,
        thread_id="thread-a",
        requested_grants=(),
    )
    teams.spawn_child(
        team_id="team-v01",
        parent_id="root",
        child_id="worker-b",
        agent_id="agent-b",
        agent_version=1,
        thread_id="thread-b",
        requested_grants=(),
    )

    monkeypatch.chdir(cwd_b)
    second_config = AppConfig.from_environment()
    assert second_config.database_path == expected_database
    second_store = SQLiteStore(second_config.database_path)
    second_store.initialize()
    recovered_task = TaskQueue(second_store).get(task.task_id)
    recovered_team = MultiAgentStore(second_store)
    assert recovered_task.payload == {"command": "Перевір стійкий стан команди"}
    assert recovered_team.team_state("team-v01") is TeamState.ACTIVE
    assert {member.member_id for member in recovered_team.members("team-v01")} == {
        "root",
        "worker-a",
        "worker-b",
    }

    monkeypatch.chdir(cwd_unicode)
    third_config = AppConfig.from_environment()
    assert third_config.database_path == expected_database
    third_store = SQLiteStore(third_config.database_path)
    third_store.initialize()
    assert TaskQueue(third_store).get(task.task_id).task_id == task.task_id
    assert MultiAgentStore(third_store).team_state("team-v01") is TeamState.ACTIVE

    assert calls == [
        ("NikaCore", False),
        ("NikaCore", False),
        ("NikaCore", False),
    ]
    for launch_directory in (cwd_a, cwd_b, cwd_unicode):
        assert not (launch_directory / "data" / "nika_core.db").exists()


def test_explicit_absolute_nika_db_path_override_is_cwd_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override = tmp_path / "Explicit Override" / "дані nika.db"
    monkeypatch.setenv("NIKA_DB_PATH", str(override))
    monkeypatch.delenv("NIKA_DATABASE_PATH", raising=False)

    def unexpected_default(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("platform default must not run for an explicit NIKA_DB_PATH")

    monkeypatch.setattr("nika_core.config.user_data_path", unexpected_default)
    cwd_a, cwd_b, cwd_unicode = _make_launch_directories(tmp_path)

    monkeypatch.chdir(cwd_a)
    first_config = AppConfig.from_environment()
    assert first_config.database_path == override
    first_store = SQLiteStore(first_config.database_path)
    first_store.initialize()
    task = TaskQueue(first_store).create(
        workspace_id="override-workspace",
        agent_id="override-agent",
        payload={"source": "explicit"},
    )

    for launch_directory in (cwd_b, cwd_unicode):
        monkeypatch.chdir(launch_directory)
        config = AppConfig.from_environment()
        assert config.database_path == override
        store = SQLiteStore(config.database_path)
        store.initialize()
        assert TaskQueue(store).get(task.task_id).payload == {"source": "explicit"}
        assert not (launch_directory / "data" / "nika_core.db").exists()


def test_relative_nika_db_path_override_fails_closed_without_cwd_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd_a, _cwd_b, _cwd_unicode = _make_launch_directories(tmp_path)
    monkeypatch.chdir(cwd_a)
    monkeypatch.setenv("NIKA_DB_PATH", "relative-data/nika.db")
    monkeypatch.delenv("NIKA_DATABASE_PATH", raising=False)

    with pytest.raises(ValueError, match="database_path must be absolute"):
        AppConfig.from_environment()

    assert not (cwd_a / "relative-data" / "nika.db").exists()
    assert not (cwd_a / "data" / "nika_core.db").exists()


def test_unusable_explicit_database_path_fails_without_cwd_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd_a, _cwd_b, _cwd_unicode = _make_launch_directories(tmp_path)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocked", encoding="utf-8")
    requested_database = blocker / "nika.db"
    monkeypatch.chdir(cwd_a)
    monkeypatch.setenv("NIKA_DB_PATH", str(requested_database))
    monkeypatch.delenv("NIKA_DATABASE_PATH", raising=False)

    config = AppConfig.from_environment()
    with pytest.raises(OSError):
        SQLiteStore(config.database_path).initialize()

    assert not (cwd_a / "data" / "nika_core.db").exists()
