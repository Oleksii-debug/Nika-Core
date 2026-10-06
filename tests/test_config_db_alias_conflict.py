from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic_settings import SettingsError

from nika_core.config import AppConfig


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_database_aliases_fail_before_default_or_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reverse: bool
) -> None:
    first = tmp_path / "Перші дані" / "nika.db"
    second = tmp_path / "Другі дані" / "nika.db"
    monkeypatch.delenv("NIKA_DB_PATH", raising=False)
    monkeypatch.delenv("NIKA_DATABASE_PATH", raising=False)
    overrides = [("NIKA_DB_PATH", first), ("NIKA_DATABASE_PATH", second)]
    for key, value in reversed(overrides) if reverse else overrides:
        monkeypatch.setenv(key, str(value))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        "nika_core.config.user_data_path",
        lambda *_args, **_kwargs: pytest.fail("default location was consulted"),
    )
    with pytest.raises(SettingsError, match="Суперечливі змінні середовища"):
        AppConfig.from_environment()
    assert not first.exists() and not second.exists()


def test_equivalent_database_aliases_preserve_explicit_unicode_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "Дані користувача" / "nika.db"
    monkeypatch.setenv("NIKA_DB_PATH", str(path))
    monkeypatch.setenv("NIKA_DATABASE_PATH", str(path.parent / "." / path.name))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    config = AppConfig.from_environment()
    assert config.database_path == path
    assert not path.exists()


@pytest.mark.parametrize("alias", ["NIKA_DB_PATH", "NIKA_DATABASE_PATH"])
def test_single_database_alias_still_selects_explicit_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias: str
) -> None:
    monkeypatch.delenv("NIKA_DB_PATH", raising=False)
    monkeypatch.delenv("NIKA_DATABASE_PATH", raising=False)
    path = tmp_path / "single path" / "nika.db"
    monkeypatch.setenv(alias, str(path))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert AppConfig.from_environment().database_path == path
    assert not path.exists()


def test_relative_path_is_still_rejected_when_aliases_agree(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NIKA_DB_PATH", "relative/nika.db")
    monkeypatch.setenv("NIKA_DATABASE_PATH", "relative/nika.db")
    with pytest.raises(ValueError, match="database_path must be absolute"):
        AppConfig.from_environment()


def test_case_insensitive_alias_name_conflict_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NIKA_DB_PATH", raising=False)
    monkeypatch.delenv("NIKA_DATABASE_PATH", raising=False)
    monkeypatch.setenv("nika_db_path", str(tmp_path / "one.db"))
    monkeypatch.setenv("NIKA_DATABASE_PATH", str(tmp_path / "two.db"))
    with pytest.raises(SettingsError, match="Суперечливі змінні середовища"):
        AppConfig.from_environment()
