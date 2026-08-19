from __future__ import annotations

import json

from ..data.sqlite import SQLiteStore
from .accounting import AccountSnapshot
from .orders import SimulatedFill


_TRADER_SCHEMA_VERSION = 1


class TradingStateRepository:
    """Trader-owned durable paper state inside the canonical Nika SQLite database."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def initialize(self) -> None:
        with self._store.connection() as conn:
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
            if current < 1:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS trading_research_fills ("
                    "fill_id TEXT PRIMARY KEY, approval_id TEXT NOT NULL, intent_id TEXT NOT NULL, "
                    "instrument_id TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL, "
                    "price TEXT NOT NULL, fee TEXT NOT NULL, filled_at TEXT NOT NULL, "
                    "filled_slice INTEGER NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS trading_research_account_state ("
                    "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), payload TEXT NOT NULL, "
                    "last_fill_id TEXT NOT NULL)"
                )
                conn.execute("INSERT INTO trading_research_schema_migrations(version) VALUES (1)")

    def commit_fill_and_account(self, fill: SimulatedFill, snapshot: AccountSnapshot) -> bool:
        payload = _snapshot_payload(snapshot)
        with self._store.connection() as conn:
            existing = conn.execute(
                "SELECT 1 FROM trading_research_fills WHERE fill_id = ?", (fill.fill_id,)
            ).fetchone()
            if existing is not None:
                return False
            conn.execute(
                "INSERT INTO trading_research_fills("
                "fill_id, approval_id, intent_id, instrument_id, side, quantity, price, fee, "
                "filled_at, filled_slice) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fill.fill_id,
                    fill.approval_id,
                    fill.intent_id,
                    fill.instrument.instrument_id,
                    fill.side.value,
                    str(fill.quantity),
                    str(fill.price),
                    str(fill.fee),
                    fill.filled_at.isoformat(),
                    fill.filled_slice,
                ),
            )
            conn.execute(
                "INSERT INTO trading_research_account_state(singleton, payload, last_fill_id) "
                "VALUES (1, ?, ?) ON CONFLICT(singleton) DO UPDATE SET "
                "payload = excluded.payload, last_fill_id = excluded.last_fill_id",
                (payload, fill.fill_id),
            )
        return True

    def fill_count(self) -> int:
        with self._store.connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM trading_research_fills").fetchone()
        return int(row["count"])

    def has_fill(self, fill_id: str) -> bool:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM trading_research_fills WHERE fill_id = ?", (fill_id,)
            ).fetchone()
        return row is not None

    def account_payload(self) -> dict[str, object] | None:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT payload FROM trading_research_account_state WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return None
        value = json.loads(str(row["payload"]))
        if not isinstance(value, dict):
            raise TypeError("invalid durable trading account payload")
        return value


def _snapshot_payload(snapshot: AccountSnapshot) -> str:
    positions = [
        {
            "instrument_id": item.instrument.instrument_id,
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
