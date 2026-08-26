from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import OddsSnapshot, PeriodState, ScoreState, Settlement, SportsbookSource
from .repository import SportsbookRepository


@dataclass(frozen=True, slots=True)
class SourceBatch:
    next_cursor: str
    odds: tuple[OddsSnapshot, ...] = ()
    scores: tuple[ScoreState, ...] = ()
    periods: tuple[PeriodState, ...] = ()
    settlements: tuple[Settlement, ...] = ()


class SportsbookDataPort(Protocol):
    """Read-only provider contract; deliberately exposes no wagering operations."""

    def fetch_updates(self, *, cursor: str | None) -> SourceBatch: ...


class SportsbookResearchService:
    def __init__(self, repository: SportsbookRepository) -> None:
        self._repository = repository

    def sync(self, source: SportsbookSource, provider: SportsbookDataPort) -> SourceBatch:
        self._repository.register_source(source)
        cursor = self._repository.cursor(source.source_id)
        batch = provider.fetch_updates(cursor=cursor)
        self._repository.apply_batch(
            source_id=source.source_id,
            expected_cursor=cursor,
            next_cursor=batch.next_cursor,
            odds=batch.odds,
            scores=batch.scores,
            periods=batch.periods,
            settlements=batch.settlements,
        )
        return batch
