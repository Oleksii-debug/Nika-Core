from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.trading_research.accounting import AccountSnapshot, PortfolioLedger, Position
from nika_core.trading_research.contracts import Instrument, Venue
from nika_core.trading_research.identity import instrument_identity
from nika_core.trading_research.orders import OrderAuthority, Side, SimulatedFill
from nika_core.trading_research.persistence import (
    TradingStateRepository,
    _decode_account_payload,
    _snapshot_payload,
)


def _empty_snapshot() -> AccountSnapshot:
    return AccountSnapshot(
        cash=Decimal("10"),
        fees=Decimal(0),
        realized_pnl=Decimal(0),
        unrealized_pnl=Decimal(0),
        equity=Decimal("10"),
        gross_exposure=Decimal(0),
        net_exposure=Decimal(0),
        positions=(),
    )


def _open_snapshot() -> AccountSnapshot:
    instrument = Instrument("ABC", Venue("SIM"), "USD")
    return AccountSnapshot(
        cash=Decimal("10"),
        fees=Decimal(0),
        realized_pnl=Decimal("1"),
        unrealized_pnl=Decimal("2"),
        equity=Decimal("18"),
        gross_exposure=Decimal("8"),
        net_exposure=Decimal("8"),
        positions=(Position(instrument, Decimal("2"), Decimal("3"), Decimal("1")),),
    )


def test_valid_snapshot_round_trip_is_preserved() -> None:
    for snapshot in (_empty_snapshot(), _open_snapshot()):
        text = _snapshot_payload(snapshot)
        assert _decode_account_payload(text) == json.loads(text)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("cash", "NaN"),
        ("cash", "Infinity"),
        ("cash", "not-a-number"),
        ("cash", 10),
        ("cash", True),
        ("fees", "-1"),
        ("gross_exposure", "-1"),
        ("net_exposure", "1"),
        ("equity", "11"),
        ("positions", {}),
    ],
)
def test_account_payload_rejects_malformed_amounts_and_totals(
    field: str, replacement: object
) -> None:
    value = json.loads(_snapshot_payload(_empty_snapshot()))
    value[field] = replacement
    with pytest.raises(RuntimeError):
        _decode_account_payload(json.dumps(value))


@pytest.mark.parametrize(
    "raw",
    [
        '{"cash":"10","cash":"20"}',
        '{"cash":NaN}',
        '{"cash":Infinity}',
        '[]',
        'null',
        '{',
    ],
)
def test_account_payload_rejects_ambiguous_or_invalid_json(raw: str) -> None:
    with pytest.raises(RuntimeError):
        _decode_account_payload(raw)


def test_unknown_account_field_is_rejected() -> None:
    value = json.loads(_snapshot_payload(_empty_snapshot()))
    value["injected"] = "data"
    with pytest.raises(RuntimeError, match="fields"):
        _decode_account_payload(json.dumps(value))


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("quantity", "NaN"),
        ("average_price", "-1"),
        ("average_price", "0"),
        ("realized_pnl", "Infinity"),
        ("currency", "usd"),
        ("venue_id", ""),
        ("instrument_id", "A" + chr(10) + "B"),
    ],
)
def test_position_payload_fails_closed(
    field: str, replacement: object
) -> None:
    value = json.loads(_snapshot_payload(_open_snapshot()))
    value["positions"][0][field] = replacement
    with pytest.raises(RuntimeError):
        _decode_account_payload(json.dumps(value))


def test_duplicate_position_identity_and_realized_pnl_tampering_are_rejected() -> None:
    value = json.loads(_snapshot_payload(_open_snapshot()))
    value["positions"].append(value["positions"][0].copy())
    with pytest.raises(RuntimeError, match="duplicate"):
        _decode_account_payload(json.dumps(value))
    value["positions"].pop()
    value["realized_pnl"] = "0"
    with pytest.raises(RuntimeError, match="realized"):
        _decode_account_payload(json.dumps(value))


def _fill() -> SimulatedFill:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    instrument = Instrument("ABC", Venue("SIM"), "USD")
    return SimulatedFill(
        fill_id="fill-1",
        approval_id="risk-1",
        intent_id="intent-1",
        authority=OrderAuthority("trader-workspace", "paper-run", "order-1", now, 0),
        instrument=instrument,
        side=Side.BUY,
        quantity=Decimal("2"),
        price=Decimal("100"),
        fee=Decimal("1"),
        filled_at=now,
        filled_slice=1,
    )


def test_invalid_snapshot_does_not_commit_fill_or_account(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = TradingStateRepository(store)
    repository.initialize()
    fill = _fill()
    ledger = PortfolioLedger(Decimal("1000"))
    ledger.apply_fill(fill)
    valid = ledger.snapshot({instrument_identity(fill.instrument): Decimal("100")})
    forged = replace(valid, equity=Decimal("9999"))
    with pytest.raises(RuntimeError, match="equity"):
        repository.commit_fill_and_account(fill, forged)
    assert repository.fill_count("trader-workspace", "paper-run") == 0
    assert repository.account_payload("trader-workspace", "paper-run") is None


def test_corrupted_snapshot_after_restart_never_becomes_account_truth(tmp_path) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    repository = TradingStateRepository(store)
    repository.initialize()
    fill = _fill()
    ledger = PortfolioLedger(Decimal("1000"))
    ledger.apply_fill(fill)
    valid = ledger.snapshot({instrument_identity(fill.instrument): Decimal("100")})
    assert repository.commit_fill_and_account(fill, valid) is True

    reopened = TradingStateRepository(SQLiteStore(path))
    reopened.initialize()
    original = reopened.account_payload("trader-workspace", "paper-run")
    assert original is not None
    assert original["cash"] == "799"
    forged = dict(original, cash="NaN")
    with store.connection() as conn:
        conn.execute(
            "UPDATE trading_research_run_account_state SET payload = ? "
            "WHERE workspace_id = ? AND run_id = ?",
            (json.dumps(forged), "trader-workspace", "paper-run"),
        )
    reopened_again = TradingStateRepository(SQLiteStore(path))
    reopened_again.initialize()
    with pytest.raises(RuntimeError, match="non-finite"):
        reopened_again.account_payload("trader-workspace", "paper-run")
    with pytest.raises(RuntimeError, match="conflicting durable account state"):
        reopened_again.commit_fill_and_account(fill, valid)
    assert reopened_again.fill_count("trader-workspace", "paper-run") == 1
