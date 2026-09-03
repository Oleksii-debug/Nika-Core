from __future__ import annotations

from dataclasses import dataclass

import pytest

from nika_core.interaction.domain import StaleSnapshotError
from nika_core.interaction.site_diagnostics import PlaywrightSiteDiagnosticsProbe


_PAYLOAD = {
    "url": "https://example.test/old-document",
    "title": "Old document",
    "ready_state": "complete",
    "controls": [],
    "headings": [{"level": 1, "text": "Old heading"}],
    "forms": [],
    "frames": [],
    "shadow_root_count": 0,
    "shadow_scan_truncated": False,
}


@dataclass
class _Record:
    page: object | None = None
    document_generation: int = 7


class _Page:
    def __init__(self, record: _Record, *, advance_generation: bool) -> None:
        self._record = record
        self._advance_generation = advance_generation

    def evaluate(self, script: str) -> object:
        assert "ready_state" in script
        payload = dict(_PAYLOAD)
        if self._advance_generation:
            # Deterministically model a main-frame navigation completing after the old
            # document was read but before capture() labels the returned evidence.
            self._record.document_generation += 1
        return payload


class _Registry:
    def __init__(self, record: _Record) -> None:
        self._record = record

    def get(self, page_id: str) -> _Record:
        assert page_id == "page-1"
        return self._record


class _Session:
    def __init__(self, record: _Record) -> None:
        self.registry = _Registry(record)


def _probe(*, advance_generation: bool) -> PlaywrightSiteDiagnosticsProbe:
    record = _Record()
    record.page = _Page(record, advance_generation=advance_generation)
    return PlaywrightSiteDiagnosticsProbe(_Session(record), "page-1")  # type: ignore[arg-type]


def test_site_model_capture_preserves_stable_document_generation() -> None:
    model = _probe(advance_generation=False).capture()

    assert model.document_generation == 7
    assert model.url == "https://example.test/old-document"
    assert model.title == "Old document"
    assert model.headings[0].text == "Old heading"


def test_site_model_fails_closed_if_document_generation_changes_during_capture() -> None:
    with pytest.raises(StaleSnapshotError):
        _probe(advance_generation=True).capture()
