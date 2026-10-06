from __future__ import annotations

from collections.abc import Iterable

from nika_core.media.contracts import StructuredMediaArtifact
from nika_core.media.handoff import CorpusMediaHandoffV1, MediaTextSourceKind


def _format_ms(value: int) -> str:
    total_seconds, milliseconds = divmod(value, 1000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"
    return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def render_accessible_media_text(
    artifact: StructuredMediaArtifact,
    handoff: CorpusMediaHandoffV1,
    *,
    errors: Iterable[str] = (),
) -> str:
    """Render copyable screen-reader-friendly text without visual-only status semantics."""

    lines = [
        "Nika Core media artifact",
        f"Artifact: {handoff.artifact_id}",
        f"Source: {handoff.source_id}",
        f"Version: {handoff.version_id}",
        f"Privacy: {handoff.privacy}",
        "",
        "Text blocks",
    ]

    if not handoff.blocks:
        lines.append("No timed or paged text blocks are available.")
    for block in handoff.blocks:
        confidence = ""
        if block.confidence is not None:
            confidence = f" confidence {block.confidence:.3f}"
        if block.source_kind == MediaTextSourceKind.TRANSCRIPT:
            assert block.start_ms is not None and block.end_ms is not None
            locus = f"{_format_ms(block.start_ms)}–{_format_ms(block.end_ms)}"
        else:
            assert block.page_number is not None
            locus = f"Page {block.page_number}"
        lines.append(f"{locus}{confidence}: {block.text}")

    lines.extend(["", "Accepted revision"])
    if handoff.accepted_revision is None:
        lines.append("No accepted correction revision.")
    else:
        revision = handoff.accepted_revision
        lines.append(
            f"Revision {revision.ordinal} ({revision.revision_id}), reason: {revision.reason}"
        )
        lines.append(revision.text)

    lines.extend(["", "Engine and model evidence"])
    if not artifact.engines:
        lines.append("No engine descriptors recorded.")
    for engine in artifact.engines:
        lines.append(
            f"Engine {engine.name} {engine.version}; license {engine.license_id}; id {engine.engine_id}."
        )
    if not artifact.models:
        lines.append("No model descriptors recorded.")
    for model in artifact.models:
        checksum = model.sha256 or "not recorded"
        lines.append(
            f"Model {model.model_id} version {model.version}; license {model.license_reference}; "
            f"checksum {checksum}."
        )

    lines.extend(["", "Provenance"])
    if not handoff.provenance:
        lines.append("No provenance events recorded.")
    for event in handoff.provenance:
        lines.append(f"{event.sequence}: {event.event_type}")

    normalized_errors = tuple(str(error).strip() for error in errors if str(error).strip())
    lines.extend(["", "Errors"])
    if not normalized_errors:
        lines.append("No errors recorded.")
    else:
        lines.extend(f"Error: {error}" for error in normalized_errors)

    return "\n".join(lines).rstrip() + "\n"
