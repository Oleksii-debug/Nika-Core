from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from nika_core.data.schema import MIGRATIONS, SCHEMA_VERSION
from nika_core.m3_extension_schema import (
    M3_EXTENSION_MIGRATIONS,
    M3_EXTENSION_SCHEMA_VERSION,
)
from nika_core.product_project_schema import (
    PRODUCT_PROJECT_MIGRATIONS,
    PRODUCT_PROJECT_SCHEMA_VERSION,
)


class SQLiteStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            row = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
            current = int(row["version"] or 0)
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {current} is newer than supported schema {SCHEMA_VERSION}"
                )
            for version in range(current + 1, SCHEMA_VERSION + 1):
                statements = MIGRATIONS.get(version)
                if statements is None:
                    raise RuntimeError(f"missing migration {version}")
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, datetime.now(UTC).isoformat()),
                )
            self._initialize_m3_extension_schema(conn)
            self._initialize_product_project_schema(conn)

    @staticmethod
    def _initialize_m3_extension_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS m3_extension_schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        row = conn.execute(
            "SELECT MAX(version) AS version FROM m3_extension_schema_migrations"
        ).fetchone()
        current = int(row["version"] or 0)
        if current > M3_EXTENSION_SCHEMA_VERSION:
            raise RuntimeError(
                "M3 extension database schema "
                f"{current} is newer than supported schema {M3_EXTENSION_SCHEMA_VERSION}"
            )
        for version in range(current + 1, M3_EXTENSION_SCHEMA_VERSION + 1):
            statements = M3_EXTENSION_MIGRATIONS.get(version)
            if statements is None:
                raise RuntimeError(f"missing M3 extension migration {version}")
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO m3_extension_schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, datetime.now(UTC).isoformat()),
            )

    @staticmethod
    def _initialize_product_project_schema(conn: sqlite3.Connection) -> None:
        """Apply the independently-owned PF schema without editing reserved research migrations."""
        conn.execute(
            "CREATE TABLE IF NOT EXISTS product_project_schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        row = conn.execute(
            "SELECT MAX(version) AS version FROM product_project_schema_migrations"
        ).fetchone()
        current = int(row["version"] or 0)
        if current > PRODUCT_PROJECT_SCHEMA_VERSION:
            raise RuntimeError(
                "product project database schema "
                f"{current} is newer than supported schema {PRODUCT_PROJECT_SCHEMA_VERSION}"
            )
        for version in range(current + 1, PRODUCT_PROJECT_SCHEMA_VERSION + 1):
            statements = PRODUCT_PROJECT_MIGRATIONS.get(version)
            if statements is None:
                raise RuntimeError(f"missing product project migration {version}")
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO product_project_schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, datetime.now(UTC).isoformat()),
            )

    def schema_version(self) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
        return int(row["version"] or 0)
