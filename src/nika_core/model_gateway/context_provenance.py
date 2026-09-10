from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeAlias

from nika_core.memory.contracts import MemoryRecord, MemoryScope
from nika_core.multi_agent.research_results import (
    SourceInspectionAssignment,
    SourceResultBindingError,
    encode_source_result,
)
from nika_core.research.models import (
    FreshnessState,
    ResearchResultItem,
    ResearchResultSet,
    SourceKind,
)

_CONTEXT_PROVENANCE_SCHEMA = "nika.model-context.provenance:v1"
CONTEXT_PROVENANCE_METADATA_KEY = "nika.context_provenance.v1"
_UNSAFE_FRESHNESS = frozenset(
    {
        FreshnessState.STALE,
        FreshnessState.REMOVED,
        FreshnessState.BLOCKED,
        FreshnessState.ERROR,
    }
)


class ContextEvidenceOrigin(StrEnum):
    RESEARCH = "research"
    MEMORY = "memory"


class ContextProvenanceError(ValueError):
    """Raised when model context cannot retain truthful structured provenance."""


@dataclass(frozen=True, slots=True)
class ResearchContextSelection:
    """Select one result item using a trusted source assignment, never text labels."""

    assignment: SourceInspectionAssignment
    result_set: ResearchResultSet
    item_ordinal: int

    def __post_init__(self) -> None:
        if not isinstance(self.assignment, SourceInspectionAssignment):
            raise TypeError("assignment must be SourceInspectionAssignment")
        if not isinstance(self.result_set, ResearchResultSet):
            raise TypeError("result_set must be ResearchResultSet")
        if isinstance(self.item_ordinal, bool) or not isinstance(self.item_ordinal, int):
            raise TypeError("item_ordinal must be an integer")
        if self.item_ordinal < 0:
            raise ValueError("item_ordinal must not be negative")


@dataclass(frozen=True, slots=True)
class MemoryContextSelection:
    """Select one already-retrieved canonical MemoryRecord."""

    record: MemoryRecord

    def __post_init__(self) -> None:
        if not isinstance(self.record, MemoryRecord):
            raise TypeError("record must be MemoryRecord")


ContextSelection: TypeAlias = ResearchContextSelection | MemoryContextSelection


@dataclass(frozen=True, slots=True)
class ContextEvidenceProvenance:
    """Content-free internal binding for one model-visible context unit."""

    position: int
    origin: ContextEvidenceOrigin
    source_id: str
    revision_id: str
    content_sha256: str
    freshness: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.position, bool)
            or not isinstance(self.position, int)
            or self.position < 1
        ):
            raise ValueError("position must be a positive integer")
        if not isinstance(self.origin, ContextEvidenceOrigin):
            raise TypeError("origin must be ContextEvidenceOrigin")
        _require_identifier(self.source_id, "source_id")
        _require_identifier(self.revision_id, "revision_id")
        if not _is_sha256(self.content_sha256):
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        if any(not isinstance(value, str) or not value for value in self.freshness):
            raise ValueError("freshness values must be non-empty text")

    def to_payload(self) -> dict[str, object]:
        return {
            "position": self.position,
            "origin": self.origin.value,
            "source_id": self.source_id,
            "revision_id": self.revision_id,
            "content_sha256": self.content_sha256,
            "freshness": list(self.freshness),
        }


@dataclass(frozen=True, slots=True)
class ModelContextAssembly:
    """Model-visible content plus internal provenance that must travel separately."""

    model_text: str
    provenance: tuple[ContextEvidenceProvenance, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.model_text, str) or not self.model_text:
            raise ValueError("model_text must be non-empty")
        if not self.provenance:
            raise ValueError("at least one provenance unit is required")
        expected_positions = tuple(range(1, len(self.provenance) + 1))
        if tuple(item.position for item in self.provenance) != expected_positions:
            raise ValueError("provenance positions must be contiguous and ordered")

    def to_request_metadata(self) -> dict[str, str]:
        """Return content-free metadata; provider adapters must not serialize it as messages."""
        payload = {
            "schema": _CONTEXT_PROVENANCE_SCHEMA,
            "units": [item.to_payload() for item in self.provenance],
        }
        return {CONTEXT_PROVENANCE_METADATA_KEY: _canonical_json(payload)}


ContextAuthorizer: TypeAlias = Callable[[ContextEvidenceProvenance], bool]


def assemble_model_context(
    selections: Sequence[ContextSelection],
    *,
    authorizer: ContextAuthorizer,
    now: datetime | None = None,
) -> ModelContextAssembly:
    """Bind ordered context data to internal provenance before model injection.

    The authorizer sees only Nika-owned structured provenance, never source text.
    Any stale/revoked/unauthorized unit aborts the entire assembly before text is
    returned, preventing a caller from accidentally sending a mixed unsafe set.
    """

    if not callable(authorizer):
        raise TypeError("authorizer must be callable")
    current_time = _as_utc(now) if now is not None else datetime.now(UTC)

    contents: list[str] = []
    provenance: list[ContextEvidenceProvenance] = []
    for position, selection in enumerate(selections, start=1):
        content, item_provenance = _bind_selection(
            selection,
            position=position,
            now=current_time,
        )
        decision = authorizer(item_provenance)
        if decision is not True:
            if decision is False:
                raise PermissionError(
                    f"context evidence at position {position} is not authorized"
                )
            raise TypeError("authorizer must return bool")
        contents.append(content)
        provenance.append(item_provenance)

    if not contents:
        raise ValueError("at least one context selection is required")

    model_payload = {
        "context_units": [
            {"position": position, "content": content}
            for position, content in enumerate(contents, start=1)
        ]
    }
    return ModelContextAssembly(
        model_text=_canonical_json(model_payload),
        provenance=tuple(provenance),
    )


def merge_context_provenance_metadata(
    metadata: Mapping[str, str],
    assembly: ModelContextAssembly,
) -> dict[str, str]:
    """Attach provenance without allowing an older binding to be silently overwritten."""

    if CONTEXT_PROVENANCE_METADATA_KEY in metadata:
        raise ValueError("context provenance metadata already exists")
    result = dict(metadata)
    result.update(assembly.to_request_metadata())
    return result


def _bind_selection(
    selection: ContextSelection,
    *,
    position: int,
    now: datetime,
) -> tuple[str, ContextEvidenceProvenance]:
    if isinstance(selection, ResearchContextSelection):
        return _bind_research(selection, position=position)
    if isinstance(selection, MemoryContextSelection):
        return _bind_memory(selection, position=position, now=now)
    raise TypeError("unsupported context selection")


def _bind_research(
    selection: ResearchContextSelection,
    *,
    position: int,
) -> tuple[str, ContextEvidenceProvenance]:
    assignment = selection.assignment
    result_set = selection.result_set
    try:
        encoded = encode_source_result(assignment, result_set)
    except (SourceResultBindingError, TypeError, ValueError) as exc:
        raise ContextProvenanceError(
            "research result is not bound to the trusted source assignment"
        ) from exc

    matches = tuple(item for item in result_set.items if item.ordinal == selection.item_ordinal)
    if len(matches) != 1:
        raise ContextProvenanceError("research item ordinal is missing or duplicated")
    item = matches[0]
    _validate_research_freshness(assignment.source.kind, item)

    result_digest = encoded.get("result_digest")
    if not isinstance(result_digest, str) or not _is_sha256(result_digest):
        raise ContextProvenanceError("research result digest is invalid")
    content = _require_content(item.snippet)
    content_sha256 = _sha256_text(content)
    revision_id = "research:" + result_digest
    freshness = tuple(
        sorted(
            {
                evidence.freshness.value
                for evidence in item.evidence
                if evidence.freshness is not None
            }
        )
    )
    return content, ContextEvidenceProvenance(
        position=position,
        origin=ContextEvidenceOrigin.RESEARCH,
        source_id=assignment.source.source_id,
        revision_id=revision_id,
        content_sha256=content_sha256,
        freshness=freshness,
    )


def _validate_research_freshness(
    source_kind: SourceKind,
    item: ResearchResultItem,
) -> None:
    for evidence in item.evidence:
        if evidence.freshness in _UNSAFE_FRESHNESS:
            raise ContextProvenanceError(
                f"research item freshness is unsafe: {evidence.freshness.value}"
            )
        if source_kind is SourceKind.HTTP and evidence.freshness is FreshnessState.UNKNOWN:
            raise ContextProvenanceError("HTTP research item freshness is unknown")


def _bind_memory(
    selection: MemoryContextSelection,
    *,
    position: int,
    now: datetime,
) -> tuple[str, ContextEvidenceProvenance]:
    record = selection.record
    if record.expires_at is not None and _as_utc(record.expires_at) <= now:
        raise ContextProvenanceError("memory record is expired")
    if record.scope is MemoryScope.USER and not record.user_approved:
        raise PermissionError("user memory lacks explicit approval")

    content = _canonical_json(record.value)
    content_sha256 = _sha256_text(content)
    source_material = {
        "scope": record.scope.value,
        "owner_id": record.owner_id,
        "namespace": record.namespace,
        "key": record.key,
    }
    source_id = "memory:" + _sha256_text(_canonical_json(source_material))
    revision_material = {
        **source_material,
        "updated_at": _as_utc(record.updated_at).isoformat(),
        "content_sha256": content_sha256,
    }
    revision_id = "memory:" + _sha256_text(_canonical_json(revision_material))
    return content, ContextEvidenceProvenance(
        position=position,
        origin=ContextEvidenceOrigin.MEMORY,
        source_id=source_id,
        revision_id=revision_id,
        content_sha256=content_sha256,
    )


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ContextProvenanceError("context value must be canonical JSON") from exc


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _require_identifier(value: str, label: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    if not value or value != value.strip():
        raise ValueError(f"{label} must be canonical non-empty text")
    if len(value) > 1024:
        raise ValueError(f"{label} is too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{label} must not contain control characters")


def _require_content(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("context content must be text")
    if not value:
        raise ContextProvenanceError("context content must not be empty")
    return value


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("datetime must be datetime")
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)
