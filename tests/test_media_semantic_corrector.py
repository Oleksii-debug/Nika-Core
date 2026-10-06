from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.media.corrector import RevisionCorrector, SemanticCorrectionPolicy
from nika_core.media.repository import MediaRepository
from nika_core.media.schema import initialize_media_schema
from nika_core.model_gateway.contracts import (
    ModelRequest,
    ModelResponse,
    PrivacyClass,
    ProviderKind,
)


class FakeGateway:
    def __init__(self, *, text: str = "Corrected text", kind: ProviderKind = ProviderKind.LOCAL) -> None:
        self.text = text
        self.kind = kind
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            request_id=request.request_id,
            text=self.text,
            provider_id="fixture-provider",
            provider_kind=self.kind,
            model="fixture-model",
        )


def _corrector(tmp_path: Path) -> tuple[RevisionCorrector, MediaRepository]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    initialize_media_schema(store)
    repository = MediaRepository(store)
    return RevisionCorrector(repository), repository


def test_semantic_suggestion_defaults_to_local_private_and_does_not_persist(tmp_path: Path) -> None:
    corrector, repository = _corrector(tmp_path)
    gateway = FakeGateway(text="Виправлений текст")

    suggestion = asyncio.run(
        corrector.request_semantic_suggestion(
            gateway,
            artifact_id="artifact-1",
            current_text="Початковий текст",
        )
    )

    assert suggestion.proposed_text == "Виправлений текст"
    assert suggestion.base_revision_id is None
    assert repository.revisions("artifact-1") == ()
    request = gateway.requests[0]
    assert request.provider_kind is ProviderKind.LOCAL
    assert request.privacy is PrivacyClass.PRIVATE
    assert request.metadata == {"purpose": "media_semantic_correction_suggestion"}
    assert request.messages[-1].content == "Початковий текст"


def test_cloud_semantic_correction_requires_explicit_opt_in() -> None:
    with pytest.raises(ValueError, match="explicit allow_cloud"):
        SemanticCorrectionPolicy(provider_kind=ProviderKind.CLOUD)

    policy = SemanticCorrectionPolicy(provider_kind=ProviderKind.CLOUD, allow_cloud=True)
    assert policy.provider_kind is ProviderKind.CLOUD
    assert policy.allow_cloud is True


def test_semantic_suggestion_only_becomes_revision_after_explicit_accept(tmp_path: Path) -> None:
    corrector, repository = _corrector(tmp_path)
    first = corrector.append_deterministic_revision(
        artifact_id="artifact-2",
        original_text="A   B",
    )
    gateway = FakeGateway(text="A B corrected")

    suggestion = asyncio.run(
        corrector.request_semantic_suggestion(
            gateway,
            artifact_id="artifact-2",
            current_text=first.text,
            privacy="sensitive",
        )
    )
    assert len(repository.revisions("artifact-2")) == 1
    assert gateway.requests[0].privacy is PrivacyClass.SENSITIVE

    accepted = corrector.accept_semantic_suggestion(suggestion, current_text=first.text)
    revisions = repository.revisions("artifact-2")
    assert len(revisions) == 2
    assert accepted == revisions[-1]
    assert accepted.parent_revision_id == first.revision_id
    assert accepted.accepted is True
    assert accepted.text == "A B corrected"
    assert accepted.reason.startswith("accepted_semantic_suggestion:local:")


def test_semantic_suggestion_rejects_stale_revision_head(tmp_path: Path) -> None:
    corrector, repository = _corrector(tmp_path)
    first = corrector.append_deterministic_revision(
        artifact_id="artifact-3",
        original_text="one   two",
    )
    suggestion = asyncio.run(
        corrector.request_semantic_suggestion(
            FakeGateway(text="one two suggested"),
            artifact_id="artifact-3",
            current_text=first.text,
        )
    )

    newer = corrector.append_deterministic_revision(
        artifact_id="artifact-3",
        original_text=first.text + "   three",
    )
    with pytest.raises(ValueError, match="revision head changed"):
        corrector.accept_semantic_suggestion(suggestion, current_text=first.text)
    assert repository.revisions("artifact-3")[-1] == newer


def test_semantic_suggestion_rejects_changed_source_text(tmp_path: Path) -> None:
    corrector, repository = _corrector(tmp_path)
    suggestion = asyncio.run(
        corrector.request_semantic_suggestion(
            FakeGateway(),
            artifact_id="artifact-4",
            current_text="source text",
        )
    )

    with pytest.raises(ValueError, match="source text changed"):
        corrector.accept_semantic_suggestion(suggestion, current_text="different text")
    assert repository.revisions("artifact-4") == ()


def test_semantic_suggestion_rejects_empty_provider_output(tmp_path: Path) -> None:
    corrector, _repository = _corrector(tmp_path)
    with pytest.raises(ValueError, match="empty suggestion"):
        asyncio.run(
            corrector.request_semantic_suggestion(
                FakeGateway(text="   "),
                artifact_id="artifact-5",
                current_text="source text",
            )
        )
