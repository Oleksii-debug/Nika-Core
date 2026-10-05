from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core import app


def _forbid_startup() -> None:
    raise AssertionError("offline CLI options must not read settings or open SQLite")


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_does_not_initialize_settings_or_database(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flag: str,
) -> None:
    monkeypatch.setattr(app, "AppConfig", SimpleNamespace(from_environment=_forbid_startup))
    monkeypatch.setattr(app, "build_runtime", _forbid_startup)
    with pytest.raises(SystemExit) as caught:
        app.main([flag])
    assert caught.value.code == 0
    output = capsys.readouterr().out
    assert "--version" in output
    assert "Inspect the local Nika Core runtime." in output


def test_version_uses_installed_package_without_startup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(app, "AppConfig", SimpleNamespace(from_environment=_forbid_startup))
    monkeypatch.setattr(app, "build_runtime", _forbid_startup)
    monkeypatch.setattr(app, "version", lambda name: "1.2.3" if name == "nika-core" else "")
    assert app.main(["--version"]) == 0
    assert capsys.readouterr().out == "Nika Core 1.2.3\n"


def test_source_checkout_version_has_a_safe_fallback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(app, "AppConfig", SimpleNamespace(from_environment=_forbid_startup))
    monkeypatch.setattr(app, "build_runtime", _forbid_startup)

    def not_installed(_name: str) -> str:
        raise app.PackageNotFoundError("nika-core")

    monkeypatch.setattr(app, "version", not_installed)
    assert app.main(["--version"]) == 0
    assert capsys.readouterr().out == "Nika Core source checkout (not installed)\n"


def test_unknown_option_rejects_before_any_startup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(app, "AppConfig", SimpleNamespace(from_environment=_forbid_startup))
    monkeypatch.setattr(app, "build_runtime", _forbid_startup)
    with pytest.raises(SystemExit) as caught:
        app.main(["--not-a-real-option"])
    assert caught.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_no_arguments_preserve_existing_status_and_database_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "папка з пробілами" / "ніка.db"
    config = SimpleNamespace(app_version="0.0.2", database_path=db_path)
    seen: list[object] = []
    monkeypatch.setattr(
        app, "AppConfig", SimpleNamespace(from_environment=lambda: config)
    )

    def runtime(value: object) -> tuple[object, SimpleNamespace, SimpleNamespace]:
        seen.append(value)
        return object(), SimpleNamespace(count=2), SimpleNamespace(count_ready=3)

    monkeypatch.setattr(app, "build_runtime", runtime)
    assert app.main([]) == 0
    assert seen == [config]
    assert capsys.readouterr().out == (
        f"Nika Core 0.0.2: agents=2, queued=3, db={db_path}\n"
    )


@pytest.mark.parametrize(
    ("flag", "expected_code", "expected_text"),
    [
        ("--help", 0, "--version"),
        ("-h", 0, "--version"),
        ("--version", 0, "Nika Core "),
        ("--not-a-real-option", 2, "unrecognized arguments"),
    ],
)
def test_real_module_entrypoint_offline_options_do_not_read_config(
    tmp_path: Path,
    flag: str,
    expected_code: int,
    expected_text: str,
) -> None:
    db_path = tmp_path / "папка з пробілами" / "ніка.db"
    env = os.environ.copy()
    env["NIKA_DB_PATH"] = str(db_path)
    env["NIKA_LOG_LEVEL"] = "invalid_offline"
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-m", "nika_core", flag],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )
    assert completed.returncode == expected_code, completed.stderr
    assert expected_text in completed.stdout + completed.stderr
    assert not db_path.exists()


def test_real_default_start_creates_unicode_sqlite_database(tmp_path: Path) -> None:
    db_path = tmp_path / "папка з пробілами" / "ніка.db"
    env = os.environ.copy()
    env["NIKA_DB_PATH"] = str(db_path)
    env["NIKA_LOG_LEVEL"] = "INFO"
    env["NIKA_SCHEMA_VERSION"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-m", "nika_core"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "agents=" in completed.stdout and "queued=" in completed.stdout
    assert str(db_path) in completed.stdout
    assert db_path.is_file()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone() == (1,)
