"""Plan 5 §1 paper query: Core permission, tenant isolation and durable recovery."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.trading_research.accounting import PortfolioLedger
from nika_core.trading_research.contracts import Instrument, TradingResearchError, Venue
from nika_core.trading_research.identity import instrument_identity
from nika_core.trading_research.orders import OrderAuthority, Side, SimulatedFill
from nika_core.trading_research.persistence import TradingStateRepository
from nika_core.trading_research.workspace_query import PaperWorkspaceQuery

NOW = datetime(2026, 1, 1, tzinfo=UTC)
INSTRUMENT = Instrument("TEST", Venue("SIM", "UTC"), "USD")


class FakeRepository:
    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def account_payload(self, workspace_id: str, run_id: str):
        self.calls.append((workspace_id, run_id))
        return self.payload


def test_unconfigured_read_is_accessible_but_never_implies_real_trading() -> None:
    repo = FakeRepository()
    query = PaperWorkspaceQuery(repo, authorize_read=lambda ws, run: ws == "mine" and run == "r1")
    data = query.read_account("mine", "r1").to_accessible_state()
    assert data["mode"] == "PAPER_ONLY"
    assert data["state"] == "NO_PAPER_DATA"
    assert data["positions"] == []
    assert data["equity"] is None
    assert repo.calls == [("mine", "r1")]


@pytest.mark.parametrize(
    ("workspace", "run"),
    [("foreign", "r1"), ("mine", "foreign"), ("foreign", "foreign")],
)
def test_foreign_scope_fails_before_any_sql_read(workspace: str, run: str) -> None:
    repo = FakeRepository()
    query = PaperWorkspaceQuery(repo, authorize_read=lambda w, r: (w, r) == ("mine", "r1"))
    with pytest.raises(PermissionError, match="paper workspace read denied"):
        query.read_account(workspace, run)
    assert repo.calls == []


@pytest.mark.parametrize("bad", [None, 1, True, "yes", [], object()])
def test_truthy_or_nonboolean_authority_is_never_accepted(bad: object) -> None:
    repo = FakeRepository()
    query = PaperWorkspaceQuery(repo, authorize_read=lambda _w, _r: bad)
    with pytest.raises(PermissionError, match="paper workspace read denied"):
        query.read_account("mine", "r1")
    assert repo.calls == []


def test_authorization_failure_hides_exceptions_and_does_not_query() -> None:
    repo = FakeRepository()

    def broken(_w: str, _r: str) -> bool:
        raise RuntimeError("SECRET_HOST_AUTH_DETAIL")

    with pytest.raises(PermissionError, match="^paper workspace read denied$") as exc:
        PaperWorkspaceQuery(repo, authorize_read=broken).read_account("mine", "r1")
    assert "SECRET" not in str(exc.value)
    assert repo.calls == []


def test_revocation_during_database_read_is_not_returned_to_ui() -> None:
    repo = FakeRepository()
    calls = 0

    def authorize(_w: str, _r: str) -> bool:
        nonlocal calls
        calls += 1
        return calls == 1

    query = PaperWorkspaceQuery(repo, authorize_read=authorize)
    with pytest.raises(PermissionError, match="paper workspace read denied"):
        query.read_account("mine", "r1")
    assert repo.calls == [("mine", "r1")]
    assert calls == 2


def test_invalid_scope_and_hostile_str_subclass_rejected_before_authority() -> None:
    repo = FakeRepository()
    events: list[str] = []

    class Hostile(str):
        def strip(self, *_a, **_kw):
            events.append("strip")
            raise AssertionError("behavioral scope callback")

    query = PaperWorkspaceQuery(
        repo, authorize_read=lambda _w, _r: events.append("authorize") or True
    )
    for scope in (None, "", "   ", Hostile("mine")):
        with pytest.raises(TradingResearchError, match="invalid paper workspace scope"):
            query.read_account(scope, "r1")
    assert events == []
    assert repo.calls == []


def test_restarted_sqlite_account_is_paper_only_and_has_text_first_projection(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repo = TradingStateRepository(store)
    repo.initialize()
    authority = OrderAuthority("mine", "r1", "order", NOW, 0)
    fill = SimulatedFill(
        "fill-1", "approval-1", "intent-1", authority,
        INSTRUMENT, Side.BUY, Decimal("2"), Decimal("100"),
        Decimal("1"), NOW, 1,
    )
    ledger = PortfolioLedger(Decimal("1000"))
    ledger.apply_fill(fill)
    snapshot = ledger.snapshot({instrument_identity(INSTRUMENT): Decimal("100")})
    assert repo.commit_fill_and_account(fill, snapshot)

    restarted = TradingStateRepository(SQLiteStore(tmp_path / "nika.db"))
    restarted.initialize()
    query = PaperWorkspaceQuery(
        restarted, authorize_read=lambda ws, run: ws == "mine" and run == "r1"
    )
    view = query.read_account("mine", "r1").to_accessible_state()
    assert view["mode"] == "PAPER_ONLY"
    assert view["state"] == "PAPER_DATA"
    assert view["cash"] == "799"
    assert view["equity"] == "999"
    assert view["fees"] == "1"
    assert view["positions"] == [{
        "venue": "SIM", "instrument": "TEST", "currency": "USD",
        "quantity": "2", "average_price": "100", "realized_pnl": "0",
    }]

    with store.connection() as conn:
        conn.execute(
            "UPDATE trading_research_run_account_state SET payload = ? "
            "WHERE workspace_id = ? AND run_id = ?", ("{", "mine", "r1"),
        )
    with pytest.raises(TradingResearchError, match="paper account evidence unavailable"):
        query.read_account("mine", "r1")


def test_unsafe_operator_identity_is_not_rendered_in_accessible_state() -> None:
    repo = FakeRepository({
        "positions": [{
            "venue_id": "SIM",
            "instrument_id": "fake\\u202eorder".encode().decode("unicode_escape"),
            "currency": "USD",
            "quantity": "2", "average_price": "100", "realized_pnl": "0",
        }],
        "cash": "799", "equity": "999", "gross_exposure": "200",
        "net_exposure": "200", "fees": "1", "realized_pnl": "0", "unrealized_pnl": "0",
    })
    query = PaperWorkspaceQuery(repo, authorize_read=lambda _w, _r: True)
    with pytest.raises(TradingResearchError, match="paper account evidence unavailable"):
        query.read_account("mine", "r1")


def test_existing_ui_bridge_renders_shared_paper_state_without_a_new_runtime(tmp_path) -> None:
    from nika_core.kernel.action_registry import ActionRegistry, Keymap
    from nika_core.ui.bridge import UIActionBridge
    from nika_core.trading_research.workspace_query import paper_state_provider

    repo = FakeRepository()
    query = PaperWorkspaceQuery(repo, authorize_read=lambda w, r: (w, r) == ("mine", "r1"))
    registry = ActionRegistry()
    bridge = UIActionBridge(
        registry,
        Keymap(SQLiteStore(tmp_path / "nika.db"), registry),
        state_provider=paper_state_provider(query, host_scope=lambda: ("mine", "r1")),
    )
    result = bridge.get_state()
    assert result["ok"] is True
    assert result["state"]["mode"] == "PAPER_ONLY"
    assert result["state"]["state"] == "NO_PAPER_DATA"

    # A client cannot select another workspace via a UI command payload.
    assert repo.calls == [("mine", "r1")]


def test_semantic_ui_adapter_reports_denial_and_corruption_not_empty(tmp_path) -> None:
    from nika_core.kernel.action_registry import ActionRegistry, Keymap
    from nika_core.ui.bridge import UIActionBridge
    from nika_core.trading_research.workspace_query import paper_state_provider

    repo = FakeRepository()
    grants = False
    query = PaperWorkspaceQuery(repo, authorize_read=lambda _w, _r: grants)
    registry = ActionRegistry()
    bridge = UIActionBridge(
        registry,
        Keymap(SQLiteStore(tmp_path / "nika.db"), registry),
        state_provider=paper_state_provider(query, host_scope=lambda: ("mine", "r1")),
    )
    denied = bridge.get_state()["state"]
    assert denied["state"] == "ACCESS_DENIED"
    assert "balance" not in denied
    assert repo.calls == []

    grants = True
    repo.payload = {"positions": "corrupted"}
    unavailable = bridge.get_state()["state"]
    assert unavailable["state"] == "EVIDENCE_UNAVAILABLE"
    assert "cash" not in unavailable
    assert repo.calls == [("mine", "r1")]


def test_invalid_host_identity_never_queries_trader_repository() -> None:
    from nika_core.trading_research.workspace_query import paper_state_provider

    repo = FakeRepository()
    query = PaperWorkspaceQuery(repo, authorize_read=lambda _w, _r: True)
    projection = paper_state_provider(query, host_scope=lambda: ("mine", "r1", "foreign"))
    assert projection()["state"] == "ACCESS_DENIED"
    assert repo.calls == []
