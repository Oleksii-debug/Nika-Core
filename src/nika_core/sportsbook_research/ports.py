from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from .domain import SportsbookCatalog, SportsbookObservation, SportsbookSource


class SportsbookSourcePort(Protocol):
    """Provider-neutral read-only adapter boundary.

    Implementations may fetch public/licensed source data, but this contract intentionally exposes
    no wager, account, funding, deposit, withdrawal, credential-redemption, or bookmaker-action API.
    """

    def source(self) -> SportsbookSource: ...

    def read_catalog(self) -> SportsbookCatalog: ...

    def read_observations(self, *, after: datetime | None = None) -> Iterable[SportsbookObservation]: ...


class SportsbookRepositoryPort(Protocol):
    def initialize(self) -> None: ...

    def register_catalog(self, catalog: SportsbookCatalog) -> int: ...

    def load_catalog(self) -> SportsbookCatalog: ...

    def ingest(self, observation: SportsbookObservation) -> bool: ...

    def ingest_many(self, observations: Iterable[SportsbookObservation]) -> int: ...

    def observations_at(
        self,
        at: datetime,
        *,
        source_id: str | None = None,
        event_id: str | None = None,
    ) -> tuple[SportsbookObservation, ...]: ...
