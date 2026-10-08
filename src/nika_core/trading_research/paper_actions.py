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


PAPER_POSITIONS_INSPECT = "trader.paper.positions.inspect"
_PAPER_POSITIONS_PAGE_SIZE = 10
_MAX_PAPER_POSITIONS_PAGE = 10_000


def paper_positions_definition() -> ActionDefinition:
    """Read-only, remappable semantic action using the incumbent Core registry."""
    return ActionDefinition(
        action_id=PAPER_POSITIONS_INSPECT,
        label="Переглянути PAPER-позиції",
        category="AI Trader",
        default_binding=None,
        scope="trader.paper",
        may_be_unbound=True,
    )


def paper_positions_handler(
    query: PaperWorkspaceQuery,
    *,
    host_scope: Callable[[], tuple[str, str]],
) -> Callable[[Mapping[str, Any]], str]:
    """Page through Core-authorized PAPER positions; no UI-supplied scope/grants.

    A command may select a bounded page only. Each invocation re-reads the
    canonical store and rechecks the host's revocable Core authorization.
    Never log, return or echo rejected caller payloads or provider exceptions.
    """
    provider = paper_state_provider(query, host_scope=host_scope)

    def handle(payload: Mapping[str, Any]) -> str:
        if (
            type(payload) is not dict
            or len(payload) > 1
            or any(type(key) is not str for key in payload)
            or (payload and "page" not in payload)
        ):
            raise ValueError("Команда PAPER-позицій приймає лише номер сторінки.")
        page = payload.get("page", 0)
        if type(page) is not int or not 0 <= page <= _MAX_PAPER_POSITIONS_PAGE:
            raise ValueError("Неприпустимий номер сторінки PAPER-позицій.")

        state = provider()
        kind = state.get("state")
        if kind == "ACCESS_DENIED":
            raise ValueError("Доступ до PAPER-позицій заборонено.")
        if kind == "EVIDENCE_UNAVAILABLE":
            raise ValueError("Дані PAPER-позицій недоступні.")
        if kind == "NO_PAPER_DATA":
            if page:
                raise ValueError("Сторінку PAPER-позицій не знайдено.")
            return "Лише PAPER: записів рахунку поки немає."
        if kind != "PAPER_DATA":
            raise ValueError("Стан PAPER-позицій недоступний.")

        positions = state.get("positions")
        if type(positions) is not list:
            raise ValueError("Стан PAPER-позицій недоступний.")
        if not positions:
            if page:
                raise ValueError("Сторінку PAPER-позицій не знайдено.")
            return "Лише PAPER: відкритих позицій немає."

        start = page * _PAPER_POSITIONS_PAGE_SIZE
        if start >= len(positions):
            raise ValueError("Сторінку PAPER-позицій не знайдено.")
        end = min(start + _PAPER_POSITIONS_PAGE_SIZE, len(positions))
        lines = [
            f"Лише PAPER: позиції {start + 1}–{end} із {len(positions)}; "
            f"сторінка {page + 1}."
        ]
        for index in range(start, end):
            item = positions[index]
            if type(item) is not dict or set(item) != {
                "venue", "venue_timezone", "instrument", "currency",
                "quantity", "average_price", "realized_pnl",
            } or any(type(value) is not str for value in item.values()):
                raise ValueError("Стан PAPER-позицій недоступний.")
            lines.append(
                f"Позиція {index + 1}: майданчик {item['venue']}; "
                f"часовий пояс {item['venue_timezone']}; "
                f"інструмент {item['instrument']}; валюта {item['currency']}; "
                f"кількість {item['quantity']}; середня ціна {item['average_price']}; "
                f"реалізований PnL {item['realized_pnl']}."
            )
        if end < len(positions):
            lines.append(f"Для наступних позицій відкрийте сторінку {page + 2}.")
        message = "\n".join(lines)
        # Bounded screen-reader and bridge response, with no partial/truncated
        # position evidence. A broken provider must not leak arbitrary text.
        if len(message.encode("utf-8")) > 32_768:
            raise ValueError("Стан PAPER-позицій недоступний.")
        return message

    return handle
