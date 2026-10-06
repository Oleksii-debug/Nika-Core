from __future__ import annotations

import json
import sqlite3

from ..data.sqlite import SQLiteStore
from .accounting import AccountSnapshot
from .orders import SimulatedFill

_TRADER_SCHEMA_VERSION = 3
_FILL_EVIDENCE_COLUMNS = (
    "approval_id",
    "intent_id",
    "order_id",
    "venue_id",
    "venue_timezone",
    "instrument_id",
    "currency",
    "side",
    "quantity",
    "price",
    "fee",
    "filled_at",
    "filled_slice",
)
_FILL_COLUMN_TYPES = {
    "workspace_id": "TEXT",
    "run_id": "TEXT",
    "fill_id": "TEXT",
    "approval_id": "TEXT",
    "intent_id": "TEXT",
    "order_id": "TEXT",
    "venue_id": "TEXT",
    "venue_timezone": "TEXT",
    "instrument_id": "TEXT",
    "currency": "TEXT",
    "side": "TEXT",
    "quantity": "TEXT",
    "price": "TEXT",
    "fee": "TEXT",
    "filled_at": "TEXT",
    "filled_slice": "INTEGER",
}
_ACCOUNT_COLUMN_TYPES = {
    "workspace_id": "TEXT",
    "run_id": "TEXT",
    "payload": "TEXT",
    "last_fill_id": "TEXT",
}


class TradingStateRepository:
    """Trader-owned durable paper state inside the canonical Nika SQLite database."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def initialize(self) -> None:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS trading_research_schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            row = conn.execute(
                "SELECT MAX(version) AS version FROM trading_research_schema_migrations"
            ).fetchone()
            current = int(row["version"] or 0)
            if current > _TRADER_SCHEMA_VERSION:
                raise RuntimeError("trading research schema is newer than supported")
            if current == 0:
                if _has_unversioned_trading_rows(conn):
                    raise RuntimeError(
                        "unversioned trading state exists; refusing to invent schema authority"
                    )
                _create_v3_tables(conn)
                conn.execute(
                    "INSERT INTO trading_research_schema_migrations(version) VALUES (3)"
                )
                current = 3
            elif current in {1, 2}:
                _upgrade_empty_legacy(conn, current)
                conn.execute(
                    "INSERT INTO trading_research_schema_migrations(version) VALUES (3)"
                )
                current = 3
            if current != _TRADER_SCHEMA_VERSION:
                raise RuntimeError("unsupported trading research schema version")
            _verify_v3_schema(conn)

    def commit_fill_and_account(self, fill: SimulatedFill, snapshot: AccountSnapshot) -> bool:
        payload = _snapshot_payload(snapshot)
        workspace_id = fill.authority.workspace_id
        run_id = fill.authority.run_id
        evidence = _fill_evidence(fill)
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT approval_id, intent_id, order_id, venue_id, venue_timezone, "
                "instrument_id, currency, side, quantity, price, fee, filled_at, filled_slice "
                "FROM trading_research_run_fills "
                "WHERE workspace_id = ? AND run_id = ? AND fill_id = ?",
                (workspace_id, run_id, fill.fill_id),
            ).fetchone()
            if existing is not None:
                actual = tuple(existing[name] for name in _FILL_EVIDENCE_COLUMNS)
                if actual != evidence:
                    raise RuntimeError("conflicting durable fill identity")
                return False
            conn.execute(
                "INSERT INTO trading_research_run_fills("
                "workspace_id, run_id, fill_id, approval_id, intent_id, order_id, "
                "venue_id, venue_timezone, instrument_id, currency, side, quantity, "
                "price, fee, filled_at, filled_slice) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (workspace_id, run_id, fill.fill_id, *evidence),
            )
            conn.execute(
                "INSERT INTO trading_research_run_account_state("
                "workspace_id, run_id, payload, last_fill_id) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(workspace_id, run_id) DO UPDATE SET "
                "payload = excluded.payload, last_fill_id = excluded.last_fill_id",
                (workspace_id, run_id, payload, fill.fill_id),
            )
        return True

    def fill_count(self, workspace_id: str, run_id: str) -> int:
        _validate_scope(workspace_id, run_id)
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM trading_research_run_fills "
                "WHERE workspace_id = ? AND run_id = ?",
                (workspace_id, run_id),
            ).fetchone()
        return int(row["count"])

    def has_fill(self, workspace_id: str, run_id: str, fill_id: str) -> bool:
        _validate_scope(workspace_id, run_id)
        if type(fill_id) is not str or not fill_id.strip():
            raise ValueError("fill_id must be nonblank text")
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM trading_research_run_fills "
                "WHERE workspace_id = ? AND run_id = ? AND fill_id = ?",
                (workspace_id, run_id, fill_id),
            ).fetchone()
        return row is not None

    def account_payload(self, workspace_id: str, run_id: str) -> dict[str, object] | None:
        _validate_scope(workspace_id, run_id)
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT payload FROM trading_research_run_account_state "
                "WHERE workspace_id = ? AND run_id = ?",
                (workspace_id, run_id),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(str(row["payload"]))
        if not isinstance(value, dict):
            raise TypeError("invalid durable trading account payload")
        return value




def _fill_evidence(fill: SimulatedFill) -> tuple[object, ...]:
    return (
        fill.approval_id,
        fill.intent_id,
        fill.authority.order_id,
        fill.instrument.venue.venue_id,
        fill.instrument.venue.timezone,
        fill.instrument.instrument_id,
        fill.instrument.currency,
        fill.side.value,
        str(fill.quantity),
        str(fill.price),
        str(fill.fee),
        fill.filled_at.isoformat(),
        fill.filled_slice,
    )




def _has_unversioned_trading_rows(conn: sqlite3.Connection) -> bool:
    for table in (
        "trading_research_fills",
        "trading_research_account_state",
        "trading_research_run_fills",
        "trading_research_run_account_state",
    ):
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if row is None:
            continue
        data = conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        if data is not None:
            return True
    return False


def _create_v3_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS trading_research_run_fills ("
        "workspace_id TEXT NOT NULL, run_id TEXT NOT NULL, fill_id TEXT NOT NULL, "
        "approval_id TEXT NOT NULL, intent_id TEXT NOT NULL, order_id TEXT NOT NULL, "
        "venue_id TEXT NOT NULL, venue_timezone TEXT NOT NULL, instrument_id TEXT NOT NULL, "
        "currency TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL, "
        "price TEXT NOT NULL, fee TEXT NOT NULL, filled_at TEXT NOT NULL, "
        "filled_slice INTEGER NOT NULL, "
        "PRIMARY KEY(workspace_id, run_id, fill_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS trading_research_run_account_state ("
        "workspace_id TEXT NOT NULL, run_id TEXT NOT NULL, payload TEXT NOT NULL, "
        "last_fill_id TEXT NOT NULL, PRIMARY KEY(workspace_id, run_id))"
    )


def _upgrade_empty_legacy(conn: sqlite3.Connection, current: int) -> None:
    fill_row = conn.execute(
        "SELECT COUNT(*) AS count FROM trading_research_fills"
    ).fetchone()
    account_row = conn.execute(
        "SELECT COUNT(*) AS count FROM trading_research_account_state"
    ).fetchone()
    if int(fill_row["count"]) or int(account_row["count"]):
        missing = "venue/run identity" if current == 1 else "workspace/run identity"
        raise RuntimeError(
            f"legacy trading state lacks {missing}; export/reset it before upgrade"
        )
    _create_v3_tables(conn)


def _verify_v3_schema(conn: sqlite3.Connection) -> None:
    fill_rows = conn.execute(
        "PRAGMA table_info(trading_research_run_fills)"
    ).fetchall()
    required_fill = set(_FILL_COLUMN_TYPES)
    if _column_names(fill_rows) != required_fill:
        raise RuntimeError("invalid trading research run fill schema")
    if _column_types(fill_rows) != _FILL_COLUMN_TYPES:
        raise RuntimeError("invalid trading research run fill column types")
    if _primary_key_columns(fill_rows) != ("workspace_id", "run_id", "fill_id"):
        raise RuntimeError("invalid trading research run fill primary key")
    if _not_null_columns(fill_rows) != required_fill:
        raise RuntimeError("invalid nullable trading research run fill column")

    account_rows = conn.execute(
        "PRAGMA table_info(trading_research_run_account_state)"
    ).fetchall()
    required_account = set(_ACCOUNT_COLUMN_TYPES)
    if _column_names(account_rows) != required_account:
        raise RuntimeError("invalid trading research run account schema")
    if _column_types(account_rows) != _ACCOUNT_COLUMN_TYPES:
        raise RuntimeError("invalid trading research run account column types")
    if _primary_key_columns(account_rows) != ("workspace_id", "run_id"):
        raise RuntimeError("invalid trading research run account primary key")
    if _not_null_columns(account_rows) != required_account:
        raise RuntimeError("invalid nullable trading research run account column")




def _column_names(rows: list[sqlite3.Row]) -> set[str]:
    return {str(row["name"]) for row in rows}


def _column_types(rows: list[sqlite3.Row]) -> dict[str, str]:
    return {str(row["name"]): str(row["type"]).upper() for row in rows}


def _primary_key_columns(rows: list[sqlite3.Row]) -> tuple[str, ...]:
    keyed = sorted(
        ((int(row["pk"]), str(row["name"])) for row in rows if int(row["pk"]) > 0),
        key=lambda item: item[0],
    )
    return tuple(name for _, name in keyed)


def _not_null_columns(rows: list[sqlite3.Row]) -> set[str]:
    return {str(row["name"]) for row in rows if int(row["notnull"]) == 1}


def _validate_scope(workspace_id: str, run_id: str) -> None:
    if type(workspace_id) is not str or not workspace_id.strip():
        raise ValueError("workspace_id must be nonblank text")
    if type(run_id) is not str or not run_id.strip():
        raise ValueError("run_id must be nonblank text")


def _snapshot_payload(snapshot: AccountSnapshot) -> str:
    positions = [
        {
            "venue_id": item.instrument.venue.venue_id,
            "venue_timezone": item.instrument.venue.timezone,
            "instrument_id": item.instrument.instrument_id,
            "currency": item.instrument.currency,
            "quantity": str(item.quantity),
            "average_price": str(item.average_price),
            "realized_pnl": str(item.realized_pnl),
        }
        for item in snapshot.positions
    ]
    payload = {
        "cash": str(snapshot.cash),
        "fees": str(snapshot.fees),
        "realized_pnl": str(snapshot.realized_pnl),
        "unrealized_pnl": str(snapshot.unrealized_pnl),
        "equity": str(snapshot.equity),
        "gross_exposure": str(snapshot.gross_exposure),
        "net_exposure": str(snapshot.net_exposure),
        "positions": positions,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
