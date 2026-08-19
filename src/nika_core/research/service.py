from __future__ import annotations

from pathlib import Path

from nika_core.research.local import extract_local_file
from nika_core.research.models import IngestResult, SourceSpec
from nika_core.research.repository import ResearchRepository


class LocalCorpusService:
    """Deterministic local-source vertical: extract -> normalize/dedup -> persist -> FTS."""

    def __init__(self, repository: ResearchRepository, *, allowed_root: Path | str) -> None:
        self._repository = repository
        self._allowed_root = Path(allowed_root)

    def ingest(self, source: SourceSpec, *, max_bytes: int = 16 * 1024 * 1024) -> IngestResult:
        extracted = extract_local_file(
            source.locator,
            allowed_root=self._allowed_root,
            max_bytes=max_bytes,
        )
        self._repository.upsert_source(source)
        return self._repository.ingest_document(source, extracted)
