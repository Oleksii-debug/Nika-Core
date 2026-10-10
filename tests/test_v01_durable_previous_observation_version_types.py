from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.previous_observation import (
    DurablePreviousObservationLoader,
    PreviousObservationError,
    PreviousObservationErrorCode,
    PreviousObservationExpectation,
)
from nika_core.research.profiles import ResearchProfileRepository


@pytest.mark.parametrize(
    ("profile_version", "source_set_version", "corrupt_field"),
    [
        (1.5, 1, "profile_version"),
        (1, 1.5, "source_set_version"),
    ],
)
def test_fractional_sqlite_history_versions_fail_closed(
    tmp_path: Path,
    profile_version: object,
    source_set_version: object,
    corrupt_field: str,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    with store.connection() as conn:
        conn.execute(
            """CREATE TABLE adversarial_history (
                profile_id TEXT NOT NULL,
                profile_version INTEGER NOT NULL,
                source_set_id TEXT NOT NULL,
                source_set_version INTEGER NOT NULL
            )"""
        )
        conn.execute(
            """INSERT INTO adversarial_history (
                profile_id, profile_version, source_set_id, source_set_version
            ) VALUES (?, ?, ?, ?)""",
            ("monitor", profile_version, "sources", source_set_version),
        )
        row = conn.execute(
            """SELECT profile_id, profile_version, source_set_id, source_set_version
            FROM adversarial_history"""
        ).fetchone()
        storage_types = conn.execute(
            """SELECT typeof(profile_version) AS profile_version_type,
                      typeof(source_set_version) AS source_set_version_type
            FROM adversarial_history"""
        ).fetchone()

    assert row is not None
    assert storage_types is not None
    assert storage_types[f"{corrupt_field}_type"] == "real"

    loader = DurablePreviousObservationLoader(
        store=store,
        profiles=ResearchProfileRepository(store),
        network_repository=NetworkResearchRepository(store),
    )
    expected = PreviousObservationExpectation(
        series_id="series",
        workspace_id="ws",
        profile_id="monitor",
        profile_version=1,
        source_set_id="sources",
        source_set_version=1,
    )

    with pytest.raises(PreviousObservationError) as caught:
        loader._validate_history_identity(row, expected)

    assert caught.value.code is PreviousObservationErrorCode.CORRUPT_BASELINE

class _BehavioralIdentity(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        return "accepted"

    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False


class _BehavioralVersion(int):
    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False


@pytest.mark.parametrize(
    "field_name",
    ["series_id", "workspace_id", "profile_id", "source_set_id"],
)
def test_expectation_rejects_behavioral_identity_subclasses(field_name: str) -> None:
    values: dict[str, object] = {
        "series_id": "series",
        "workspace_id": "ws",
        "profile_id": "monitor",
        "profile_version": 1,
        "source_set_id": "sources",
        "source_set_version": 1,
    }
    values[field_name] = _BehavioralIdentity("foreign")

    with pytest.raises(TypeError, match=rf"{field_name} must be an exact str"):
        PreviousObservationExpectation(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("field_name", ["profile_version", "source_set_version"])
def test_expectation_rejects_behavioral_version_subclasses(field_name: str) -> None:
    values: dict[str, object] = {
        "series_id": "series",
        "workspace_id": "ws",
        "profile_id": "monitor",
        "profile_version": 1,
        "source_set_id": "sources",
        "source_set_version": 1,
    }
    values[field_name] = _BehavioralVersion(999)

    with pytest.raises(TypeError, match=rf"{field_name} must be an exact int"):
        PreviousObservationExpectation(**values)  # type: ignore[arg-type]

def test_loader_revalidates_forged_exact_expectation_before_lookup(tmp_path: Path) -> None:
    forged = object.__new__(PreviousObservationExpectation)
    object.__setattr__(forged, "series_id", _BehavioralIdentity("series"))
    object.__setattr__(forged, "workspace_id", "ws")
    object.__setattr__(forged, "profile_id", "monitor")
    object.__setattr__(forged, "profile_version", 1)
    object.__setattr__(forged, "source_set_id", "sources")
    object.__setattr__(forged, "source_set_version", 1)

    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    loader = DurablePreviousObservationLoader(
        store=store,
        profiles=ResearchProfileRepository(store),
        network_repository=NetworkResearchRepository(store),
    )

    with pytest.raises(TypeError, match=r"series_id must be an exact str"):
        loader.load(forged)

