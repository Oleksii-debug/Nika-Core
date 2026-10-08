"""Plan 5 §1: SQLite paper evidence accepts only inert canonical domain records."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.trading_research.accounting import AccountSnapshot, PortfolioLedger
from nika_core.trading_research.contracts import Instrument, TradingResearchError, Venue
from nika_core.trading_research.identity import instrument_identity
from nika_core.trading_research.orders import OrderAuthority, Side, SimulatedFill
from nika_core.trading_research.persistence import TradingStateRepository


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _fill() -> SimulatedFill:
    return SimulatedFill(
        "fill-1", "approval-1", "intent-1",
        OrderAuthority("trader", "paper-run", "order-1", NOW, 0),
        Instrument("ABC", Venue("PAPER", "UTC"), "USD"),
        Side.BUY, Decimal("2"), Decimal("100"), Decimal("1"), NOW, 1,
    )


def _snapshot(fill: SimulatedFill) -> AccountSnapshot:
    ledger = PortfolioLedger(Decimal("1000"))
    ledger.apply_fill(fill)
    return ledger.snapshot({instrument_identity(fill.instrument): Decimal("100")})


def _repository(tmp_path) -> TradingStateRepository:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repo = TradingStateRepository(store)
    repo.initialize()
    return repo


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("quantity", Decimal("NaN")),
        ("quantity", Decimal("Infinity")),
        ("quantity", True),
        ("price", Decimal("-1")),
        ("fee", Decimal("Infinity")),
        ("fee", 1.0),
        ("side", "buy"),
        ("filled_slice", True),
        ("fill_id", 123),
        ("approval_id", 5),
    ],
)
def test_forged_fill_rejected_without_durable_effect(tmp_path, field, replacement) -> None:
    repo = _repository(tmp_path)
    fill = _fill()
    snapshot = _snapshot(fill)
    object.__setattr__(fill, field, replacement)

    with pytest.raises(TradingResearchError):
        repo.commit_fill_and_account(fill, snapshot)
    assert repo.fill_count("trader", "paper-run") == 0
    assert repo.account_payload("trader", "paper-run") is None

    corrected = _fill()
    assert repo.commit_fill_and_account(corrected, _snapshot(corrected))
    assert repo.fill_count("trader", "paper-run") == 1


def test_numeric_subclass_never_executes_custom_string_conversion(tmp_path) -> None:
    repo = _repository(tmp_path)
    fill = _fill()
    snapshot = _snapshot(fill)
    called: list[str] = []

    class BehavioralDecimal(Decimal):
        def __str__(self):
            called.append("to-string")
            raise AssertionError("behavioral numeric coercion")

    object.__setattr__(fill, "price", BehavioralDecimal("100"))
    with pytest.raises(TradingResearchError, match="finite Decimal"):
        repo.commit_fill_and_account(fill, snapshot)
    assert called == []
    assert repo.fill_count("trader", "paper-run") == 0


def test_forged_timezone_does_not_execute_callbacks(tmp_path) -> None:
    repo = _repository(tmp_path)
    fill = _fill()
    snapshot = _snapshot(fill)
    called: list[str] = []

    class CallbackZone(tzinfo):
        def utcoffset(self, dt):
            called.append("timezone callback")
            return timedelta(0)

        def dst(self, dt):
            return timedelta(0)

    # datetime can contain an arbitrary tzinfo object even when its type is builtin.
    object.__setattr__(
        fill, "filled_at", datetime(2026, 1, 1, tzinfo=CallbackZone()),
    )
    with pytest.raises(TradingResearchError, match="canonical timezone"):
        repo.commit_fill_and_account(fill, snapshot)
    assert called == []
    assert repo.fill_count("trader", "paper-run") == 0


def test_mutated_instrument_and_authority_identity_rejected(tmp_path) -> None:
    repo = _repository(tmp_path)
    fill = _fill()
    snapshot = _snapshot(fill)
    object.__setattr__(fill.instrument, "currency", "usd")
    with pytest.raises(TradingResearchError, match="instrument identity"):
        repo.commit_fill_and_account(fill, snapshot)
    assert repo.fill_count("trader", "paper-run") == 0

    repaired = _fill()
    object.__setattr__(repaired.authority, "submitted_slice", False)
    with pytest.raises(TradingResearchError, match="submitted_slice"):
        repo.commit_fill_and_account(repaired, snapshot)
    assert repo.fill_count("trader", "paper-run") == 0


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("cash", True),
        ("fees", Decimal("NaN")),
        ("equity", 999.0),
        ("gross_exposure", Decimal("Infinity")),
        ("positions", []),
    ],
)
def test_forged_snapshot_rejected_before_serialization(tmp_path, field, replacement) -> None:
    repo = _repository(tmp_path)
    fill = _fill()
    snapshot = _snapshot(fill)
    object.__setattr__(snapshot, field, replacement)
    with pytest.raises(TradingResearchError):
        repo.commit_fill_and_account(fill, snapshot)
    assert repo.fill_count("trader", "paper-run") == 0
    assert repo.account_payload("trader", "paper-run") is None


def test_nested_position_decimal_subclass_cannot_coerce_into_json(tmp_path) -> None:
    repo = _repository(tmp_path)
    fill = _fill()
    snapshot = _snapshot(fill)
    called: list[str] = []

    class BehavioralDecimal(Decimal):
        def __str__(self):
            called.append("position string")
            raise AssertionError("unexpected conversion")

    position = replace(snapshot.positions[0], quantity=BehavioralDecimal("2"))
    object.__setattr__(snapshot, "positions", (position,))
    with pytest.raises(TradingResearchError, match="position quantity"):
        repo.commit_fill_and_account(fill, snapshot)
    assert called == []
    assert repo.fill_count("trader", "paper-run") == 0


def test_plain_durable_fill_and_snapshot_remain_exactly_once_after_restart(tmp_path) -> None:
    repo = _repository(tmp_path)
    fill = _fill()
    snapshot = _snapshot(fill)
    assert repo.commit_fill_and_account(fill, snapshot) is True
    reopened = _repository(tmp_path)
    assert reopened.commit_fill_and_account(fill, snapshot) is False
    assert reopened.fill_count("trader", "paper-run") == 1
    payload = reopened.account_payload("trader", "paper-run")
    assert payload is not None
    assert payload["cash"] == "799"


def test_sqlite_uses_detached_records_not_post_admission_mutations(
    tmp_path, monkeypatch,
) -> None:
    import nika_core.trading_research.persistence as persistence

    repo = _repository(tmp_path)
    fill = _fill()
    snapshot = _snapshot(fill)
    original_fill_admission = persistence._require_durable_fill
    original_snapshot_admission = persistence._require_durable_snapshot

    def mutate_original_fill(value):
        detached = original_fill_admission(value)
        object.__setattr__(value, "quantity", Decimal("999"))
        return detached

    def mutate_original_snapshot(value):
        detached = original_snapshot_admission(value)
        object.__setattr__(value, "cash", Decimal("1"))
        return detached

    monkeypatch.setattr(persistence, "_require_durable_fill", mutate_original_fill)
    monkeypatch.setattr(persistence, "_require_durable_snapshot", mutate_original_snapshot)

    assert repo.commit_fill_and_account(fill, snapshot) is True
    with repo._store.connection() as conn:
        row = conn.execute(
            "SELECT quantity FROM trading_research_run_fills WHERE "
            "workspace_id = ? AND run_id = ? AND fill_id = ?",
            ("trader", "paper-run", "fill-1"),
        ).fetchone()
    assert row["quantity"] == "2"
    assert repo.account_payload("trader", "paper-run")["cash"] == "799"
