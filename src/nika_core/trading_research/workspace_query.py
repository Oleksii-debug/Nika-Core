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
        or any(ord(character) < 32 for character in value)
    ):
        raise TradingResearchError("paper operator projection contains unsafe identity")
    return value


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
        if (
            type(workspace_id) is not str
            or type(run_id) is not str
            or not workspace_id.strip()
            or not run_id.strip()
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
                positions = payload["positions"]
                if type(positions) is not list:
                    raise TradingResearchError("invalid paper position projection")
                projected: list[PaperPositionView] = []
                for row in positions:
                    if type(row) is not dict:
                        raise TradingResearchError("invalid paper position projection")
                    projected.append(
                        PaperPositionView(
                            _safe_identity(row["venue_id"]),
                            _safe_identity(row["instrument_id"]),
                            _safe_identity(row["currency"]),
                            row["quantity"],
                            row["average_price"],
                            row["realized_pnl"],
                        )
                    )
                result = PaperAccountView(
                    "PAPER_DATA",
                    payload["cash"], payload["equity"],
                    payload["gross_exposure"], payload["net_exposure"],
                    payload["fees"], payload["realized_pnl"],
                    payload["unrealized_pnl"], tuple(projected),
                )
        except (RuntimeError, ValueError, KeyError, TypeError, sqlite3.Error):
            # Corrupt durable evidence is neither NOT_CONFIGURED nor empty/healthy.
            raise TradingResearchError("paper account evidence unavailable") from None
        # Re-evaluate authorization to catch revocation during the storage read.
        self._require_authorized(workspace_id, run_id)
        return result
