from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from nika_core.data.experience_ledger_schema import (
    EXPERIENCE_LEDGER_MIGRATIONS,
    EXPERIENCE_LEDGER_SCHEMA_VERSION,
)
from nika_core.data.multi_agent_state_schema import (
    MULTI_AGENT_STATE_MIGRATIONS,
    MULTI_AGENT_STATE_SCHEMA_VERSION,
)
from nika_core.data.schema import MIGRATIONS, SCHEMA_VERSION
from nika_core.model_artifact_schema import (
    MODEL_ARTIFACT_MIGRATIONS,
    MODEL_ARTIFACT_SCHEMA_VERSION,
)
from nika_core.product_project_schema import (
    PRODUCT_PROJECT_MIGRATIONS,
    PRODUCT_PROJECT_SCHEMA_VERSION,
)
from nika_core.research.knowledge_schema import initialize_knowledge_schema


class SQLiteStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._database_identity = (
            None
            if str(self.path) == ":memory:"
            else self.path.expanduser().resolve(strict=False)
        )
        self._active_connections: dict[int, sqlite3.Connection] = {}

    def require_connection(self, conn: sqlite3.Connection) -> None:
        """Fail closed unless conn is bound to this store's main database."""
        if type(conn) is not sqlite3.Connection:
            raise TypeError("conn must be an exact sqlite3.Connection")
        if self._active_connections.get(id(conn)) is not conn:
            raise ValueError("connection was not opened by this SQLiteStore")
        rows = conn.execute("PRAGMA database_list").fetchall()
        main_rows = tuple(row for row in rows if row[1] == "main")
        if len(main_rows) != 1:
            raise ValueError("connection does not expose one canonical main database")
        database_file = main_rows[0][2]
        if self._database_identity is None:
            if database_file != "":
                raise ValueError("connection does not belong to this SQLiteStore")
            return
        if not database_file:
            raise ValueError("connection does not belong to this SQLiteStore")
        actual_identity = Path(database_file).expanduser().resolve(strict=False)
        if actual_identity != self._database_identity:
            raise ValueError("connection does not belong to this SQLiteStore")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        connection_id = id(conn)
        self._active_connections[connection_id] = conn
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            if self._active_connections.get(connection_id) is conn:
                del self._active_connections[connection_id]
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
            self._initialize_experience_ledger_schema(conn)
            self._initialize_multi_agent_state_schema(conn)
            self._initialize_product_project_schema(conn)
            self._initialize_model_artifact_schema(conn)
            initialize_knowledge_schema(conn)

    @staticmethod
    def _initialize_experience_ledger_schema(conn: sqlite3.Connection) -> None:
        """Apply continuity Experience Ledger migrations through the canonical store."""
        conn.execute(
            "CREATE TABLE IF NOT EXISTS experience_ledger_schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        row = conn.execute(
            "SELECT MAX(version) AS version FROM experience_ledger_schema_migrations"
        ).fetchone()
        current = int(row["version"] or 0)
        if current > EXPERIENCE_LEDGER_SCHEMA_VERSION:
            raise RuntimeError(
                "experience ledger database schema "
                f"{current} is newer than supported schema {EXPERIENCE_LEDGER_SCHEMA_VERSION}"
            )
        for version in range(current + 1, EXPERIENCE_LEDGER_SCHEMA_VERSION + 1):
            statements = EXPERIENCE_LEDGER_MIGRATIONS.get(version)
            if statements is None:
                raise RuntimeError(f"missing experience ledger migration {version}")
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO experience_ledger_schema_migrations(version, applied_at) "
                "VALUES (?, ?)",
                (version, datetime.now(UTC).isoformat()),
            )

    @staticmethod
    def _initialize_multi_agent_state_schema(conn: sqlite3.Connection) -> None:
        """Apply the ordered V0.1 member-state extension through the canonical store."""
        conn.execute(
            "CREATE TABLE IF NOT EXISTS multi_agent_state_schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        row = conn.execute(
            "SELECT MAX(version) AS version FROM multi_agent_state_schema_migrations"
        ).fetchone()
        current = int(row["version"] or 0)
        if current > MULTI_AGENT_STATE_SCHEMA_VERSION:
            raise RuntimeError(
                "multi-agent state database schema "
                f"{current} is newer than supported schema {MULTI_AGENT_STATE_SCHEMA_VERSION}"
            )
        legacy_members_exist = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'multi_agent_members'"
            ).fetchone()
            is not None
        )
        if not legacy_members_exist:
            return
        for version in range(current + 1, MULTI_AGENT_STATE_SCHEMA_VERSION + 1):
            statements = MULTI_AGENT_STATE_MIGRATIONS.get(version)
            if statements is None:
                raise RuntimeError(f"missing multi-agent state migration {version}")
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO multi_agent_state_schema_migrations(version, applied_at) "
                "VALUES (?, ?)",
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

    @staticmethod
    def _initialize_model_artifact_schema(conn: sqlite3.Connection) -> None:
        """Apply provider-neutral model provenance migrations through the canonical store."""
        conn.execute(
            "CREATE TABLE IF NOT EXISTS model_artifact_schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        row = conn.execute(
            "SELECT MAX(version) AS version FROM model_artifact_schema_migrations"
        ).fetchone()
        current = int(row["version"] or 0)
        if current > MODEL_ARTIFACT_SCHEMA_VERSION:
            raise RuntimeError(
                "model artifact database schema "
                f"{current} is newer than supported schema {MODEL_ARTIFACT_SCHEMA_VERSION}"
            )
        for version in range(current + 1, MODEL_ARTIFACT_SCHEMA_VERSION + 1):
            statements = MODEL_ARTIFACT_MIGRATIONS.get(version)
            if statements is None:
                raise RuntimeError(f"missing model artifact migration {version}")
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO model_artifact_schema_migrations(version, applied_at) "
                "VALUES (?, ?)",
                (version, datetime.now(UTC).isoformat()),
            )

    def schema_version(self) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
        return int(row["version"] or 0)

    def knowledge_schema_version(self) -> int:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS version FROM knowledge_schema_migrations"
            ).fetchone()
        return int(row["version"] or 0)
