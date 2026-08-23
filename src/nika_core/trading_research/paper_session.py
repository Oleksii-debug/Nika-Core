from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any

from ..data.sqlite import SQLiteStore
from .accounting import AccountSnapshot, PortfolioLedger, Position
from .contracts import (
    Bar,
    Instrument,
    MarketEvent,
    Quote,
    Tick,
    TradingResearchError,
    Venue,
    require_aware_utc,
)
from .dataset import DatasetVersion, canonical_event_bytes
from .orders import (
    ExecutionPolicy,
    OrderIntent,
    OrderState,
    OrderType,
    RiskApprovedOrder,
    Side,
    SimulatedFill,
)
from .replay import OrderUpdate, SimulationExecutionEngine, TimeSlice
from .risk import RiskEngine, RiskLimits, RiskState

_PAPER_SCHEMA_VERSION = 1
_TERMINAL_STATES = frozenset({OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED})
_OPEN_STATES = frozenset(
    {OrderState.PENDING, OrderState.ACTIVE, OrderState.PARTIALLY_FILLED}
)


class PaperSessionError(TradingResearchError):
    """Base error for durable deterministic paper-session state."""


class PaperSessionConflict(PaperSessionError):
    """Raised when a caller attempts a non-deterministic or stale session transition."""


class PaperSessionIntegrityError(PaperSessionError):
    """Raised when durable paper-session state fails reconstruction invariants."""


@dataclass(frozen=True, slots=True)
class PaperDataProvenance:
    dataset_id: str
    dataset_version: str
    raw_hash: str
    semantic_hash: str
    source_id: str
    acquired_at: datetime
    cutoff_at: datetime
    license_id: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.dataset_id, "dataset_id")
        _require_text(self.dataset_version, "dataset_version")
        _require_digest(self.raw_hash, "raw_hash")
        _require_digest(self.semantic_hash, "semantic_hash")
        _require_text(self.source_id, "source_id")
        acquired_at = require_aware_utc(self.acquired_at, "acquired_at")
        cutoff_at = require_aware_utc(self.cutoff_at, "cutoff_at")
        object.__setattr__(self, "acquired_at", acquired_at)
        object.__setattr__(self, "cutoff_at", cutoff_at)

    @classmethod
    def from_dataset_version(
        cls,
        version: DatasetVersion,
        *,
        cutoff_at: datetime,
    ) -> PaperDataProvenance:
        return cls(
            dataset_id=version.dataset_id,
            dataset_version=version.version,
            raw_hash=version.raw_hash,
            semantic_hash=version.semantic_hash,
            source_id=version.provenance.source_id,
            acquired_at=version.provenance.acquired_at,
            cutoff_at=cutoff_at,
            license_id=version.provenance.license_id,
        )


@dataclass(frozen=True, slots=True)
class PaperSessionConfig:
    session_id: str
    strategy_id: str
    strategy_version: str
    starting_cash: Decimal
    data: PaperDataProvenance
    execution_policy: ExecutionPolicy
    risk_limits: RiskLimits

    def __post_init__(self) -> None:
        _require_text(self.session_id, "session_id")
        _require_text(self.strategy_id, "strategy_id")
        _require_text(self.strategy_version, "strategy_version")
        _finite_decimal(self.starting_cash, "starting_cash")
        if self.starting_cash < 0:
            raise PaperSessionError("starting_cash cannot be negative")


@dataclass(frozen=True, slots=True)
class PaperOrderSnapshot:
    order: RiskApprovedOrder
    state: OrderState
    remaining_quantity: Decimal
    terminal_reason: str
    queued_seq: int
    last_update_slice: int | None

    def __post_init__(self) -> None:
        remaining = _finite_decimal(self.remaining_quantity, "remaining_quantity")
        if remaining < 0 or remaining > self.order.intent.quantity:
            raise PaperSessionIntegrityError("durable order remaining quantity is invalid")
        if self.queued_seq < 0:
            raise PaperSessionIntegrityError("queued_seq must be non-negative")
        if self.last_update_slice is not None and self.last_update_slice < 0:
            raise PaperSessionIntegrityError("last_update_slice must be non-negative")
        if self.state is OrderState.FILLED and remaining != 0:
            raise PaperSessionIntegrityError("filled order must have zero remaining quantity")
        if self.state in _OPEN_STATES and remaining <= 0:
            raise PaperSessionIntegrityError("open order must have positive remaining quantity")
        if self.state is OrderState.REJECTED:
            raise PaperSessionIntegrityError("rejected orders are never executable session state")
        object.__setattr__(self, "remaining_quantity", remaining)

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES


@dataclass(frozen=True, slots=True)
class PaperSessionSnapshot:
    config: PaperSessionConfig
    account: AccountSnapshot
    risk_state: RiskState
    cursor_slice: int | None
    cursor_at: datetime | None
    orders: tuple[PaperOrderSnapshot, ...]

    @property
    def open_orders(self) -> tuple[PaperOrderSnapshot, ...]:
        return tuple(item for item in self.orders if not item.is_terminal)

    @property
    def terminal_orders(self) -> tuple[PaperOrderSnapshot, ...]:
        return tuple(item for item in self.orders if item.is_terminal)


@dataclass(frozen=True, slots=True)
class PaperSliceResult:
    snapshot: PaperSessionSnapshot
    updates: tuple[OrderUpdate, ...]
    replayed_committed_slice: bool = False


@dataclass(frozen=True, slots=True)
class _SessionRecord:
    snapshot: PaperSessionSnapshot
    marks: dict[str, Decimal]
    last_slice_sha256: str | None
    row_version: int
    fills: tuple[SimulatedFill, ...]


class PaperSessionRepository:
    """Session-scoped durability in the canonical Nika SQLite database."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def initialize(self) -> None:
        self._store.initialize()
        with self._store.connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS trading_research_paper_schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            row = conn.execute(
                "SELECT MAX(version) AS version "
                "FROM trading_research_paper_schema_migrations"
            ).fetchone()
            current = int(row["version"] or 0)
            if current > _PAPER_SCHEMA_VERSION:
                raise RuntimeError("paper-session schema is newer than supported")
            if current < 1:
                self._migrate_v1(conn)
                conn.execute(
                    "INSERT INTO trading_research_paper_schema_migrations(version) VALUES (1)"
                )

    @staticmethod
    def _migrate_v1(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS trading_research_paper_sessions ("
            "session_id TEXT PRIMARY KEY, "
            "config_json TEXT NOT NULL, config_sha256 TEXT NOT NULL, "
            "risk_json TEXT NOT NULL, account_json TEXT NOT NULL, marks_json TEXT NOT NULL, "
            "cursor_slice INTEGER, cursor_at TEXT, last_slice_sha256 TEXT, "
            "state_sha256 TEXT NOT NULL, row_version INTEGER NOT NULL, "
            "next_order_seq INTEGER NOT NULL, "
            "CHECK(cursor_slice IS NULL OR cursor_slice >= 0), "
            "CHECK(row_version >= 0), CHECK(next_order_seq >= 0))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS trading_research_paper_orders ("
            "session_id TEXT NOT NULL, approval_id TEXT NOT NULL, queued_seq INTEGER NOT NULL, "
            "order_json TEXT NOT NULL, order_sha256 TEXT NOT NULL, state TEXT NOT NULL, "
            "remaining_quantity TEXT NOT NULL, terminal_reason TEXT NOT NULL, "
            "state_sha256 TEXT NOT NULL, "
            "last_update_slice INTEGER, "
            "PRIMARY KEY(session_id, approval_id), UNIQUE(session_id, queued_seq), "
            "FOREIGN KEY(session_id) REFERENCES trading_research_paper_sessions(session_id) "
            "ON DELETE CASCADE, CHECK(queued_seq >= 0), "
            "CHECK(last_update_slice IS NULL OR last_update_slice >= 0))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS trading_research_paper_fills ("
            "session_id TEXT NOT NULL, fill_id TEXT NOT NULL, approval_id TEXT NOT NULL, "
            "fill_json TEXT NOT NULL, fill_sha256 TEXT NOT NULL, filled_slice INTEGER NOT NULL, "
            "PRIMARY KEY(session_id, fill_id), "
            "FOREIGN KEY(session_id, approval_id) "
            "REFERENCES trading_research_paper_orders(session_id, approval_id) "
            "ON DELETE RESTRICT, CHECK(filled_slice >= 0))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_paper_orders_session_state "
            "ON trading_research_paper_orders(session_id, state, queued_seq)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_paper_fills_session_slice "
            "ON trading_research_paper_fills(session_id, filled_slice, fill_id)"
        )

    def create(self, config: PaperSessionConfig) -> _SessionRecord:
        config_json = _canonical_json(_config_payload(config))
        config_hash = _sha(config_json)
        ledger = PortfolioLedger(config.starting_cash)
        account = ledger.snapshot({})
        risk_state = RiskState(config.starting_cash, config.starting_cash)
        risk_json = _canonical_json(_risk_state_payload(risk_state))
        account_json = _canonical_json(_account_payload(account))
        marks_json = _canonical_json({})
        state_hash = _state_hash(
            config_hash=config_hash,
            risk_json=risk_json,
            account_json=account_json,
            marks_json=marks_json,
            cursor_slice=None,
            cursor_at=None,
            last_slice_sha256=None,
            row_version=0,
            next_order_seq=0,
        )
        with self._store.connection() as conn:
            try:
                conn.execute(
                    "INSERT INTO trading_research_paper_sessions("
                    "session_id, config_json, config_sha256, risk_json, account_json, marks_json, "
                    "cursor_slice, cursor_at, last_slice_sha256, state_sha256, row_version, "
                    "next_order_seq) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, 0, 0)",
                    (
                        config.session_id,
                        config_json,
                        config_hash,
                        risk_json,
                        account_json,
                        marks_json,
                        state_hash,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PaperSessionConflict(
                    f"paper session already exists: {config.session_id}"
                ) from exc
        return self.load(config.session_id)

    def load(self, session_id: str) -> _SessionRecord:
        _require_text(session_id, "session_id")
        with self._store.connection() as conn:
            session_row = conn.execute(
                "SELECT * FROM trading_research_paper_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session_row is None:
                raise PaperSessionError(f"unknown paper session: {session_id}")
            order_rows = conn.execute(
                "SELECT * FROM trading_research_paper_orders WHERE session_id = ? "
                "ORDER BY queued_seq, approval_id",
                (session_id,),
            ).fetchall()
            fill_rows = conn.execute(
                "SELECT f.* FROM trading_research_paper_fills AS f "
                "JOIN trading_research_paper_orders AS o "
                "ON o.session_id = f.session_id AND o.approval_id = f.approval_id "
                "WHERE f.session_id = ? "
                "ORDER BY f.filled_slice, o.queued_seq, f.fill_id",
                (session_id,),
            ).fetchall()
        return self._decode_record(session_row, order_rows, fill_rows)

    def insert_order(
        self,
        record: _SessionRecord,
        order: RiskApprovedOrder,
    ) -> _SessionRecord:
        existing = next(
            (
                item
                for item in record.snapshot.orders
                if item.order.intent.intent_id == order.intent.intent_id
            ),
            None,
        )
        if existing is not None:
            if existing.order != order:
                raise PaperSessionConflict(
                    "intent_id is already bound to a different approved order"
                )
            return record

        order_json = _canonical_json(_order_payload(order))
        order_hash = _sha(order_json)
        queued_seq = len(record.snapshot.orders)
        new_version = record.row_version + 1
        state_row = self._state_row_values(
            record,
            row_version=new_version,
            next_order_seq=queued_seq + 1,
        )
        with self._store.connection() as conn:
            current = conn.execute(
                "SELECT row_version, next_order_seq FROM trading_research_paper_sessions "
                "WHERE session_id = ?",
                (record.snapshot.config.session_id,),
            ).fetchone()
            if current is None or int(current["row_version"]) != record.row_version:
                raise PaperSessionConflict("paper session changed concurrently")
            queued_seq = int(current["next_order_seq"])
            try:
                conn.execute(
                    "INSERT INTO trading_research_paper_orders("
                    "session_id, approval_id, queued_seq, order_json, order_sha256, state, "
                    "remaining_quantity, terminal_reason, state_sha256, last_update_slice) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, NULL)",
                    (
                        record.snapshot.config.session_id,
                        order.approval_id,
                        queued_seq,
                        order_json,
                        order_hash,
                        OrderState.PENDING.value,
                        str(order.intent.quantity),
                        _order_state_hash(
                            order.approval_id,
                            OrderState.PENDING,
                            order.intent.quantity,
                            "",
                            None,
                        ),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PaperSessionConflict("approved order identity already exists") from exc
            updated = conn.execute(
                "UPDATE trading_research_paper_sessions SET state_sha256 = ?, row_version = ?, "
                "next_order_seq = ? WHERE session_id = ? AND row_version = ?",
                (
                    state_row["state_sha256"],
                    new_version,
                    queued_seq + 1,
                    record.snapshot.config.session_id,
                    record.row_version,
                ),
            )
            if updated.rowcount != 1:
                raise PaperSessionConflict("paper session changed concurrently")
        return self.load(record.snapshot.config.session_id)

    def cancel_order(
        self,
        record: _SessionRecord,
        approval_id: str,
        reason: str,
    ) -> _SessionRecord:
        current = _find_order(record.snapshot.orders, approval_id)
        if current.is_terminal:
            return record
        if not reason.strip():
            raise PaperSessionError("cancel reason must not be empty")
        new_version = record.row_version + 1
        last_slice = record.snapshot.cursor_slice
        state_row = self._state_row_values(
            record,
            row_version=new_version,
            next_order_seq=len(record.snapshot.orders),
        )
        with self._store.connection() as conn:
            changed = conn.execute(
                "UPDATE trading_research_paper_orders SET state = ?, terminal_reason = ?, "
                "state_sha256 = ?, last_update_slice = ? "
                "WHERE session_id = ? AND approval_id = ? "
                "AND state IN (?, ?, ?)",
                (
                    OrderState.CANCELLED.value,
                    reason,
                    _order_state_hash(
                        approval_id,
                        OrderState.CANCELLED,
                        current.remaining_quantity,
                        reason,
                        last_slice,
                    ),
                    last_slice,
                    record.snapshot.config.session_id,
                    approval_id,
                    OrderState.PENDING.value,
                    OrderState.ACTIVE.value,
                    OrderState.PARTIALLY_FILLED.value,
                ),
            )
            if changed.rowcount != 1:
                raise PaperSessionConflict("order changed concurrently")
            updated = conn.execute(
                "UPDATE trading_research_paper_sessions SET state_sha256 = ?, row_version = ? "
                "WHERE session_id = ? AND row_version = ?",
                (
                    state_row["state_sha256"],
                    new_version,
                    record.snapshot.config.session_id,
                    record.row_version,
                ),
            )
            if updated.rowcount != 1:
                raise PaperSessionConflict("paper session changed concurrently")
        return self.load(record.snapshot.config.session_id)

    def commit_slice(
        self,
        record: _SessionRecord,
        *,
        time_slice: TimeSlice,
        slice_sha256: str,
        marks: dict[str, Decimal],
        account: AccountSnapshot,
        risk_state: RiskState,
        updates: tuple[OrderUpdate, ...],
    ) -> _SessionRecord:
        new_version = record.row_version + 1
        risk_json = _canonical_json(_risk_state_payload(risk_state))
        account_json = _canonical_json(_account_payload(account))
        marks_json = _canonical_json(_marks_payload(marks))
        config_json = _canonical_json(_config_payload(record.snapshot.config))
        config_hash = _sha(config_json)
        state_hash = _state_hash(
            config_hash=config_hash,
            risk_json=risk_json,
            account_json=account_json,
            marks_json=marks_json,
            cursor_slice=time_slice.index,
            cursor_at=time_slice.at.isoformat(),
            last_slice_sha256=slice_sha256,
            row_version=new_version,
            next_order_seq=len(record.snapshot.orders),
        )
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT row_version FROM trading_research_paper_sessions WHERE session_id = ?",
                (record.snapshot.config.session_id,),
            ).fetchone()
            if row is None or int(row["row_version"]) != record.row_version:
                raise PaperSessionConflict("paper session changed concurrently")
            for update in updates:
                if update.fill is not None:
                    fill_json = _canonical_json(_fill_payload(update.fill))
                    try:
                        conn.execute(
                            "INSERT INTO trading_research_paper_fills("
                            "session_id, fill_id, approval_id, fill_json, fill_sha256, "
                            "filled_slice) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                record.snapshot.config.session_id,
                                update.fill.fill_id,
                                update.approval_id,
                                fill_json,
                                _sha(fill_json),
                                update.fill.filled_slice,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise PaperSessionConflict("paper fill identity already committed") from exc
                changed = conn.execute(
                    "UPDATE trading_research_paper_orders SET state = ?, remaining_quantity = ?, "
                    "terminal_reason = ?, state_sha256 = ?, last_update_slice = ? "
                    "WHERE session_id = ? AND approval_id = ?",
                    (
                        update.state.value,
                        str(update.remaining_quantity),
                        update.reason if update.state in _TERMINAL_STATES else "",
                        _order_state_hash(
                            update.approval_id,
                            update.state,
                            update.remaining_quantity,
                            update.reason if update.state in _TERMINAL_STATES else "",
                            time_slice.index,
                        ),
                        time_slice.index,
                        record.snapshot.config.session_id,
                        update.approval_id,
                    ),
                )
                if changed.rowcount != 1:
                    raise PaperSessionIntegrityError(
                        "slice update references unknown approved order"
                    )
            updated = conn.execute(
                "UPDATE trading_research_paper_sessions SET risk_json = ?, account_json = ?, "
                "marks_json = ?, cursor_slice = ?, cursor_at = ?, last_slice_sha256 = ?, "
                "state_sha256 = ?, row_version = ? WHERE session_id = ? AND row_version = ?",
                (
                    risk_json,
                    account_json,
                    marks_json,
                    time_slice.index,
                    time_slice.at.isoformat(),
                    slice_sha256,
                    state_hash,
                    new_version,
                    record.snapshot.config.session_id,
                    record.row_version,
                ),
            )
            if updated.rowcount != 1:
                raise PaperSessionConflict("paper session changed concurrently")
        return self.load(record.snapshot.config.session_id)

    def _decode_record(
        self,
        session_row: sqlite3.Row,
        order_rows: list[sqlite3.Row],
        fill_rows: list[sqlite3.Row],
    ) -> _SessionRecord:
        config_json = str(session_row["config_json"])
        if _sha(config_json) != str(session_row["config_sha256"]):
            raise PaperSessionIntegrityError("paper session config digest mismatch")
        config = _config_from_payload(_json_object(config_json, "config_json"))
        if config.session_id != str(session_row["session_id"]):
            raise PaperSessionIntegrityError("paper session identity changed")
        risk_json = str(session_row["risk_json"])
        account_json = str(session_row["account_json"])
        marks_json = str(session_row["marks_json"])
        risk_state = _risk_state_from_payload(_json_object(risk_json, "risk_json"))
        account = _account_from_payload(_json_object(account_json, "account_json"))
        marks = _marks_from_payload(_json_object(marks_json, "marks_json"))
        cursor_slice = session_row["cursor_slice"]
        cursor_at_raw = session_row["cursor_at"]
        cursor_at = None
        if cursor_slice is None:
            if cursor_at_raw is not None or session_row["last_slice_sha256"] is not None:
                raise PaperSessionIntegrityError("empty cursor has committed-slice metadata")
        else:
            cursor_slice = int(cursor_slice)
            if cursor_slice < 0 or cursor_at_raw is None:
                raise PaperSessionIntegrityError("durable cursor is malformed")
            cursor_at = require_aware_utc(_parse_datetime(str(cursor_at_raw)), "cursor_at")
            _require_digest(str(session_row["last_slice_sha256"]), "last_slice_sha256")
        row_version = int(session_row["row_version"])
        next_order_seq = int(session_row["next_order_seq"])
        expected_state_hash = _state_hash(
            config_hash=str(session_row["config_sha256"]),
            risk_json=risk_json,
            account_json=account_json,
            marks_json=marks_json,
            cursor_slice=cursor_slice,
            cursor_at=None if cursor_at is None else cursor_at.isoformat(),
            last_slice_sha256=session_row["last_slice_sha256"],
            row_version=row_version,
            next_order_seq=next_order_seq,
        )
        if expected_state_hash != str(session_row["state_sha256"]):
            raise PaperSessionIntegrityError("paper session state digest mismatch")

        orders = tuple(_order_snapshot_from_row(row) for row in order_rows)
        if tuple(item.queued_seq for item in orders) != tuple(range(len(orders))):
            raise PaperSessionIntegrityError("durable order sequence is not contiguous")
        if next_order_seq != len(orders):
            raise PaperSessionIntegrityError("next_order_seq disagrees with durable orders")
        order_by_approval = {item.order.approval_id: item for item in orders}
        fills = tuple(_fill_from_row(row) for row in fill_rows)
        self._validate_fill_lineage(config, order_by_approval, fills)
        rebuilt = PortfolioLedger(config.starting_cash)
        for fill in fills:
            rebuilt.apply_fill(fill)
        rebuilt_snapshot = rebuilt.snapshot(marks)
        if rebuilt_snapshot != account:
            raise PaperSessionIntegrityError("persisted account disagrees with durable fills")
        _validate_order_quantities(orders, fills)
        if risk_state.session_start_equity != config.starting_cash:
            raise PaperSessionIntegrityError("risk session-start equity changed")
        if risk_state.peak_equity < account.equity:
            raise PaperSessionIntegrityError("risk peak equity is below current account equity")
        snapshot = PaperSessionSnapshot(
            config=config,
            account=account,
            risk_state=risk_state,
            cursor_slice=cursor_slice,
            cursor_at=cursor_at,
            orders=orders,
        )
        return _SessionRecord(
            snapshot=snapshot,
            marks=marks,
            last_slice_sha256=session_row["last_slice_sha256"],
            row_version=row_version,
            fills=fills,
        )

    @staticmethod
    def _validate_fill_lineage(
        config: PaperSessionConfig,
        orders: dict[str, PaperOrderSnapshot],
        fills: tuple[SimulatedFill, ...],
    ) -> None:
        seen: set[str] = set()
        for fill in fills:
            if fill.fill_id in seen:
                raise PaperSessionIntegrityError("duplicate durable fill identity")
            seen.add(fill.fill_id)
            order_snapshot = orders.get(fill.approval_id)
            if order_snapshot is None:
                raise PaperSessionIntegrityError("durable fill has no approved order")
            order = order_snapshot.order
            if fill.intent_id != order.intent.intent_id:
                raise PaperSessionIntegrityError("durable fill intent does not match approval")
            if fill.instrument != order.intent.instrument or fill.side is not order.intent.side:
                raise PaperSessionIntegrityError("durable fill instrument/side changed")
            session_hash = sha256(config.session_id.encode("utf-8")).hexdigest()[:16]
            if not fill.fill_id.startswith(f"paper:{session_hash}:"):
                raise PaperSessionIntegrityError("durable fill is not session-scoped")

    @staticmethod
    def _state_row_values(
        record: _SessionRecord,
        *,
        row_version: int,
        next_order_seq: int,
    ) -> dict[str, object]:
        config_json = _canonical_json(_config_payload(record.snapshot.config))
        risk_json = _canonical_json(_risk_state_payload(record.snapshot.risk_state))
        account_json = _canonical_json(_account_payload(record.snapshot.account))
        marks_json = _canonical_json(_marks_payload(record.marks))
        cursor_at = (
            None if record.snapshot.cursor_at is None else record.snapshot.cursor_at.isoformat()
        )
        return {
            "state_sha256": _state_hash(
                config_hash=_sha(config_json),
                risk_json=risk_json,
                account_json=account_json,
                marks_json=marks_json,
                cursor_slice=record.snapshot.cursor_slice,
                cursor_at=cursor_at,
                last_slice_sha256=record.last_slice_sha256,
                row_version=row_version,
                next_order_seq=next_order_seq,
            )
        }


class PaperTradingSession:
    """Paper-only deterministic coordinator with restart-safe approved-order authority."""

    def __init__(self, repository: PaperSessionRepository, record: _SessionRecord) -> None:
        self._repository = repository
        self._record = record
        self._execution = SimulationExecutionEngine()

    @classmethod
    def start(
        cls,
        store: SQLiteStore,
        config: PaperSessionConfig,
    ) -> PaperTradingSession:
        repository = PaperSessionRepository(store)
        repository.initialize()
        return cls(repository, repository.create(config))

    @classmethod
    def resume(
        cls,
        store: SQLiteStore,
        session_id: str,
        *,
        expected_data: PaperDataProvenance | None = None,
    ) -> PaperTradingSession:
        repository = PaperSessionRepository(store)
        repository.initialize()
        record = repository.load(session_id)
        if expected_data is not None and expected_data != record.snapshot.config.data:
            raise PaperSessionConflict("data source fingerprint/cutoff changed across restart")
        return cls(repository, record)

    @property
    def snapshot(self) -> PaperSessionSnapshot:
        return self._record.snapshot

    @property
    def fill_count(self) -> int:
        return len(self._record.fills)

    def queue_intent(
        self,
        intent: OrderIntent,
        *,
        mark_price: Decimal,
    ) -> RiskApprovedOrder:
        cursor_slice = self._record.snapshot.cursor_slice
        cursor_at = self._record.snapshot.cursor_at
        if cursor_slice is None or cursor_at is None:
            raise PaperSessionConflict(
                "an intent can be queued only after a committed market slice"
            )
        if intent.submitted_slice != cursor_slice or intent.submitted_at != cursor_at:
            raise PaperSessionConflict(
                "intent must be bound to the current committed market cursor"
            )
        existing = next(
            (
                item
                for item in self._record.snapshot.orders
                if item.order.intent.intent_id == intent.intent_id
            ),
            None,
        )
        if existing is not None:
            if existing.order.intent != intent:
                raise PaperSessionConflict("intent_id is already bound to different intent data")
            return existing.order

        pending_signed = sum(
            (
                item.remaining_quantity * Decimal(item.order.intent.side.sign)
                for item in self._record.snapshot.open_orders
                if item.order.intent.instrument.instrument_id == intent.instrument.instrument_id
            ),
            Decimal(0),
        )
        engine = RiskEngine(self._record.snapshot.config.risk_limits)
        order = engine.approve(
            intent,
            snapshot=self._record.snapshot.account,
            mark_price=_finite_decimal(mark_price, "mark_price"),
            pending_signed_quantity=pending_signed,
            approved_at=cursor_at,
            approved_slice=cursor_slice,
            policy=self._record.snapshot.config.execution_policy,
            risk_state=self._record.snapshot.risk_state,
        )
        self._record = self._repository.insert_order(self._record, order)
        return order

    def cancel(self, approval_id: str, reason: str = "cancelled by paper session") -> None:
        self._record = self._repository.cancel_order(self._record, approval_id, reason)

    def process_slice(self, time_slice: TimeSlice) -> PaperSliceResult:
        data = self._record.snapshot.config.data
        if time_slice.at > data.cutoff_at:
            raise PaperSessionConflict("market slice exceeds the session data cutoff")
        slice_hash = _slice_hash(time_slice)
        cursor_slice = self._record.snapshot.cursor_slice
        cursor_at = self._record.snapshot.cursor_at
        if cursor_slice is not None:
            if time_slice.index == cursor_slice:
                if time_slice.at == cursor_at and slice_hash == self._record.last_slice_sha256:
                    return PaperSliceResult(
                        snapshot=self._record.snapshot,
                        updates=(),
                        replayed_committed_slice=True,
                    )
                raise PaperSessionConflict("committed slice index was replayed with different data")
            if time_slice.index < cursor_slice:
                raise PaperSessionConflict("market cursor cannot move backwards")
            if time_slice.index != cursor_slice + 1:
                raise PaperSessionConflict("market cursor cannot skip slices")
            if cursor_at is not None and time_slice.at < cursor_at:
                raise PaperSessionConflict("market cursor time cannot move backwards")

        marks = dict(self._record.marks)
        _update_marks(marks, time_slice.events)
        ledger = PortfolioLedger(self._record.snapshot.config.starting_cash)
        for fill in self._record.fills:
            ledger.apply_fill(fill)
        engine = RiskEngine(self._record.snapshot.config.risk_limits)
        updates: list[OrderUpdate] = []
        risk_state = self._record.snapshot.risk_state
        for order_snapshot in self._record.snapshot.open_orders:
            update = self._execution.execute(
                order_snapshot.order,
                time_slice,
                remaining_quantity=order_snapshot.remaining_quantity,
            )
            if update.fill is not None:
                scoped_fill = _scope_fill(self._record.snapshot.config.session_id, update.fill)
                ledger.apply_fill(scoped_fill)
                account_after_fill = ledger.snapshot(marks)
                engine.assert_post_fill(account_after_fill, risk_state)
                update = OrderUpdate(
                    approval_id=update.approval_id,
                    state=update.state,
                    remaining_quantity=update.remaining_quantity,
                    fill=scoped_fill,
                    reason=update.reason,
                )
            updates.append(update)

        account = ledger.snapshot(marks)
        risk_state = RiskState(
            peak_equity=max(risk_state.peak_equity, account.equity),
            session_start_equity=risk_state.session_start_equity,
        )
        self._record = self._repository.commit_slice(
            self._record,
            time_slice=time_slice,
            slice_sha256=slice_hash,
            marks=marks,
            account=account,
            risk_state=risk_state,
            updates=tuple(updates),
        )
        return PaperSliceResult(self._record.snapshot, tuple(updates))


def _scope_fill(session_id: str, fill: SimulatedFill) -> SimulatedFill:
    return SimulatedFill(
        fill_id=_session_fill_id(session_id, fill.fill_id),
        approval_id=fill.approval_id,
        intent_id=fill.intent_id,
        instrument=fill.instrument,
        side=fill.side,
        quantity=fill.quantity,
        price=fill.price,
        fee=fill.fee,
        filled_at=fill.filled_at,
        filled_slice=fill.filled_slice,
    )


def _session_fill_id(session_id: str, fill_id: str) -> str:
    session_hash = sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return f"paper:{session_hash}:{fill_id}"


def _update_marks(marks: dict[str, Decimal], events: tuple[MarketEvent, ...]) -> None:
    for event in events:
        if isinstance(event, Quote):
            marks[event.instrument.instrument_id] = (event.bid + event.ask) / Decimal(2)
        elif isinstance(event, Bar):
            marks[event.instrument.instrument_id] = event.close
        elif isinstance(event, Tick):
            marks[event.instrument.instrument_id] = event.price


def _slice_hash(time_slice: TimeSlice) -> str:
    digest = sha256()
    header = f"nika-paper-slice-v1|{time_slice.index}|{time_slice.at.isoformat()}".encode()
    digest.update(len(header).to_bytes(8, "big"))
    digest.update(header)
    for event in time_slice.events:
        encoded = canonical_event_bytes(event)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _config_payload(config: PaperSessionConfig) -> dict[str, object]:
    return {
        "session_id": config.session_id,
        "strategy_id": config.strategy_id,
        "strategy_version": config.strategy_version,
        "starting_cash": str(config.starting_cash),
        "data": {
            "dataset_id": config.data.dataset_id,
            "dataset_version": config.data.dataset_version,
            "raw_hash": config.data.raw_hash,
            "semantic_hash": config.data.semantic_hash,
            "source_id": config.data.source_id,
            "acquired_at": config.data.acquired_at.isoformat(),
            "cutoff_at": config.data.cutoff_at.isoformat(),
            "license_id": config.data.license_id,
        },
        "execution_policy": _policy_payload(config.execution_policy),
        "risk_limits": _risk_limits_payload(config.risk_limits),
    }


def _config_from_payload(payload: dict[str, Any]) -> PaperSessionConfig:
    data = _object(payload, "data")
    return PaperSessionConfig(
        session_id=_string(payload, "session_id"),
        strategy_id=_string(payload, "strategy_id"),
        strategy_version=_string(payload, "strategy_version"),
        starting_cash=_decimal_value(payload, "starting_cash"),
        data=PaperDataProvenance(
            dataset_id=_string(data, "dataset_id"),
            dataset_version=_string(data, "dataset_version"),
            raw_hash=_string(data, "raw_hash"),
            semantic_hash=_string(data, "semantic_hash"),
            source_id=_string(data, "source_id"),
            acquired_at=_parse_datetime(_string(data, "acquired_at")),
            cutoff_at=_parse_datetime(_string(data, "cutoff_at")),
            license_id=_optional_string(data, "license_id"),
        ),
        execution_policy=_policy_from_payload(_object(payload, "execution_policy")),
        risk_limits=_risk_limits_from_payload(_object(payload, "risk_limits")),
    )


def _order_payload(order: RiskApprovedOrder) -> dict[str, object]:
    intent = order.intent
    return {
        "approval_id": order.approval_id,
        "approved_at": order.approved_at.isoformat(),
        "approved_slice": order.approved_slice,
        "policy": _policy_payload(order.policy),
        "intent": {
            "intent_id": intent.intent_id,
            "instrument": _instrument_payload(intent.instrument),
            "side": intent.side.value,
            "order_type": intent.order_type.value,
            "quantity": str(intent.quantity),
            "submitted_at": intent.submitted_at.isoformat(),
            "submitted_slice": intent.submitted_slice,
            "limit_price": None if intent.limit_price is None else str(intent.limit_price),
            "expires_at": None if intent.expires_at is None else intent.expires_at.isoformat(),
        },
    }


def _order_from_payload(payload: dict[str, Any]) -> RiskApprovedOrder:
    intent_payload = _object(payload, "intent")
    limit_raw = intent_payload.get("limit_price")
    expires_raw = intent_payload.get("expires_at")
    intent = OrderIntent(
        intent_id=_string(intent_payload, "intent_id"),
        instrument=_instrument_from_payload(_object(intent_payload, "instrument")),
        side=Side(_string(intent_payload, "side")),
        order_type=OrderType(_string(intent_payload, "order_type")),
        quantity=_decimal_value(intent_payload, "quantity"),
        submitted_at=_parse_datetime(_string(intent_payload, "submitted_at")),
        submitted_slice=_int_value(intent_payload, "submitted_slice"),
        limit_price=None if limit_raw is None else _decimal(limit_raw, "limit_price"),
        expires_at=None if expires_raw is None else _parse_datetime(str(expires_raw)),
    )
    return RiskApprovedOrder(
        approval_id=_string(payload, "approval_id"),
        intent=intent,
        approved_at=_parse_datetime(_string(payload, "approved_at")),
        approved_slice=_int_value(payload, "approved_slice"),
        policy=_policy_from_payload(_object(payload, "policy")),
    )


def _fill_payload(fill: SimulatedFill) -> dict[str, object]:
    return {
        "fill_id": fill.fill_id,
        "approval_id": fill.approval_id,
        "intent_id": fill.intent_id,
        "instrument": _instrument_payload(fill.instrument),
        "side": fill.side.value,
        "quantity": str(fill.quantity),
        "price": str(fill.price),
        "fee": str(fill.fee),
        "filled_at": fill.filled_at.isoformat(),
        "filled_slice": fill.filled_slice,
    }


def _fill_from_payload(payload: dict[str, Any]) -> SimulatedFill:
    return SimulatedFill(
        fill_id=_string(payload, "fill_id"),
        approval_id=_string(payload, "approval_id"),
        intent_id=_string(payload, "intent_id"),
        instrument=_instrument_from_payload(_object(payload, "instrument")),
        side=Side(_string(payload, "side")),
        quantity=_decimal_value(payload, "quantity"),
        price=_decimal_value(payload, "price"),
        fee=_decimal_value(payload, "fee"),
        filled_at=_parse_datetime(_string(payload, "filled_at")),
        filled_slice=_int_value(payload, "filled_slice"),
    )


def _instrument_payload(instrument: Instrument) -> dict[str, str]:
    return {
        "instrument_id": instrument.instrument_id,
        "venue_id": instrument.venue.venue_id,
        "venue_timezone": instrument.venue.timezone,
        "currency": instrument.currency,
    }


def _instrument_from_payload(payload: dict[str, Any]) -> Instrument:
    return Instrument(
        instrument_id=_string(payload, "instrument_id"),
        venue=Venue(
            venue_id=_string(payload, "venue_id"),
            timezone=_string(payload, "venue_timezone"),
        ),
        currency=_string(payload, "currency"),
    )


def _policy_payload(policy: ExecutionPolicy) -> dict[str, object]:
    latency_us = (
        (policy.latency.days * 86_400 + policy.latency.seconds) * 1_000_000
        + policy.latency.microseconds
    )
    return {
        "policy_id": policy.policy_id,
        "latency_us": latency_us,
        "slippage_bps": str(policy.slippage_bps),
        "fee_bps": str(policy.fee_bps),
        "fixed_fee": str(policy.fixed_fee),
        "max_fill_fraction": str(policy.max_fill_fraction),
    }


def _policy_from_payload(payload: dict[str, Any]) -> ExecutionPolicy:
    latency_us = _int_value(payload, "latency_us")
    if latency_us < 0:
        raise PaperSessionIntegrityError("execution latency cannot be negative")
    return ExecutionPolicy(
        policy_id=_string(payload, "policy_id"),
        latency=timedelta(microseconds=latency_us),
        slippage_bps=_decimal_value(payload, "slippage_bps"),
        fee_bps=_decimal_value(payload, "fee_bps"),
        fixed_fee=_decimal_value(payload, "fixed_fee"),
        max_fill_fraction=_decimal_value(payload, "max_fill_fraction"),
    )


def _risk_limits_payload(limits: RiskLimits) -> dict[str, object]:
    return {
        "max_abs_position": str(limits.max_abs_position),
        "max_gross_exposure": str(limits.max_gross_exposure),
        "max_net_exposure": str(limits.max_net_exposure),
        "max_session_loss": str(limits.max_session_loss),
        "max_drawdown": str(limits.max_drawdown),
        "allow_short": limits.allow_short,
        "max_leverage": str(limits.max_leverage),
    }


def _risk_limits_from_payload(payload: dict[str, Any]) -> RiskLimits:
    allow_short = payload.get("allow_short")
    if not isinstance(allow_short, bool):
        raise PaperSessionIntegrityError("allow_short must be boolean")
    return RiskLimits(
        max_abs_position=_decimal_value(payload, "max_abs_position"),
        max_gross_exposure=_decimal_value(payload, "max_gross_exposure"),
        max_net_exposure=_decimal_value(payload, "max_net_exposure"),
        max_session_loss=_decimal_value(payload, "max_session_loss"),
        max_drawdown=_decimal_value(payload, "max_drawdown"),
        allow_short=allow_short,
        max_leverage=_decimal_value(payload, "max_leverage"),
    )


def _risk_state_payload(state: RiskState) -> dict[str, str]:
    return {
        "peak_equity": str(state.peak_equity),
        "session_start_equity": str(state.session_start_equity),
    }


def _risk_state_from_payload(payload: dict[str, Any]) -> RiskState:
    return RiskState(
        peak_equity=_decimal_value(payload, "peak_equity"),
        session_start_equity=_decimal_value(payload, "session_start_equity"),
    )


def _account_payload(snapshot: AccountSnapshot) -> dict[str, object]:
    return {
        "cash": str(snapshot.cash),
        "fees": str(snapshot.fees),
        "realized_pnl": str(snapshot.realized_pnl),
        "unrealized_pnl": str(snapshot.unrealized_pnl),
        "equity": str(snapshot.equity),
        "gross_exposure": str(snapshot.gross_exposure),
        "net_exposure": str(snapshot.net_exposure),
        "positions": [
            {
                "instrument": _instrument_payload(item.instrument),
                "quantity": str(item.quantity),
                "average_price": str(item.average_price),
                "realized_pnl": str(item.realized_pnl),
            }
            for item in snapshot.positions
        ],
    }


def _account_from_payload(payload: dict[str, Any]) -> AccountSnapshot:
    raw_positions = payload.get("positions")
    if not isinstance(raw_positions, list):
        raise PaperSessionIntegrityError("account positions must be a list")
    positions: list[Position] = []
    for raw in raw_positions:
        if not isinstance(raw, dict):
            raise PaperSessionIntegrityError("account position must be an object")
        positions.append(
            Position(
                instrument=_instrument_from_payload(_object(raw, "instrument")),
                quantity=_decimal_value(raw, "quantity"),
                average_price=_decimal_value(raw, "average_price"),
                realized_pnl=_decimal_value(raw, "realized_pnl"),
            )
        )
    return AccountSnapshot(
        cash=_decimal_value(payload, "cash"),
        fees=_decimal_value(payload, "fees"),
        realized_pnl=_decimal_value(payload, "realized_pnl"),
        unrealized_pnl=_decimal_value(payload, "unrealized_pnl"),
        equity=_decimal_value(payload, "equity"),
        gross_exposure=_decimal_value(payload, "gross_exposure"),
        net_exposure=_decimal_value(payload, "net_exposure"),
        positions=tuple(positions),
    )


def _marks_payload(marks: dict[str, Decimal]) -> dict[str, str]:
    return {key: str(value) for key, value in sorted(marks.items())}


def _marks_from_payload(payload: dict[str, Any]) -> dict[str, Decimal]:
    marks: dict[str, Decimal] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not key.strip():
            raise PaperSessionIntegrityError("mark instrument identity must not be empty")
        mark = _decimal(value, f"mark:{key}")
        if mark <= 0:
            raise PaperSessionIntegrityError("durable marks must be positive")
        marks[key] = mark
    return marks


def _order_snapshot_from_row(row: sqlite3.Row) -> PaperOrderSnapshot:
    order_json = str(row["order_json"])
    if _sha(order_json) != str(row["order_sha256"]):
        raise PaperSessionIntegrityError("approved order digest mismatch")
    order = _order_from_payload(_json_object(order_json, "order_json"))
    if order.approval_id != str(row["approval_id"]):
        raise PaperSessionIntegrityError("approved order identity changed")
    try:
        state = OrderState(str(row["state"]))
    except ValueError as exc:
        raise PaperSessionIntegrityError("unknown durable order state") from exc
    remaining = _decimal(row["remaining_quantity"], "remaining_quantity")
    terminal_reason = str(row["terminal_reason"])
    last_update_slice = (
        None if row["last_update_slice"] is None else int(row["last_update_slice"])
    )
    expected_state_hash = _order_state_hash(
        order.approval_id,
        state,
        remaining,
        terminal_reason,
        last_update_slice,
    )
    if expected_state_hash != str(row["state_sha256"]):
        raise PaperSessionIntegrityError("durable order state digest mismatch")
    return PaperOrderSnapshot(
        order=order,
        state=state,
        remaining_quantity=remaining,
        terminal_reason=terminal_reason,
        queued_seq=int(row["queued_seq"]),
        last_update_slice=last_update_slice,
    )


def _fill_from_row(row: sqlite3.Row) -> SimulatedFill:
    fill_json = str(row["fill_json"])
    if _sha(fill_json) != str(row["fill_sha256"]):
        raise PaperSessionIntegrityError("paper fill digest mismatch")
    fill = _fill_from_payload(_json_object(fill_json, "fill_json"))
    if fill.fill_id != str(row["fill_id"]):
        raise PaperSessionIntegrityError("paper fill identity changed")
    if fill.approval_id != str(row["approval_id"]):
        raise PaperSessionIntegrityError("paper fill approval identity changed")
    if fill.filled_slice != int(row["filled_slice"]):
        raise PaperSessionIntegrityError("paper fill slice changed")
    return fill


def _validate_order_quantities(
    orders: tuple[PaperOrderSnapshot, ...],
    fills: tuple[SimulatedFill, ...],
) -> None:
    filled: dict[str, Decimal] = {}
    last_fill_slice: dict[str, int] = {}
    for fill in fills:
        filled[fill.approval_id] = filled.get(fill.approval_id, Decimal(0)) + fill.quantity
        last_fill_slice[fill.approval_id] = max(
            fill.filled_slice,
            last_fill_slice.get(fill.approval_id, fill.filled_slice),
        )
    for item in orders:
        total = filled.get(item.order.approval_id, Decimal(0))
        if total > item.order.intent.quantity:
            raise PaperSessionIntegrityError("durable fills exceed approved quantity")
        expected_remaining = item.order.intent.quantity - total
        if item.remaining_quantity != expected_remaining:
            raise PaperSessionIntegrityError(
                "order remaining quantity disagrees with durable fills"
            )
        if item.last_update_slice is not None:
            latest = last_fill_slice.get(item.order.approval_id)
            if latest is not None and latest > item.last_update_slice:
                raise PaperSessionIntegrityError("fill occurred after durable order update")


def _find_order(orders: tuple[PaperOrderSnapshot, ...], approval_id: str) -> PaperOrderSnapshot:
    for item in orders:
        if item.order.approval_id == approval_id:
            return item
    raise PaperSessionError(f"unknown approved order: {approval_id}")


def _order_state_hash(
    approval_id: str,
    state: OrderState,
    remaining_quantity: Decimal,
    terminal_reason: str,
    last_update_slice: int | None,
) -> str:
    return _sha(
        _canonical_json(
            {
                "approval_id": approval_id,
                "state": state.value,
                "remaining_quantity": str(remaining_quantity),
                "terminal_reason": terminal_reason,
                "last_update_slice": last_update_slice,
            }
        )
    )


def _state_hash(
    *,
    config_hash: str,
    risk_json: str,
    account_json: str,
    marks_json: str,
    cursor_slice: int | None,
    cursor_at: str | None,
    last_slice_sha256: object | None,
    row_version: int,
    next_order_seq: int,
) -> str:
    payload = {
        "config_sha256": config_hash,
        "risk": json.loads(risk_json),
        "account": json.loads(account_json),
        "marks": json.loads(marks_json),
        "cursor_slice": cursor_slice,
        "cursor_at": cursor_at,
        "last_slice_sha256": last_slice_sha256,
        "row_version": row_version,
        "next_order_seq": next_order_seq,
    }
    return _sha(_canonical_json(payload))


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _json_object(value: str, field: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise PaperSessionIntegrityError(f"{field} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise PaperSessionIntegrityError(f"{field} must contain a JSON object")
    return payload


def _object(payload: dict[str, Any], field: str) -> dict[str, Any]:
    value = payload.get(field)
    if not isinstance(value, dict):
        raise PaperSessionIntegrityError(f"{field} must be an object")
    return value


def _string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise PaperSessionIntegrityError(f"{field} must be a non-empty string")
    return value


def _optional_string(payload: dict[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PaperSessionIntegrityError(f"{field} must be null or a non-empty string")
    return value


def _int_value(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PaperSessionIntegrityError(f"{field} must be an integer")
    return value


def _decimal_value(payload: dict[str, Any], field: str) -> Decimal:
    if field not in payload:
        raise PaperSessionIntegrityError(f"missing decimal field: {field}")
    return _decimal(payload[field], field)


def _decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        raise PaperSessionIntegrityError(f"{field} must be an exact decimal value")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise PaperSessionIntegrityError(f"{field} is not a valid decimal") from exc
    return _finite_decimal(result, field)


def _finite_decimal(value: Decimal, field: str) -> Decimal:
    result = Decimal(value)
    if not result.is_finite():
        raise PaperSessionIntegrityError(f"{field} must be finite")
    return result


def _require_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise PaperSessionError(f"{field} must not be empty")


def _require_digest(value: str, field: str) -> None:
    if len(value) != 64:
        raise PaperSessionIntegrityError(f"{field} must be a sha256 digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise PaperSessionIntegrityError(f"{field} must be hexadecimal") from exc


def _parse_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise PaperSessionIntegrityError("invalid durable datetime") from exc
