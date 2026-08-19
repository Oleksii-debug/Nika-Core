from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass

from nika_core.media.contracts import TextRevision
from nika_core.media.repository import MediaRepository

_HORIZONTAL_WS = re.compile(r"[^\S\r\n]+")
_MANY_BLANKS = re.compile(r"\n{3,}")


@dataclass(frozen=True, slots=True)
class CorrectionResult:
    original: str
    normalized: str
    changed: bool


def normalize_text(text: str, *, protected_terms: tuple[str, ...] = ()) -> CorrectionResult:
    """Deterministic NFC/whitespace normalization without semantic rewriting."""
    placeholders: dict[str, str] = {}
    working = text
    for index, term in enumerate(sorted(set(protected_terms), key=len, reverse=True)):
        if not term:
            continue
        token = f"\uE000NIKA_PROTECTED_{index}\uE001"
        placeholders[token] = term
        working = working.replace(term, token)

    working = unicodedata.normalize("NFC", working).replace("\r\n", "\n").replace("\r", "\n")
    lines = [_HORIZONTAL_WS.sub(" ", line).strip() for line in working.split("\n")]
    working = _MANY_BLANKS.sub("\n\n", "\n".join(lines)).strip()
    for token, term in placeholders.items():
        working = working.replace(token, term)
    return CorrectionResult(original=text, normalized=working, changed=working != text)


class RevisionCorrector:
    """Append-only correction facade. Original evidence is never overwritten."""

    def __init__(self, repository: MediaRepository) -> None:
        self._repository = repository

    def append_deterministic_revision(
        self,
        *,
        artifact_id: str,
        original_text: str,
        protected_terms: tuple[str, ...] = (),
        accept: bool = True,
    ) -> TextRevision:
        result = normalize_text(original_text, protected_terms=protected_terms)
        existing = self._repository.revisions(artifact_id)
        parent = existing[-1].revision_id if existing else None
        revision = TextRevision(
            revision_id=str(uuid.uuid4()),
            artifact_id=artifact_id,
            parent_revision_id=parent,
            ordinal=len(existing),
            text=result.normalized,
            reason="deterministic_unicode_whitespace_normalization",
            accepted=accept,
        )
        self._repository.append_revision(revision)
        return revision
