from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from nika_core.config import AppConfig
from nika_core.data.schema import SCHEMA_VERSION
from nika_core.product_project_schema import PRODUCT_PROJECT_SCHEMA_VERSION
from nika_core.resources.contracts import ResourceObserverPort, ResourceSnapshot

SUPPORTED_CONFIG_SCHEMA_VERSION = 1


class HealthStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


_STATUS_RANK = {
    HealthStatus.PASS: 0,
    HealthStatus.WARN: 1,
    HealthStatus.FAIL: 2,
}


@dataclass(frozen=True, slots=True)
class HealthCheck:
    check_id: str
    status: HealthStatus
    summary: str

    def as_dict(self) -> dict[str, str]:
        return {
            "check_id": self.check_id,
            "status": self.status.value,
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class HealthReport:
    generated_at: datetime
    checks: tuple[HealthCheck, ...]

    @property
    def overall(self) -> HealthStatus:
        if not self.checks:
            return HealthStatus.FAIL
        return max(self.checks, key=lambda item: _STATUS_RANK[item.status]).status

    @property
    def exit_code(self) -> int:
        return _STATUS_RANK[self.overall]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "nika-health-report:v1",
            "generated_at": self.generated_at.astimezone(UTC).isoformat(),
            "overall": self.overall.value,
            "checks": [check.as_dict() for check in self.checks],
        }

    def render_text(self) -> str:
        lines = [
            f"Nika Core health: {self.overall.value.upper()}",
            f"Checked at: {self.generated_at.astimezone(UTC).isoformat()}",
        ]
        for index, check in enumerate(self.checks, start=1):
            lines.append(
                f"{index}. {check.status.value.upper()}: {check.check_id}. {check.summary}"
            )
        return "\n".join(lines)


class HealthService:
    """Read-only deterministic health aggregation over canonical Nika Core surfaces."""

    def __init__(
        self,
        config: AppConfig,
        *,
        resource_observer: ResourceObserverPort | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._resource_observer = resource_observer
        self._clock = clock or (lambda: datetime.now(UTC))

    def run(self) -> HealthReport:
        checks = [self._check_configuration()]
        checks.extend(self._check_database())
        checks.append(self._check_resources())
        return HealthReport(generated_at=self._normalized_now(), checks=tuple(checks))

    def _normalized_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("health clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _check_configuration(self) -> HealthCheck:
        # The provider value is intentionally not echoed because AppConfig accepts arbitrary
        # provider identifiers, and a malformed identifier may contain credential material.
        if self._config.schema_version != SUPPORTED_CONFIG_SCHEMA_VERSION:
            return HealthCheck(
                check_id="configuration",
                status=HealthStatus.FAIL,
                summary="Application configuration schema is not supported by this build.",
            )
        if not self._config.app_version.strip():
            return HealthCheck(
                check_id="configuration",
                status=HealthStatus.FAIL,
                summary="Application version is empty.",
            )
        return HealthCheck(
            check_id="configuration",
            status=HealthStatus.PASS,
            summary=(
                "Typed configuration loaded; provider identity is present but intentionally hidden."
            ),
        )

    def _check_database(self) -> list[HealthCheck]:
        path = Path(self._config.database_path)
        if not path.exists():
            return [
                HealthCheck(
                    check_id="database.present",
                    status=HealthStatus.FAIL,
                    summary="Canonical SQLite database file does not exist.",
                )
            ]
        if not path.is_file():
            return [
                HealthCheck(
                    check_id="database.present",
                    status=HealthStatus.FAIL,
                    summary="Configured SQLite database path is not a regular file.",
                )
            ]

        checks = [
            HealthCheck(
                check_id="database.present",
                status=HealthStatus.PASS,
                summary="Canonical SQLite database file is present.",
            )
        ]
        try:
            with self._read_only_connection(path) as conn:
                checks.append(self._check_database_integrity(conn))
                checks.append(self._check_foreign_keys(conn))
                checks.append(
                    self._check_migration_history(
                        conn,
                        query=(
                            "SELECT version, typeof(version) FROM schema_migrations "
                            "ORDER BY version LIMIT ?"
                        ),
                        supported_version=SCHEMA_VERSION,
                        check_id="database.schema.core",
                    )
                )
                checks.append(
                    self._check_migration_history(
                        conn,
                        query=(
                            "SELECT version, typeof(version) "
                            "FROM product_project_schema_migrations "
                            "ORDER BY version LIMIT ?"
                        ),
                        supported_version=PRODUCT_PROJECT_SCHEMA_VERSION,
                        check_id="database.schema.product-project",
                    )
                )
        except (OSError, sqlite3.Error):
            checks.append(
                HealthCheck(
                    check_id="database.open",
                    status=HealthStatus.FAIL,
                    summary="SQLite database could not be opened safely in read-only mode.",
                )
            )
        return checks

    @staticmethod
    def _read_only_connection(path: Path) -> sqlite3.Connection:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.execute("PRAGMA query_only = ON")
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    @staticmethod
    def _check_database_integrity(conn: sqlite3.Connection) -> HealthCheck:
        rows = tuple(row[0] for row in conn.execute("PRAGMA quick_check"))
        if rows == ("ok",):
            return HealthCheck(
                check_id="database.integrity",
                status=HealthStatus.PASS,
                summary="SQLite quick_check passed.",
            )
        return HealthCheck(
            check_id="database.integrity",
            status=HealthStatus.FAIL,
            summary="SQLite quick_check reported corruption or structural inconsistency.",
        )

    @staticmethod
    def _check_foreign_keys(conn: sqlite3.Connection) -> HealthCheck:
        row = conn.execute("PRAGMA foreign_key_check").fetchone()
        if row is None:
            return HealthCheck(
                check_id="database.foreign-keys",
                status=HealthStatus.PASS,
                summary="SQLite foreign-key integrity passed.",
            )
        return HealthCheck(
            check_id="database.foreign-keys",
            status=HealthStatus.FAIL,
            summary="SQLite foreign-key integrity violation detected.",
        )

    @staticmethod
    def _check_migration_history(
        conn: sqlite3.Connection,
        *,
        query: str,
        supported_version: int,
        check_id: str,
    ) -> HealthCheck:
        try:
            rows = tuple(conn.execute(query, (supported_version + 1,)))
        except sqlite3.Error:
            return HealthCheck(
                check_id=check_id,
                status=HealthStatus.FAIL,
                summary="Required migration history is missing or unreadable.",
            )

        expected = tuple(range(1, supported_version + 1))
        versions = tuple(row[0] for row in rows)
        storage_types = tuple(row[1] for row in rows)
        if versions == expected and all(value == "integer" for value in storage_types):
            return HealthCheck(
                check_id=check_id,
                status=HealthStatus.PASS,
                summary=(
                    "Migration history is contiguous through supported version "
                    f"{supported_version}."
                ),
            )
        return HealthCheck(
            check_id=check_id,
            status=HealthStatus.FAIL,
            summary=(
                "Migration history is missing, non-integer, non-contiguous, "
                "or newer than this build."
            ),
        )

    def _check_resources(self) -> HealthCheck:
        observer = self._resource_observer
        if observer is None:
            return HealthCheck(
                check_id="resources.observer",
                status=HealthStatus.WARN,
                summary=(
                    "Optional resource observer is not configured; "
                    "no system load claim is made."
                ),
            )
        try:
            snapshot = observer.snapshot()
        except Exception:
            # Provider-controlled diagnostics may contain credentials and must not cross
            # this public boundary.
            return HealthCheck(
                check_id="resources.observer",
                status=HealthStatus.WARN,
                summary=(
                    "Resource observer failed; provider diagnostics were intentionally omitted."
                ),
            )
        try:
            snapshot_valid = self._valid_snapshot(snapshot)
        except (AttributeError, TypeError, ValueError, OverflowError):
            snapshot_valid = False
        if not snapshot_valid:
            return HealthCheck(
                check_id="resources.observer",
                status=HealthStatus.FAIL,
                summary="Resource observer returned invalid or non-finite measurements.",
            )
        available_mib = snapshot.available_memory_bytes // (1024 * 1024)
        return HealthCheck(
            check_id="resources.observer",
            status=HealthStatus.PASS,
            summary=(
                f"Resource observation valid: CPU {snapshot.cpu_percent:.1f}%, "
                f"memory {snapshot.memory_percent:.1f}%, available memory {available_mib} MiB."
            ),
        )

    @classmethod
    def _valid_snapshot(cls, snapshot: ResourceSnapshot) -> bool:
        return (
            cls._valid_percent(snapshot.cpu_percent)
            and cls._valid_percent(snapshot.memory_percent)
            and isinstance(snapshot.available_memory_bytes, int)
            and not isinstance(snapshot.available_memory_bytes, bool)
            and snapshot.available_memory_bytes >= 0
        )

    @staticmethod
    def _valid_percent(value: object) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and 0.0 <= float(value) <= 100.0
        )
