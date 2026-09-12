from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from nika_core.sportsbook_research import (
    Competition,
    Event,
    Market,
    OddsSnapshot,
    Participant,
    Selection,
    Sport,
    SportsbookCatalog,
    SportsbookResearchError,
    SportsbookSource,
)
from nika_core.trading_research.contracts import EventTime


def _catalog_with_reversed_input() -> SportsbookCatalog:
    return SportsbookCatalog(
        sports=(Sport("z-sport", "Z"), Sport("a-sport", "A")),
        competitions=(
            Competition("z-competition", "z-sport", "Z Competition"),
            Competition("a-competition", "a-sport", "A Competition"),
        ),
        participants=(Participant("z", "Z"), Participant("a", "A")),
        events=(
            Event(
                "z-event",
                "z-competition",
                ("z",),
                datetime(2026, 8, 27, 13, tzinfo=UTC),
            ),
            Event(
                "a-event",
                "a-competition",
                ("a",),
                datetime(2026, 8, 27, 12, tzinfo=UTC),
            ),
        ),
        markets=(
            Market("z-market", "z-event", "Z Market"),
            Market("a-market", "a-event", "A Market"),
        ),
        selections=(
            Selection("z-selection", "z-market", "Z Selection"),
            Selection("a-selection", "a-market", "A Selection"),
        ),
        sources=(SportsbookSource("z-source", "Z Source"), SportsbookSource("a-source", "A Source")),
    )


def test_catalog_canonicalizes_collection_order() -> None:
    catalog = _catalog_with_reversed_input()

    assert [item.sport_id for item in catalog.sports] == ["a-sport", "z-sport"]
    assert [item.competition_id for item in catalog.competitions] == [
        "a-competition",
        "z-competition",
    ]
    assert [item.participant_id for item in catalog.participants] == ["a", "z"]
    assert [item.event_id for item in catalog.events] == ["a-event", "z-event"]
    assert [item.market_id for item in catalog.markets] == ["a-market", "z-market"]
    assert [item.selection_id for item in catalog.selections] == [
        "a-selection",
        "z-selection",
    ]
    assert [item.source_id for item in catalog.sources] == ["a-source", "z-source"]


def test_normalized_odds_keys_cannot_silently_collapse() -> None:
    time = EventTime(
        event_at=datetime(2026, 8, 27, 12, tzinfo=UTC),
        source_at=datetime(2026, 8, 27, 12, tzinfo=UTC),
        available_at=datetime(2026, 8, 27, 12, tzinfo=UTC),
    )

    with pytest.raises(SportsbookResearchError, match="duplicate normalized odds key"):
        OddsSnapshot(
            "source",
            "market",
            time,
            {"selection": Decimal("1.90"), " selection ": Decimal("2.10")},
        )


def test_source_uri_rejects_embedded_credentials() -> None:
    with pytest.raises(SportsbookResearchError, match="must not contain credentials"):
        SportsbookSource(
            "source",
            "Source",
            "https://user:password@example.invalid/feed",
        )


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.invalid/feed?access_token=secret",
        "https://example.invalid/feed?api-key=secret",
        "https://example.invalid/feed?session_id=secret",
    ],
)
def test_source_uri_rejects_sensitive_query_parameters(uri: str) -> None:
    with pytest.raises(SportsbookResearchError, match="credential query parameter"):
        SportsbookSource("source", "Source", uri)
