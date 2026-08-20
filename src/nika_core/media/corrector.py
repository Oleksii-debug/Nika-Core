from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Protocol

from nika_core.media.contracts import TextRevision
from nika_core.media.repository import MediaRepository
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PrivacyClass,
    ProviderKind,
)

_HORIZONTAL_WS = re.compile(r"[^\S\r\n]+")
_MANY_BLANKS = re.compile(r"\n{3,}")


@dataclass(frozen=True, slots=True)
class CorrectionResult:
    original: str
    normalized: str
    changed: bool


@dataclass(frozen=True, slots=True)
class SemanticCorrectionPolicy:
    """Explicit routing policy for optional semantic suggestions.

    LOCAL is the default so private media never silently crosses a cloud boundary.
    Cloud routing requires both an explicit CLOUD provider kind and allow_cloud=True.
    """

    provider_kind: ProviderKind = ProviderKind.LOCAL
    allow_cloud: bool = False
    timeout_seconds: float = 60.0
    temperature: float = 0.0

    def __post_init__(self) -> None:
        if self.provider_kind is ProviderKind.CLOUD and not self.allow_cloud:
            raise ValueError("cloud semantic correction requires explicit allow_cloud=True")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")


@dataclass(frozen=True, slots=True)
class SemanticCorrectionSuggestion:
    suggestion_id: str
    artifact_id: str
    base_revision_id: str | None
    base_text_sha256: str
    proposed_text: str
    provider_id: str
    provider_kind: ProviderKind
    model: str


class ModelCompletionPort(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


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


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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

    async def request_semantic_suggestion(
        self,
        gateway: ModelCompletionPort,
        *,
        artifact_id: str,
        current_text: str,
        privacy: str = "private",
        policy: SemanticCorrectionPolicy | None = None,
    ) -> SemanticCorrectionSuggestion:
        """Ask the existing ModelGateway for a suggestion without persisting it.

        This method never appends a TextRevision. The returned text remains an untrusted
        suggestion until accept_semantic_suggestion() is called against the same revision
        and exact source-text hash.
        """
        if not artifact_id.strip():
            raise ValueError("artifact_id must not be empty")
        if not current_text.strip():
            raise ValueError("current_text must not be empty")
        try:
            privacy_class = PrivacyClass(privacy)
        except ValueError as exc:
            raise ValueError(f"unsupported privacy class: {privacy}") from exc

        route = policy or SemanticCorrectionPolicy()
        if route.provider_kind is ProviderKind.CLOUD and not route.allow_cloud:
            raise ValueError("cloud semantic correction requires explicit allow_cloud=True")

        existing = self._repository.revisions(artifact_id)
        base_revision = existing[-1] if existing else None
        if base_revision is not None and base_revision.text != current_text:
            raise ValueError("current_text does not match the latest persisted revision")

        request = ModelRequest(
            request_id=f"media-semantic-correction-{uuid.uuid4()}",
            messages=(
                ModelMessage(
                    role="system",
                    content=(
                        "You are a correction suggester. Return only a proposed corrected version "
                        "of the supplied text. Preserve meaning, facts, names, numbers, quotations, "
                        "language, and ordering. Do not add information or commentary."
                    ),
                ),
                ModelMessage(role="user", content=current_text),
            ),
            provider_kind=route.provider_kind,
            privacy=privacy_class,
            timeout_seconds=route.timeout_seconds,
            temperature=route.temperature,
            metadata={"purpose": "media_semantic_correction_suggestion"},
        )
        response = await gateway.complete(request)
        proposed = response.text.strip()
        if not proposed:
            raise ValueError("semantic correction provider returned an empty suggestion")

        return SemanticCorrectionSuggestion(
            suggestion_id=str(uuid.uuid4()),
            artifact_id=artifact_id,
            base_revision_id=base_revision.revision_id if base_revision else None,
            base_text_sha256=_text_sha256(current_text),
            proposed_text=proposed,
            provider_id=response.provider_id,
            provider_kind=response.provider_kind,
            model=response.model,
        )

    def accept_semantic_suggestion(
        self,
        suggestion: SemanticCorrectionSuggestion,
        *,
        current_text: str,
    ) -> TextRevision:
        """Append an explicitly accepted suggestion if its base is still current."""
        if _text_sha256(current_text) != suggestion.base_text_sha256:
            raise ValueError("semantic suggestion source text changed after suggestion creation")

        existing = self._repository.revisions(suggestion.artifact_id)
        latest = existing[-1] if existing else None
        latest_id = latest.revision_id if latest else None
        if latest_id != suggestion.base_revision_id:
            raise ValueError("semantic suggestion is stale because the revision head changed")
        if latest is not None and latest.text != current_text:
            raise ValueError("current_text does not match the latest persisted revision")

        revision = TextRevision(
            revision_id=str(uuid.uuid4()),
            artifact_id=suggestion.artifact_id,
            parent_revision_id=latest_id,
            ordinal=len(existing),
            text=suggestion.proposed_text,
            reason=(
                "accepted_semantic_suggestion:"
                f"{suggestion.provider_kind.value}:{suggestion.provider_id}:{suggestion.model}"
            )[:500],
            accepted=True,
        )
        self._repository.append_revision(revision)
        return revision
