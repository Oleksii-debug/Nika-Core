from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from nika_core.config import AppConfig
from nika_core.data.multi_agent_state_schema import MULTI_AGENT_STATE_SCHEMA_VERSION
from nika_core.data.schema import SCHEMA_VERSION
from nika_core.product_project_schema import PRODUCT_PROJECT_SCHEMA_VERSION

_SQLITE_DIAGNOSTIC_SECONDS = 2.0
_SQLITE_PROGRESS_OPCODES = 1000
_PUBLIC_TEXT_LIMIT = 96


class CheckStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    check_id: str
    status: CheckStatus
    message: str


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    app_version: str
    runtime_mode: str
    checks: tuple[DiagnosticCheck, ...]

    @property
    def status(self) -> CheckStatus:
        states = {item.status for item in self.checks}
        if CheckStatus.FAIL in states:
            return CheckStatus.FAIL
        if CheckStatus.WARN in states:
            return CheckStatus.WARN
        return CheckStatus.PASS

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "app_version": self.app_version,
            "runtime_mode": self.runtime_mode,
            "status": self.status.value,
            "checks": [
                {**asdict(item), "status": item.status.value}
                for item in self.checks
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    def exit_code(self, *, strict: bool = False) -> int:
        if self.status is CheckStatus.FAIL:
            return 2
        if strict and self.status is CheckStatus.WARN:
            return 1
        return 0

    def to_text(self) -> str:
        lines = [
            f"Nika Core diagnostics: {self.status.value.upper()}",
            f"Version: {self.app_version}",
            f"Runtime: {self.runtime_mode}",
        ]
        for item in self.checks:
            lines.append(f"{item.status.value.upper()}: {item.check_id}: {item.message}")
        return "\n".join(lines)


def _safe_public_text(value: object) -> str:
    text = str(value)
    safe = "".join(character if character.isprintable() else " " for character in text)
    normalized = " ".join(safe.split())
    if len(normalized) <= _PUBLIC_TEXT_LIMIT:
        return normalized
    return normalized[: _PUBLIC_TEXT_LIMIT - 3] + "..."


def _python_check() -> DiagnosticCheck:
    version = (sys.version_info.major, sys.version_info.minor)
    supported = (3, 12) <= version < (3, 14)
    return DiagnosticCheck(
        "python",
        CheckStatus.PASS if supported else CheckStatus.FAIL,
        (
            f"Python {version[0]}.{version[1]} runtime is supported."
            if supported
            else f"Python {version[0]}.{version[1]} runtime is outside supported 3.12-3.13."
        ),
    )


def _schema_check(
    connection: sqlite3.Connection,
    *,
    table: str,
    supported: int,
    check_id: str,
) -> DiagnosticCheck:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if exists is None:
        return DiagnosticCheck(
            check_id,
            CheckStatus.FAIL,
            "Required schema migration history is missing.",
        )
    row = connection.execute(f"SELECT MAX(version) FROM {table}").fetchone()
    current = int(row[0] or 0) if row else 0
    if current > supported:
        return DiagnosticCheck(
            check_id,
            CheckStatus.FAIL,
            f"Database schema version {current} is newer than supported {supported}.",
        )
    if current < supported:
        return DiagnosticCheck(
            check_id,
            CheckStatus.WARN,
            f"Database schema version {current} can be upgraded to supported {supported}.",
        )
    return DiagnosticCheck(
        check_id,
        CheckStatus.PASS,
        f"Database schema version {current} matches supported {supported}.",
    )


def _database_checks(path: Path) -> Iterable[DiagnosticCheck]:
    parent = path.parent
    if not parent.exists():
        yield DiagnosticCheck(
            "data_directory",
            CheckStatus.WARN,
            "Data directory does not exist yet; first normal startup may create it.",
        )
        yield DiagnosticCheck(
            "database",
            CheckStatus.WARN,
            "Database does not exist yet.",
        )
        return
    if not parent.is_dir():
        yield DiagnosticCheck(
            "data_directory",
            CheckStatus.FAIL,
            "Configured data-directory location is not a directory.",
        )
        return
    writable = os.access(parent, os.W_OK)
    yield DiagnosticCheck(
        "data_directory",
        CheckStatus.PASS if writable else CheckStatus.FAIL,
        "Data directory is writable." if writable else "Data directory is not writable.",
    )
    if path.is_symlink():
        yield DiagnosticCheck(
            "database",
            CheckStatus.FAIL,
            "Database path is not a safe regular file.",
        )
        return
    if not path.exists():
        yield DiagnosticCheck(
            "database",
            CheckStatus.WARN,
            "Database does not exist yet.",
        )
        return
    if not path.is_file():
        yield DiagnosticCheck(
            "database",
            CheckStatus.FAIL,
            "Database path is not a safe regular file.",
        )
        return
    try:
        uri = f"{path.absolute().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as connection:
            deadline = time.monotonic() + _SQLITE_DIAGNOSTIC_SECONDS
            connection.set_progress_handler(
                lambda: int(time.monotonic() >= deadline),
                _SQLITE_PROGRESS_OPCODES,
            )
            result = connection.execute("PRAGMA quick_check(1)").fetchone()
            tables = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
            ).fetchone()
            schema_checks = (
                _schema_check(
                    connection,
                    table="schema_migrations",
                    supported=SCHEMA_VERSION,
                    check_id="core_schema",
                ),
                _schema_check(
                    connection,
                    table="multi_agent_state_schema_migrations",
                    supported=MULTI_AGENT_STATE_SCHEMA_VERSION,
                    check_id="multi_agent_schema",
                ),
                _schema_check(
                    connection,
                    table="product_project_schema_migrations",
                    supported=PRODUCT_PROJECT_SCHEMA_VERSION,
                    check_id="product_project_schema",
                ),
            )
            connection.set_progress_handler(None, 0)
    except (OSError, sqlite3.Error, ValueError):
        yield DiagnosticCheck(
            "database",
            CheckStatus.FAIL,
            "Database could not be opened read-only or failed bounded integrity inspection.",
        )
        return
    if result != ("ok",):
        yield DiagnosticCheck(
            "database",
            CheckStatus.FAIL,
            "Database integrity check did not return OK.",
        )
        return
    table_count = int(tables[0]) if tables else 0
    yield DiagnosticCheck(
        "database",
        CheckStatus.PASS,
        f"Database opens read-only and passes integrity check; tables={table_count}.",
    )
    yield from schema_checks


def collect_diagnostics(config: AppConfig | None = None) -> DiagnosticReport:
    runtime_mode = "packaged" if getattr(sys, "frozen", False) else "source"
    if config is None:
        try:
            config = AppConfig()
        except ValidationError:
            return DiagnosticReport(
                app_version="unknown",
                runtime_mode=runtime_mode,
                checks=(
                    DiagnosticCheck(
                        "configuration",
                        CheckStatus.FAIL,
                        "Configuration is invalid. Review NIKA_* settings.",
                    ),
                ),
            )

    checks: list[DiagnosticCheck] = [
        DiagnosticCheck(
            "configuration",
            CheckStatus.PASS,
            "Configuration loaded and validated.",
        ),
        _python_check(),
    ]
    checks.extend(_database_checks(config.database_path))
    checks.append(
        DiagnosticCheck(
            "model_provider",
            CheckStatus.PASS,
            f"Configured model provider: {_safe_public_text(config.model_provider)}.",
        )
    )
    return DiagnosticReport(
        app_version=_safe_public_text(config.app_version),
        runtime_mode=runtime_mode,
        checks=tuple(checks),
    )
