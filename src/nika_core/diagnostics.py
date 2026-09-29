from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Iterable

from nika_core.config import AppConfig


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

    def to_text(self) -> str:
        lines = [
            f"Nika Core diagnostics: {self.status.value.upper()}",
            f"Version: {self.app_version}",
            f"Runtime: {self.runtime_mode}",
        ]
        for item in self.checks:
            lines.append(f"{item.status.value.upper()}: {item.check_id}: {item.message}")
        return "\n".join(lines)


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
    if not path.exists():
        yield DiagnosticCheck(
            "database",
            CheckStatus.WARN,
            "Database does not exist yet.",
        )
        return
    if not path.is_file() or path.is_symlink():
        yield DiagnosticCheck(
            "database",
            CheckStatus.FAIL,
            "Database path is not a safe regular file.",
        )
        return
    try:
        uri = f"{path.absolute().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2.0) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
            tables = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
            ).fetchone()
    except (OSError, sqlite3.Error, ValueError):
        yield DiagnosticCheck(
            "database",
            CheckStatus.FAIL,
            "Database could not be opened read-only or failed integrity inspection.",
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


def collect_diagnostics(config: AppConfig | None = None) -> DiagnosticReport:
    runtime_mode = "packaged" if getattr(sys, "frozen", False) else "source"
    if config is None:
        try:
            config = AppConfig()
        except Exception:
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
        DiagnosticCheck(
            "python",
            CheckStatus.PASS,
            f"Python {sys.version_info.major}.{sys.version_info.minor} runtime is available.",
        ),
    ]
    checks.extend(_database_checks(config.database_path))
    checks.append(
        DiagnosticCheck(
            "model_provider",
            CheckStatus.PASS,
            f"Configured model provider: {config.model_provider}.",
        )
    )
    return DiagnosticReport(
        app_version=config.app_version,
        runtime_mode=runtime_mode,
        checks=tuple(checks),
    )
