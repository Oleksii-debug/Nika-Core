from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class TableTennisValidationError(ValueError):
    """Raised when a normalized table-tennis observation is invalid."""


def _text(value: object, *, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise TableTennisValidationError(f"{field} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        raise TableTennisValidationError(f"{field} must not be empty")
    if len(normalized) > max_length:
        raise TableTennisValidationError(f"{field} exceeds {max_length} characters")
    if any(character in normalized for character in ("\x00", "\r", "\n", "\t")):
        raise TableTennisValidationError(f"{field} must not contain control separators")
    return normalized


def _optional_text(value: object, *, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    return _text(value, field=field, max_length=max_length)


def _sha256(value: object, *, field: str) -> str:
    digest = _text(value, field=field, max_length=64).lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise TableTennisValidationError(f"{field} must be a 64-character SHA-256 hex digest")
    return digest


def _strict_int(value: object, *, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TableTennisValidationError(f"{field} must be an integer")
    if value < minimum or value > maximum:
        raise TableTennisValidationError(f"{field} must be between {minimum} and {maximum}")
    return value


def _utc_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TableTennisValidationError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise TableTennisValidationError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class PlayerRef:
    player_id: str
    display_name: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "player_id",
            _text(self.player_id, field="player_id", max_length=200),
        )
        object.__setattr__(
            self,
            "display_name",
            _text(self.display_name, field="display_name", max_length=300),
        )


@dataclass(frozen=True, slots=True)
class MatchObservation:
    """A completed match normalized by an upstream research/parser boundary.

    ``source_id`` + ``source_record_id`` identify one logical upstream record.
    Corrections are append-only through monotonically increasing ``source_revision``.
    """

    source_id: str
    source_record_id: str
    source_revision: int
    source_locator: str
    source_evidence_sha256: str
    observed_at: datetime
    played_at: datetime
    event_name: str
    player_a: PlayerRef
    player_b: PlayerRef
    sets_a: int
    sets_b: int
    round_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_id",
            _text(self.source_id, field="source_id", max_length=200),
        )
        object.__setattr__(
            self,
            "source_record_id",
            _text(self.source_record_id, field="source_record_id", max_length=300),
        )
        object.__setattr__(
            self,
            "source_revision",
            _strict_int(
                self.source_revision,
                field="source_revision",
                minimum=1,
                maximum=2_147_483_647,
            ),
        )
        object.__setattr__(
            self,
            "source_locator",
            _text(self.source_locator, field="source_locator", max_length=2_000),
        )
        object.__setattr__(
            self,
            "source_evidence_sha256",
            _sha256(self.source_evidence_sha256, field="source_evidence_sha256"),
        )
        object.__setattr__(
            self,
            "event_name",
            _text(self.event_name, field="event_name", max_length=300),
        )
        object.__setattr__(
            self,
            "round_name",
            _optional_text(self.round_name, field="round_name", max_length=120),
        )
        observed_at = _utc_datetime(self.observed_at, field="observed_at")
        played_at = _utc_datetime(self.played_at, field="played_at")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "played_at", played_at)
        if played_at > observed_at:
            raise TableTennisValidationError("played_at must not be later than observed_at")
        if not isinstance(self.player_a, PlayerRef) or not isinstance(self.player_b, PlayerRef):
            raise TableTennisValidationError("player_a and player_b must be PlayerRef values")
        if self.player_a.player_id == self.player_b.player_id:
            raise TableTennisValidationError("a match must contain two distinct player IDs")
        sets_a = _strict_int(self.sets_a, field="sets_a", minimum=0, maximum=99)
        sets_b = _strict_int(self.sets_b, field="sets_b", minimum=0, maximum=99)
        object.__setattr__(self, "sets_a", sets_a)
        object.__setattr__(self, "sets_b", sets_b)
        if sets_a == sets_b:
            raise TableTennisValidationError("a completed match must have a winner")

    @property
    def winner_id(self) -> str:
        return self.player_a.player_id if self.sets_a > self.sets_b else self.player_b.player_id

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "event_name": self.event_name,
            "observed_at": self.observed_at.isoformat(),
            "played_at": self.played_at.isoformat(),
            "player_a": {
                "display_name": self.player_a.display_name,
                "player_id": self.player_a.player_id,
            },
            "player_b": {
                "display_name": self.player_b.display_name,
                "player_id": self.player_b.player_id,
            },
            "round_name": self.round_name,
            "sets_a": self.sets_a,
            "sets_b": self.sets_b,
            "source_evidence_sha256": self.source_evidence_sha256,
            "source_id": self.source_id,
            "source_locator": self.source_locator,
            "source_record_id": self.source_record_id,
            "source_revision": self.source_revision,
        }

    def payload_sha256(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class IngestDisposition(StrEnum):
    INSERTED = "inserted"
    REVISED = "revised"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class IngestResult:
    source_id: str
    source_record_id: str
    source_revision: int
    payload_sha256: str
    disposition: IngestDisposition


@dataclass(frozen=True, slots=True)
class PlayerStats:
    player_id: str
    display_name: str
    matches: int
    wins: int
    losses: int
    sets_for: int
    sets_against: int
    set_difference: int
    win_rate_millionths: int


@dataclass(frozen=True, slots=True)
class StatsSnapshot:
    current_match_count: int
    players: tuple[PlayerStats, ...]
