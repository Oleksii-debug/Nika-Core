from __future__ import annotations

from collections.abc import Iterable

from nika_core.media.contracts import AssetKind, MediaAsset, StructuredMediaArtifact
from nika_core.media.handoff import OCRInputRequestV1, CorpusMediaHandoffV1, build_corpus_media_handoff
from nika_core.media.presenter import render_accessible_media_text
from nika_core.media.repository import MediaRepository


class MediaDeliveryCoordinator:
    """Finalize durable DEV05 evidence for neutral downstream consumption."""

    def __init__(self, repository: MediaRepository) -> None:
        self._repository = repository

    def materialize_artifact(self, artifact_id: str) -> StructuredMediaArtifact:
        artifact = self._repository.get_artifact(artifact_id)
        revisions = self._repository.revisions(artifact_id)
        if artifact.revisions and artifact.revisions != revisions:
            raise ValueError("stored artifact revision snapshot conflicts with append-only revision ledger")
        if revisions == artifact.revisions:
            return artifact
        return artifact.model_copy(update={"revisions": revisions})

    def build_handoff(self, artifact_id: str) -> CorpusMediaHandoffV1:
        return build_corpus_media_handoff(self.materialize_artifact(artifact_id))

    def present(self, artifact_id: str, *, errors: Iterable[str] = ()) -> str:
        artifact = self.materialize_artifact(artifact_id)
        handoff = build_corpus_media_handoff(artifact)
        return render_accessible_media_text(artifact, handoff, errors=errors)

    def resolve_ocr_request(self, request: OCRInputRequestV1) -> MediaAsset:
        source = self._repository.get_source(request.source_id)
        version = self._repository.get_version(request.version_id)
        if version.source_id != source.source_id:
            raise ValueError("OCR request source/version identity mismatch")
        assets = {asset.asset_id: asset for asset in self._repository.list_assets(request.version_id)}
        asset = assets.get(request.asset_id)
        if asset is None:
            raise KeyError(f"Unknown media asset for OCR request: {request.asset_id}")
        if asset.kind not in {AssetKind.ORIGINAL, AssetKind.DOCUMENT}:
            raise ValueError("OCR requests may reference only original or document assets")
        return asset
