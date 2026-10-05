from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import nika_core.app as app


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
    assert "nica" not in output.lower()


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
