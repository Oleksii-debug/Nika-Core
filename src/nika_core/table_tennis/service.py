from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .contracts import IngestResult, MatchObservation, PlayerStats, StatsSnapshot
from .repository import TableTennisRepository


@dataclass(slots=True)
class _MutableStats:
    display_name: str
    display_key: tuple[datetime, str, str, int]
    matches: int = 0
    wins: int = 0
    losses: int = 0
    sets_for: int = 0
    sets_against: int = 0


class TableTennisStatsService:
    def __init__(self, repository: TableTennisRepository) -> None:
        self._repository = repository

    def ingest(self, observation: MatchObservation) -> IngestResult:
        return self._repository.ingest(observation)

    def ingest_many(
        self, observations: list[MatchObservation] | tuple[MatchObservation, ...]
    ) -> tuple[IngestResult, ...]:
        ordered = sorted(
            observations,
            key=lambda item: (item.source_id, item.source_record_id, item.source_revision),
        )
        return tuple(self.ingest(observation) for observation in ordered)

    def snapshot(self) -> StatsSnapshot:
        matches = self._repository.list_current_matches()
        stats: dict[str, _MutableStats] = {}
        for match in matches:
            key = (match.played_at, match.source_id, match.source_record_id, match.source_revision)
            self._apply_player(
                stats,
                player_id=match.player_a.player_id,
                display_name=match.player_a.display_name,
                display_key=key,
                won=match.sets_a > match.sets_b,
                sets_for=match.sets_a,
                sets_against=match.sets_b,
            )
            self._apply_player(
                stats,
                player_id=match.player_b.player_id,
                display_name=match.player_b.display_name,
                display_key=key,
                won=match.sets_b > match.sets_a,
                sets_for=match.sets_b,
                sets_against=match.sets_a,
            )
        players = tuple(self._freeze(player_id, stats[player_id]) for player_id in sorted(stats))
        return StatsSnapshot(current_match_count=len(matches), players=players)

    @staticmethod
    def _apply_player(
        stats: dict[str, _MutableStats],
        *,
        player_id: str,
        display_name: str,
        display_key: tuple[datetime, str, str, int],
        won: bool,
        sets_for: int,
        sets_against: int,
    ) -> None:
        current = stats.get(player_id)
        if current is None:
            current = _MutableStats(display_name=display_name, display_key=display_key)
            stats[player_id] = current
        elif display_key > current.display_key:
            current.display_name = display_name
            current.display_key = display_key
        current.matches += 1
        current.wins += int(won)
        current.losses += int(not won)
        current.sets_for += sets_for
        current.sets_against += sets_against

    @staticmethod
    def _freeze(player_id: str, current: _MutableStats) -> PlayerStats:
        return PlayerStats(
            player_id=player_id,
            display_name=current.display_name,
            matches=current.matches,
            wins=current.wins,
            losses=current.losses,
            sets_for=current.sets_for,
            sets_against=current.sets_against,
            set_difference=current.sets_for - current.sets_against,
            win_rate_millionths=(current.wins * 1_000_000) // current.matches,
        )
