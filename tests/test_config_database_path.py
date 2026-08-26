from __future__ import annotations

import sys
from pathlib import Path

from nika_core import config as config_module
from nika_core.config import AppConfig

_DATABASE_ENV_NAMES = ("NIKA_DB_PATH", "NIKA_DATABASE_PATH")


def _clear_database_environment(monkeypatch) -> None:
    for name in _DATABASE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_source_runtime_keeps_development_relative_database_default(monkeypatch) -> None:
    _clear_database_environment(monkeypatch)
    monkeypatch.delattr(sys, "frozen", raising=False)

    config = AppConfig.from_environment()

    assert config.database_path == Path("data/nika_core.db")


def test_frozen_runtime_database_default_is_stable_across_working_directories(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _clear_database_environment(monkeypatch)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    stable_root = tmp_path / "Користувач Ніка" / "Application Data"
    calls: list[tuple[str, bool]] = []

    def fake_user_data_path(appname: str, *, appauthor: bool) -> Path:
        calls.append((appname, appauthor))
        return stable_root

    monkeypatch.setattr(config_module, "user_data_path", fake_user_data_path)
    first_cwd = tmp_path / "запуск один"
    second_cwd = tmp_path / "запуск два"
    first_cwd.mkdir()
    second_cwd.mkdir()

    monkeypatch.chdir(first_cwd)
    first = AppConfig.from_environment().database_path
    monkeypatch.chdir(second_cwd)
    second = AppConfig.from_environment().database_path

    assert first == stable_root / "nika_core.db"
    assert second == first
    assert calls == [("NikaCore", False), ("NikaCore", False)]


def test_explicit_database_path_wins_over_frozen_default(monkeypatch, tmp_path: Path) -> None:
    _clear_database_environment(monkeypatch)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    explicit = tmp_path / "дані з пробілом" / "explicit.db"
    monkeypatch.setenv("NIKA_DB_PATH", str(explicit))

    def unexpected_user_data_path(*_args, **_kwargs) -> Path:
        raise AssertionError("packaged default must not run when NIKA_DB_PATH is explicit")

    monkeypatch.setattr(config_module, "user_data_path", unexpected_user_data_path)

    assert AppConfig.from_environment().database_path == explicit


def test_legacy_database_environment_alias_remains_supported(monkeypatch, tmp_path: Path) -> None:
    _clear_database_environment(monkeypatch)
    legacy = tmp_path / "legacy alias" / "ніка.db"
    monkeypatch.setenv("NIKA_DATABASE_PATH", str(legacy))

    assert AppConfig.from_environment().database_path == legacy


def test_primary_database_environment_alias_has_priority(monkeypatch, tmp_path: Path) -> None:
    _clear_database_environment(monkeypatch)
    primary = tmp_path / "primary.db"
    legacy = tmp_path / "legacy.db"
    monkeypatch.setenv("NIKA_DB_PATH", str(primary))
    monkeypatch.setenv("NIKA_DATABASE_PATH", str(legacy))

    assert AppConfig.from_environment().database_path == primary
