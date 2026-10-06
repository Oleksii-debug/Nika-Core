from __future__ import annotations

from io import BytesIO
from pathlib import Path

import httpx
from openpyxl import Workbook

from nika_core.data.sqlite import SQLiteStore
from nika_core.research import (
    ContentAddressedBlobStore,
    HttpResearchService,
    HttpxResearchFetcher,
    NetworkResearchRepository,
    RefreshDisposition,
    ResearchRepository,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)


def _xlsx_payload() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Дані"
    sheet.append(["Назва", "Сума"])
    sheet.append(["Грант", 100])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def test_http_xlsx_extracts_from_extensionless_blob(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws", "Research"))
    network = NetworkResearchRepository(store)
    payload = _xlsx_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            headers={
                "Content-Type": (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
            },
            content=payload,
        )

    service = HttpResearchService(
        repository=repository,
        network_repository=network,
        blob_store=ContentAddressedBlobStore(tmp_path / "blobs"),
        fetcher=HttpxResearchFetcher(
            resolver=lambda host, port: ("93.184.216.34",),
            transport=httpx.MockTransport(handler),
        ),
        sleeper=lambda _: None,
    )
    service.register_source(
        SourceSpec(
            "xlsx",
            "ws",
            SourceKind.HTTP,
            "https://example.com/data",
        )
    )

    result = service.refresh_source("xlsx")

    assert result.disposition is RefreshDisposition.CHANGED
    assert result.document_id is not None
    assert result.snapshot_id is not None
    hits = repository.search("ws", "Грант")
    assert [hit.document_id for hit in hits] == [result.document_id]
