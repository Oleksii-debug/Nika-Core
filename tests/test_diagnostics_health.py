from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.schema import SCHEMA_VERSION
from nika_core.diagnostics import HealthService, HealthStatus
from nika_core.diagnostics import __main__ as diagnostics_cli
from nika_core.product_project_schema import PRODUCT_PROJECT_SCHEMA_VERSION
from nika_core.resources.contracts import ResourceSnapshot

_FIXED_NOW = datetime(2026, 8, 26, 20, 0, tzinfo=UTC)
_SECRET_CANARY = "nika-health-secret-canary"


def _write_minimal_healthy_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'now')",
            ((version,) for version in range(1, SCHEMA_VERSION + 1)),
        )
        conn.execute(
            "CREATE TABLE product_project_schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO product_project_schema_migrations(version, applied_at) "
            "VALUES (?, 'now')",
            ((version,) for version in range(1, PRODUCT_PROJECT_SCHEMA_VERSION + 1)),
        )


def _config(path: Path, *, model_provider: str = "mock", schema_version: int = 1) -> AppConfig:
    return AppConfig(
        database_path=path,
        model_provider=model_provider,
        schema_version=schema_version,
    )


class _StaticObserver:
    def __init__(self, snapshot: object) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> object:
        return self._snapshot


class _FailingObserver:
    def snapshot(self) -> ResourceSnapshot:
        raise RuntimeError(f"Bearer {_SECRET_CANARY}")


def _run(
    config: AppConfig,
    observer: object | None,
):
    return HealthService(
        config,
        resource_observer=observer,  # type: ignore[arg-type]
        clock=lambda: _FIXED_NOW,
    ).run()


def _check_map(report) -> dict[str, HealthStatus]:
    return {check.check_id: check.status for check in report.checks}


def test_healthy_snapshot_is_pass_and_hides_provider_identity(tmp_path: Path) -> None:
    database = tmp_path / "дані Nika" / "core health.db"
    _write_minimal_healthy_database(database)
    provider = f"https://user:{_SECRET_CANARY}@example.invalid/v1"
    observer = _StaticObserver(ResourceSnapshot(12.5, 34.0, 4 * 1024 * 1024 * 1024))

    report = _run(_config(database, model_provider=provider), observer)

    assert report.overall is HealthStatus.PASS
    assert report.exit_code == 0
    assert all(status is HealthStatus.PASS for status in _check_map(report).values())
    rendered = report.render_text()
    payload = json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True)
    assert _SECRET_CANARY not in rendered
    assert _SECRET_CANARY not in payload
    assert str(database) not in rendered
    assert report.as_dict()["schema"] == "nika-health-report:v1"


def test_missing_database_fails_without_creating_it(tmp_path: Path) -> None:
    database = tmp_path / "missing" / "nika core.db"

    report = _run(_config(database), None)

    assert report.overall is HealthStatus.FAIL
    assert report.exit_code == 2
    assert _check_map(report)["database.present"] is HealthStatus.FAIL
    assert not database.exists()
    assert not database.parent.exists()


def test_resource_observer_is_optional_and_reports_warning(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    _write_minimal_healthy_database(database)

    report = _run(_config(database), None)

    assert report.overall is HealthStatus.WARN
    assert report.exit_code == 1
    assert _check_map(report)["resources.observer"] is HealthStatus.WARN


def test_resource_observer_exception_does_not_escape_secret_text(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    _write_minimal_healthy_database(database)

    report = _run(_config(database), _FailingObserver())

    assert report.overall is HealthStatus.WARN
    assert _check_map(report)["resources.observer"] is HealthStatus.WARN
    assert _SECRET_CANARY not in report.render_text()
    assert _SECRET_CANARY not in json.dumps(report.as_dict(), ensure_ascii=False)


@pytest.mark.parametrize(
    "snapshot",
    [
        ResourceSnapshot(float("nan"), 20.0, 1),
        ResourceSnapshot(10.0, float("inf"), 1),
        ResourceSnapshot(101.0, 20.0, 1),
        ResourceSnapshot(10.0, -1.0, 1),
        ResourceSnapshot(True, 20.0, 1),
        ResourceSnapshot(10.0, False, 1),
        ResourceSnapshot(10.0, 20.0, -1),
        ResourceSnapshot(10.0, 20.0, True),
    ],
)
def test_invalid_resource_measurements_fail_closed(tmp_path: Path, snapshot: object) -> None:
    database = tmp_path / "nika.db"
    _write_minimal_healthy_database(database)

    report = _run(_config(database), _StaticObserver(snapshot))

    assert report.overall is HealthStatus.FAIL
    assert _check_map(report)["resources.observer"] is HealthStatus.FAIL


def test_future_core_schema_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    _write_minimal_healthy_database(database)
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'future')",
            (SCHEMA_VERSION + 1,),
        )

    report = _run(
        _config(database),
        _StaticObserver(ResourceSnapshot(10.0, 20.0, 1024)),
    )

    assert _check_map(report)["database.schema.core"] is HealthStatus.FAIL
    assert report.overall is HealthStatus.FAIL


def test_missing_product_project_migration_history_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    _write_minimal_healthy_database(database)
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TABLE product_project_schema_migrations")

    report = _run(
        _config(database),
        _StaticObserver(ResourceSnapshot(10.0, 20.0, 1024)),
    )

    assert _check_map(report)["database.schema.product-project"] is HealthStatus.FAIL
    assert report.overall is HealthStatus.FAIL


def test_non_integer_migration_storage_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    _write_minimal_healthy_database(database)
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TABLE schema_migrations")
        conn.execute("CREATE TABLE schema_migrations (version, applied_at TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'now')",
            ((str(version),) for version in range(1, SCHEMA_VERSION + 1)),
        )

    report = _run(
        _config(database),
        _StaticObserver(ResourceSnapshot(10.0, 20.0, 1024)),
    )

    assert _check_map(report)["database.schema.core"] is HealthStatus.FAIL


def test_unsupported_configuration_schema_fails_without_echoing_provider(tmp_path: Path) -> None:
    database = tmp_path / "nika.db"
    _write_minimal_healthy_database(database)
    provider = f"local://{_SECRET_CANARY}"

    report = _run(
        _config(database, model_provider=provider, schema_version=2),
        _StaticObserver(ResourceSnapshot(10.0, 20.0, 1024)),
    )

    assert _check_map(report)["configuration"] is HealthStatus.FAIL
    assert _SECRET_CANARY not in report.render_text()


def test_cli_json_output_is_machine_readable_and_returns_health_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "nika.db"
    _write_minimal_healthy_database(database)
    config = _config(database)
    observer = _StaticObserver(ResourceSnapshot(10.0, 20.0, 1024))
    monkeypatch.setattr(
        diagnostics_cli.AppConfig,
        "from_environment",
        classmethod(lambda cls: config),
    )
    monkeypatch.setattr(diagnostics_cli, "_resource_observer", lambda: observer)

    exit_code = diagnostics_cli.main(["--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["schema"] == "nika-health-report:v1"
    assert payload["overall"] == "pass"


def test_cli_configuration_failure_uses_stable_sanitized_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _fail(cls):
        raise ValueError(f"bad env {_SECRET_CANARY}")

    monkeypatch.setattr(
        diagnostics_cli.AppConfig,
        "from_environment",
        classmethod(_fail),
    )

    exit_code = diagnostics_cli.main([])

    output = capsys.readouterr().out
    assert exit_code == 2
    assert "Nika Core health: FAIL" in output
    assert _SECRET_CANARY not in output
