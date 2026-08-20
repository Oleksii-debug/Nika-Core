from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research import NetworkResearchRepository, ResearchWorkspace
from nika_core.research.query_results import ScopedResearchResultWriter
from nika_core.research.repository import ResearchRepository
from nika_core.research.models import SearchHit


def test_result_header_rolls_back_when_item_insert_fails(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ResearchRepository(store).upsert_workspace(
        ResearchWorkspace(workspace_id="ws", name="Research")
    )
    writer = ScopedResearchResultWriter(
        store=store,
        network_repository=NetworkResearchRepository(store),
    )

    with pytest.raises(sqlite3.IntegrityError):
        writer.save(
            workspace_id="ws",
            query="grant",
            hits=[
                SearchHit(
                    document_id="missing-document",
                    title="Missing",
                    snippet="grant",
                    rank=0.0,
                )
            ],
            why_matched="test",
        )

    with store.connection() as conn:
        headers = conn.execute("SELECT COUNT(*) AS count FROM research_result_sets").fetchone()
        items = conn.execute("SELECT COUNT(*) AS count FROM research_result_items").fetchone()
    assert int(headers["count"]) == 0
    assert int(items["count"]) == 0
