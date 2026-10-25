"""Read-only AI Trader action wired through the existing Core UI Action Registry.

A trusted host installs this definition and handler into its canonical
UIActionBridge. UI/model payload never selects a workspace, run, grant or broker.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from nika_core.kernel.action_registry import ActionDefinition

from .workspace_query import PaperWorkspaceQuery, paper_state_provider

PAPER_ACCOUNT_INSPECT = "trader.paper.account.inspect"


def paper_inspect_definition() -> ActionDefinition:
    """Stable, remappable action; no hard-coded shortcut or second UI kernel."""
    return ActionDefinition(
        action_id=PAPER_ACCOUNT_INSPECT,
        label="Перевірити PAPER-рахунок",
        category="AI Trader",
        default_binding=None,
        scope="trader.paper",
        may_be_unbound=True,
    )


def paper_inspect_handler(
    query: PaperWorkspaceQuery,
    *,
    host_scope: Callable[[], tuple[str, str]],
) -> Callable[[Mapping[str, Any]], str]:
    """Build a command handler for the canonical UIActionBridge.

    Never accept a workspace/run from UI payload. The query and its Core
    authorization callback are the *only* read authority. Denials and faults
    are bounded ValueError messages that UIActionBridge renders as rejected.
    """
    provider = paper_state_provider(query, host_scope=host_scope)

    def handle(payload: Mapping[str, Any]) -> str:
        if type(payload) is not dict or payload:
            raise ValueError("Команда PAPER-рахунку не приймає параметрів.")
        state = provider()
        kind = state.get("state")
        if kind == "ACCESS_DENIED":
            raise ValueError("Доступ до PAPER-рахунку заборонено.")
        if kind == "EVIDENCE_UNAVAILABLE":
            raise ValueError("Дані PAPER-рахунку недоступні.")
        if kind == "NO_PAPER_DATA":
            return "Лише PAPER: записів рахунку поки немає."
        if kind != "PAPER_DATA":
            raise ValueError("Стан PAPER-рахунку недоступний.")
        positions = state["positions"]
        equity = state["equity"]
        # Both values originate from the bounded, re-admitted query projection.
        if type(positions) is not list or type(equity) is not str:
            raise ValueError("Стан PAPER-рахунку недоступний.")
        return f"Лише PAPER: капітал {equity}; позицій {len(positions)}."

    return handle
