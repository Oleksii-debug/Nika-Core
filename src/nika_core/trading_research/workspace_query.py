"""Authorized, presentation-neutral read model for existing durable PAPER evidence.

This is a read-only adapter over TradingStateRepository, not a broker, an order
command, an agent runtime, a permission database, or an accounting authority.
The host must bind authorize to canonical Core policy and current identity;
never forward a browser/model-supplied "authorized" flag as this callback.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from .contracts import TradingResearchError
from .persistence import TradingStateRepository

PaperState = Literal["NO_PAPER_DATA", "PAPER_DATA"]


@dataclass(frozen=True, slots=True)
class PaperPositionView:
    venue: str
    instrument: str
    currency: str
    quantity: str
    average_price: str
    realized_pnl: str


@dataclass(frozen=True, slots=True)
class PaperAccountView:
    """Text-first state consumable by Windows UIA, semantic Web and reports."""

    state: PaperState
    cash: str | None
    equity: str | None
    gross_exposure: str | None
    net_exposure: str | None
    fees: str | None
    realized_pnl: str | None
    unrealized_pnl: str | None
    positions: tuple[PaperPositionView, ...]

    def to_accessible_state(self) -> dict[str, object]:
        """Return inert plain values; no UI-side authority or secret-bearing objects."""
        return {
            "mode": "PAPER_ONLY",
            "state": self.state,
            "cash": self.cash,
            "equity": self.equity,
            "gross_exposure": self.gross_exposure,
            "net_exposure": self.net_exposure,
            "fees": self.fees,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "positions": [
                {
                    "venue": position.venue,
                    "instrument": position.instrument,
                    "currency": position.currency,
                    "quantity": position.quantity,
                    "average_price": position.average_price,
                    "realized_pnl": position.realized_pnl,
                }
                for position in self.positions
            ],
        }


def _safe_identity(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or not value.isprintable()
        or value != value.strip()
        or len(value.encode("utf-8")) > 512
    ):
        raise TradingResearchError("paper operator projection contains unsafe identity")
    return value


def _safe_amount(value: object) -> str:
    """Re-admit inert, finite and bounded text from the durable read adapter.

    A storage decoder normally validates these values. Enforce the projection
    contract again so a broken/replaced adapter cannot execute str hooks or
    export non-finite, non-text or unbounded values to a UI client.
    """
    if (
        type(value) is not str
        or not value
        or not value.isprintable()
        or value != value.strip()
        or len(value.encode("utf-8")) > 128
    ):
        raise TradingResearchError("unsafe paper amount projection")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise TradingResearchError("unsafe paper amount projection") from exc
    if not parsed.is_finite():
        raise TradingResearchError("unsafe paper amount projection")
    return value


def _valid_operator_scope(value: object) -> bool:
    """Require inert, printable, bounded identifiers before Core or SQLite.

    The host may select the scope, but neither the authorization callback nor
    its audit/log consumers should receive control/bidi or unbounded text.
    """
    return (
        type(value) is str
        and bool(value)
        and value.isprintable()
        and value == value.strip()
        and len(value.encode("utf-8")) <= 512
    )


class PaperWorkspaceQuery:
    """Check host-owned Core read permission on every request and before return.

    Both workspace and run must match the host's authorization decision.
    A failure is never rendered as a healthy empty account.
    """

    def __init__(
        self,
        repository: TradingStateRepository,
        *,
        authorize_read: Callable[[str, str], bool],
    ) -> None:
        if not callable(authorize_read):
            raise TypeError("host Core authorization callback is required")
        self._repository = repository
        self._authorize_read = authorize_read

    def _require_authorized(self, workspace_id: str, run_id: str) -> None:
        try:
            permitted = self._authorize_read(workspace_id, run_id)
        except Exception:
            raise PermissionError("paper workspace read denied") from None
        if permitted is not True:
            raise PermissionError("paper workspace read denied")

    def read_account(self, workspace_id: str, run_id: str) -> PaperAccountView:
        # Exact input types prevent behavioral str subclasses running in auth/SQLite.
        if not (
            _valid_operator_scope(workspace_id)
            and _valid_operator_scope(run_id)
        ):
            raise TradingResearchError("invalid paper workspace scope")
        self._require_authorized(workspace_id, run_id)
        try:
            payload = self._repository.account_payload(workspace_id, run_id)
            if payload is None:
                result = PaperAccountView(
                    "NO_PAPER_DATA", None, None, None, None, None, None, None, ()
                )
            else:
                required = {
                    "cash", "equity", "gross_exposure", "net_exposure",
                    "fees", "realized_pnl", "unrealized_pnl", "positions",
                }
                if type(payload) is not dict or set(payload) != required:
                    raise TradingResearchError("invalid paper account projection")
                positions = payload["positions"]
                if type(positions) is not list or len(positions) > 100_000:
                    raise TradingResearchError("invalid paper position projection")
                projected: list[PaperPositionView] = []
                expected_position = {
                    "venue_id", "instrument_id", "currency",
                    "quantity", "average_price", "realized_pnl",
                    "venue_timezone",
                }
                for row in positions:
                    if type(row) is not dict or set(row) != expected_position:
                        raise TradingResearchError("invalid paper position projection")
                    currency = _safe_identity(row["currency"])
                    if (
                        len(currency) != 3
                        or not currency.isascii()
                        or not currency.isalpha()
                        or not currency.isupper()
                    ):
                        raise TradingResearchError("invalid paper currency projection")
                    projected.append(
                        PaperPositionView(
                            _safe_identity(row["venue_id"]),
                            _safe_identity(row["instrument_id"]),
                            currency,
                            _safe_amount(row["quantity"]),
                            _safe_amount(row["average_price"]),
                            _safe_amount(row["realized_pnl"]),
                        )
                    )
                result = PaperAccountView(
                    "PAPER_DATA",
                    _safe_amount(payload["cash"]),
                    _safe_amount(payload["equity"]),
                    _safe_amount(payload["gross_exposure"]),
                    _safe_amount(payload["net_exposure"]),
                    _safe_amount(payload["fees"]),
                    _safe_amount(payload["realized_pnl"]),
                    _safe_amount(payload["unrealized_pnl"]),
                    tuple(projected),
                )
        except (RuntimeError, ValueError, KeyError, TypeError, sqlite3.Error):
            # Permission may be revoked *during* a failed SQLite/decode read.
            # Recheck before classifying an error as EVIDENCE_UNAVAILABLE:
            # otherwise a revoked caller learns whether this account is corrupt.
            self._require_authorized(workspace_id, run_id)
            raise TradingResearchError("paper account evidence unavailable") from None
        # Re-evaluate authorization to catch revocation during the storage read.
        self._require_authorized(workspace_id, run_id)
        return result


def paper_state_provider(
    query: PaperWorkspaceQuery,
    *,
    host_scope: Callable[[], tuple[str, str]],
) -> Callable[[], dict[str, object]]:
    """Adapt authorized paper evidence to the existing UIActionBridge StateProvider.

    The scope and Core permission callback come from the trusted host, never
    from UICommand payload, model output, browser storage, or a workspace label.
    Failures are explicit text states; they never fabricate an empty portfolio.
    """
    def state() -> dict[str, object]:
        try:
            scope = host_scope()
            if (
                type(scope) is not tuple
                or len(scope) != 2
                or not _valid_operator_scope(scope[0])
                or not _valid_operator_scope(scope[1])
            ):
                raise PermissionError("invalid trusted host scope")
            return query.read_account(scope[0], scope[1]).to_accessible_state()
        except PermissionError:
            return {
                "mode": "PAPER_ONLY", "state": "ACCESS_DENIED",
                "message": "Paper workspace access is not authorized.",
            }
        except (TradingResearchError, RuntimeError, sqlite3.Error):
            return {
                "mode": "PAPER_ONLY", "state": "EVIDENCE_UNAVAILABLE",
                "message": "Paper workspace evidence is unavailable; no balance is shown.",
            }
        except Exception:
            # A broken host identity/provider is not a trading-empty response.
            return {
                "mode": "PAPER_ONLY", "state": "ACCESS_DENIED",
                "message": "Paper workspace access is not authorized.",
            }

    return state
