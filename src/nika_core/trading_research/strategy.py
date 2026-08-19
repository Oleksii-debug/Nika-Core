from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from .contracts import require_aware_utc
from .dataset import TemporalView


@dataclass(frozen=True, slots=True)
class DecisionContext:
    decision_at: datetime
    market: TemporalView

    def __post_init__(self) -> None:
        decision_at = require_aware_utc(self.decision_at, "decision_at")
        if self.market.at != decision_at:
            raise ValueError("DecisionContext time must exactly match TemporalView time")
        object.__setattr__(self, "decision_at", decision_at)


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    action: str
    quantity: Decimal = Decimal(0)
    reason: str = ""


class Strategy(Protocol):
    def decide(self, context: DecisionContext) -> StrategyDecision: ...
