from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from nika_core.interaction.domain import StaleSnapshotError
from nika_core.interaction.site_diagnostics import PlaywrightSiteDiagnosticsProbe


@dataclass
class _Record:
    page: object
    document_generation: int = 7


class _Page:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.after_evaluate: Callable[[], None] | None = None

    def evaluate(self, _script: str) -> object:
        if self.after_evaluate is not None:
            self.after_evaluate()
        return self.payload


class _Registry:
    def __init__(self, page: _Page) -> None:
        self.record = _Record(page)
        self.registered = True

    def get(self, page_id: str) -> _Record:
        assert page_id == "page-1"
        if not self.registered:
            raise StaleSnapshotError("browser page is closed or no longer registered")
        return self.record


class _Session:
    def __init__(self, registry: _Registry) -> None:
        self.registry = registry


def _payload() -> dict[str, object]:
    return {
        "url": "https://example.test/",
        "title": "Example",
        "ready_state": "complete",
        "controls": [],
        "headings": [],
        "forms": [],
        "frames": [],
        "shadow_root_count": 0,
    }


def test_capture_rejects_document_generation_advance_during_evaluate() -> None:
    page = _Page(_payload())
    registry = _Registry(page)
    page.after_evaluate = lambda: setattr(registry.record, "document_generation", 8)

    with pytest.raises(StaleSnapshotError, match="document generation changed"):
        PlaywrightSiteDiagnosticsProbe(
            _Session(registry), "page-1"  # type: ignore[arg-type]
        ).capture()


def test_capture_rejects_page_invalidation_after_evaluate() -> None:
    page = _Page(_payload())
    registry = _Registry(page)
    page.after_evaluate = lambda: setattr(registry, "registered", False)

    with pytest.raises(StaleSnapshotError, match="closed or no longer registered"):
        PlaywrightSiteDiagnosticsProbe(
            _Session(registry), "page-1"  # type: ignore[arg-type]
        ).capture()


def test_capture_returns_pre_observation_generation_when_identity_is_stable() -> None:
    page = _Page(_payload())
    registry = _Registry(page)

    model = PlaywrightSiteDiagnosticsProbe(
        _Session(registry), "page-1"  # type: ignore[arg-type]
    ).capture()

    assert model.document_generation == 7
