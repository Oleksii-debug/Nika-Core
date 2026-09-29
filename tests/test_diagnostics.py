from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.schema import SCHEMA_VERSION
from nika_core.data.sqlite import SQLiteStore
from nika_core.diagnostics import CheckStatus, collect_diagnostics


def _config(path: Path) -> AppConfig:
    return AppConfig(database_path=path, model_provider="mock")


def test_missing_data_directory_warns_without_creating_it(tmp_path: Path) -> None:
    database = tmp_path / "missing" / "nika_core.db"

    report = collect_diagnostics(_config(database))

    assert report.status is CheckStatus.WARN
    assert not database.parent.exists()
    assert "missing" not in report.to_text()
    assert str(database) not in report.to_json()


def test_existing_database_is_checked_read_only(tmp_path: Path) -> None:
    database = tmp_path / "nika_core.db"
    SQLiteStore(database).initialize()
    before = database.read_bytes()

    report = collect_diagnostics(_config(database))

    assert report.status is CheckStatus.PASS
    assert database.read_bytes() == before
    check = next(item for item in report.checks if item.check_id == "database")
    assert check.status is CheckStatus.PASS
    assert "tables=" in check.message


def test_newer_core_schema_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "nika_core.db"
    SQLiteStore(database).initialize()
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION + 1, "future"),
    )
    connection.commit()
    connection.close()

    report = collect_diagnostics(_config(database))

    check = next(item for item in report.checks if item.check_id == "core_schema")
    assert check.status is CheckStatus.FAIL
    assert report.status is CheckStatus.FAIL


def test_older_core_schema_warns_without_migrating(tmp_path: Path) -> None:
    database = tmp_path / "nika_core.db"
    SQLiteStore(database).initialize()
    connection = sqlite3.connect(database)
    connection.execute(
        "DELETE FROM schema_migrations WHERE version = ?",
        (SCHEMA_VERSION,),
    )
    connection.commit()
    connection.close()
    before = database.read_bytes()

    report = collect_diagnostics(_config(database))

    check = next(item for item in report.checks if item.check_id == "core_schema")
    assert check.status is CheckStatus.WARN
    assert report.status is CheckStatus.WARN
    assert database.read_bytes() == before


def test_corrupt_database_fails_without_echoing_path(tmp_path: Path) -> None:
    database = tmp_path / "sensitive-name.db"
    database.write_bytes(b"not sqlite")

    report = collect_diagnostics(_config(database))

    assert report.status is CheckStatus.FAIL
    assert str(database) not in report.to_text()
    assert str(database) not in report.to_json()


def test_database_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.db"
    connection = sqlite3.connect(target)
    connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    link = tmp_path / "nika_core.db"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")

    report = collect_diagnostics(_config(link))

    check = next(item for item in report.checks if item.check_id == "database")
    assert check.status is CheckStatus.FAIL


def test_invalid_environment_configuration_fails_without_echoing_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NIKA_DB_PATH", "relative-secret.db")

    report = collect_diagnostics()

    assert report.status is CheckStatus.FAIL
    assert report.checks[0].check_id == "configuration"
    assert "relative-secret.db" not in report.to_text()
    assert "relative-secret.db" not in report.to_json()


def test_json_report_has_stable_public_shape(tmp_path: Path) -> None:
    report = collect_diagnostics(_config(tmp_path / "nika_core.db"))

    payload = json.loads(report.to_json())

    assert payload["schema_version"] == 1
    assert payload["app_version"] == "0.0.2"
    assert payload["runtime_mode"] in {"source", "packaged"}
    assert payload["status"] == "warn"
    assert all(set(item) == {"check_id", "status", "message"} for item in payload["checks"])


def test_doctor_cli_warning_exit_contract(tmp_path: Path) -> None:
    database = tmp_path / "missing" / "nika_core.db"
    environment = dict(os.environ)
    environment["NIKA_DB_PATH"] = str(database)
    command = [sys.executable, "scripts/nika_doctor.py", "--json"]

    normal = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    strict = subprocess.run(
        [*command, "--strict"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert normal.returncode == 0
    assert strict.returncode == 1
    assert json.loads(normal.stdout)["status"] == "warn"
    assert str(database) not in normal.stdout
