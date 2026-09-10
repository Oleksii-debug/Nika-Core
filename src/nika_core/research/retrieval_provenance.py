from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from enum import StrEnum

from nika_core.research.knowledge import (
    ConnectionProvider,
    CorpusCorruptionError,
    KnowledgeProvenance,
)

_EVIDENCE_SCHEMA = "nika.retrieval-provenance"
_EVIDENCE_SCHEMA_VERSION = 1
_INTEGER_FIELDS = frozenset(
    {
        "version",
        "chunk_max_chars",
        "chunk_overlap_chars",
        "chunk_ordinal",
        "start_char",
        "end_char",
    }
)
_OPTIONAL_STRING_FIELDS = frozenset({"source_id", "raw_sha256"})


class RetrievalEvidenceStatus(StrEnum):
    CURRENT = "current"
    SUPERSEDED = "superseded"


class StaleRetrievalEvidenceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RetrievalEvidence:
    workspace_id: str
    artifact_key: str
    version: int
    source_id: str | None
    source_locator_sha256: str
    normalized_sha256: str
    raw_sha256: str | None
    parser_name: str
    parser_version: str
    normalization_version: str
    chunker_version: str
    chunk_max_chars: int
    chunk_overlap_chars: int
    approved_by: str
    chunk_id: str
    chunk_ordinal: int
    start_char: int
    end_char: int
    chunk_sha256: str

    @classmethod
    def from_provenance(cls, provenance: KnowledgeProvenance) -> RetrievalEvidence:
        return cls(
            workspace_id=provenance.workspace_id,
            artifact_key=provenance.artifact_key,
            version=provenance.version,
            source_id=provenance.source_id,
            source_locator_sha256=_sha256(provenance.source_locator),
            normalized_sha256=provenance.normalized_sha256,
            raw_sha256=provenance.raw_sha256,
            parser_name=provenance.parser_name,
            parser_version=provenance.parser_version,
            normalization_version=provenance.normalization_version,
            chunker_version=provenance.chunker_version,
            chunk_max_chars=provenance.chunk_max_chars,
            chunk_overlap_chars=provenance.chunk_overlap_chars,
            approved_by=provenance.approved_by,
            chunk_id=provenance.chunk_id,
            chunk_ordinal=provenance.chunk_ordinal,
            start_char=provenance.start_char,
            end_char=provenance.end_char,
            chunk_sha256=provenance.chunk_sha256,
        )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def serialize_retrieval_provenance(provenance: KnowledgeProvenance) -> str:
    """Serialize audit evidence without duplicating the raw source locator."""
    payload = {
        "schema": _EVIDENCE_SCHEMA,
        "schema_version": _EVIDENCE_SCHEMA_VERSION,
        "evidence": asdict(RetrievalEvidence.from_provenance(provenance)),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def deserialize_retrieval_provenance(payload: str) -> RetrievalEvidence:
    """Decode exact evidence fields; source identity remains absent when it was absent."""
    try:
        decoded = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("retrieval provenance is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("retrieval provenance envelope must be an object")
    if set(decoded) != {"schema", "schema_version", "evidence"}:
        raise ValueError("retrieval provenance envelope fields are invalid")
    if decoded["schema"] != _EVIDENCE_SCHEMA:
        raise ValueError("retrieval provenance schema is unsupported")
    if type(decoded["schema_version"]) is not int:
        raise ValueError("retrieval provenance schema version must be an integer")
    if decoded["schema_version"] != _EVIDENCE_SCHEMA_VERSION:
        raise ValueError("retrieval provenance schema version is unsupported")

    values = decoded["evidence"]
    if not isinstance(values, dict):
        raise ValueError("retrieval provenance evidence must be an object")
    expected_fields = {field.name for field in fields(RetrievalEvidence)}
    if set(values) != expected_fields:
        raise ValueError("retrieval provenance fields are invalid")
    for name, value in values.items():
        if name in _INTEGER_FIELDS:
            if type(value) is not int:
                raise ValueError(f"retrieval provenance {name} must be an integer")
        elif name in _OPTIONAL_STRING_FIELDS:
            if value is not None and not isinstance(value, str):
                raise ValueError(f"retrieval provenance {name} must be a string or null")
        elif not isinstance(value, str):
            raise ValueError(f"retrieval provenance {name} must be a string")
    if len(values["source_locator_sha256"]) != 64:
        raise ValueError("retrieval provenance source locator digest is invalid")
    return RetrievalEvidence(**values)


def verify_retrieval_provenance(
    store: ConnectionProvider,
    evidence: RetrievalEvidence,
    *,
    require_current: bool = True,
) -> RetrievalEvidenceStatus:
    """Revalidate serialized evidence against durable corpus and source authority."""
    with store.connection() as conn:
        row = conn.execute(
            """SELECT
                a.current_version,
                v.source_id, v.source_locator, v.normalized_sha256, v.raw_sha256,
                v.parser_name, v.parser_version, v.normalization_version, v.chunker_version,
                v.chunk_max_chars, v.chunk_overlap_chars, v.approved_by, v.normalized_text,
                c.chunk_id, c.ordinal, c.start_char, c.end_char, c.chunk_sha256, c.text
            FROM knowledge_artifacts AS a
            JOIN knowledge_versions AS v
              ON v.workspace_id=a.workspace_id
             AND v.artifact_key=a.artifact_key
             AND v.version=?
            JOIN knowledge_chunks AS c
              ON c.workspace_id=v.workspace_id
             AND c.artifact_key=v.artifact_key
             AND c.version=v.version
             AND c.chunk_id=?
            WHERE a.workspace_id=? AND a.artifact_key=?""",
            (
                evidence.version,
                evidence.chunk_id,
                evidence.workspace_id,
                evidence.artifact_key,
            ),
        ).fetchone()
        if row is None:
            raise CorpusCorruptionError(
                "retrieval provenance has no authoritative corpus row"
            )

        authoritative = {
            "source_id": row["source_id"],
            "source_locator_sha256": _sha256(row["source_locator"]),
            "normalized_sha256": row["normalized_sha256"],
            "raw_sha256": row["raw_sha256"],
            "parser_name": row["parser_name"],
            "parser_version": row["parser_version"],
            "normalization_version": row["normalization_version"],
            "chunker_version": row["chunker_version"],
            "chunk_max_chars": int(row["chunk_max_chars"]),
            "chunk_overlap_chars": int(row["chunk_overlap_chars"]),
            "approved_by": row["approved_by"],
            "chunk_id": row["chunk_id"],
            "chunk_ordinal": int(row["ordinal"]),
            "start_char": int(row["start_char"]),
            "end_char": int(row["end_char"]),
            "chunk_sha256": row["chunk_sha256"],
        }
        for name, value in authoritative.items():
            if getattr(evidence, name) != value:
                raise CorpusCorruptionError(
                    f"retrieval provenance {name} does not match durable authority"
                )

        normalized_text = row["normalized_text"]
        if _sha256(normalized_text) != row["normalized_sha256"]:
            raise CorpusCorruptionError("retrieval provenance version hash mismatch")
        chunk_text = row["text"]
        if _sha256(chunk_text) != row["chunk_sha256"]:
            raise CorpusCorruptionError("retrieval provenance chunk hash mismatch")
        if normalized_text[evidence.start_char : evidence.end_char] != chunk_text:
            raise CorpusCorruptionError("retrieval provenance position does not match source text")

        if evidence.source_id is not None:
            local_rows = conn.execute(
                "SELECT workspace_id, locator FROM research_sources WHERE source_id=?",
                (evidence.source_id,),
            ).fetchall()
            http_rows = conn.execute(
                """SELECT workspace_id, url AS locator FROM research_http_sources
                WHERE source_id=?""",
                (evidence.source_id,),
            ).fetchall()
            source_rows = tuple(local_rows) + tuple(http_rows)
            if len(source_rows) != 1:
                raise CorpusCorruptionError(
                    "retrieval source identity is missing or ambiguous"
                )
            source = source_rows[0]
            if (
                source["workspace_id"] != evidence.workspace_id
                or source["locator"] != row["source_locator"]
            ):
                raise CorpusCorruptionError(
                    "retrieval source identity no longer matches durable authority"
                )

        status = (
            RetrievalEvidenceStatus.CURRENT
            if int(row["current_version"]) == evidence.version
            else RetrievalEvidenceStatus.SUPERSEDED
        )
    if require_current and status is RetrievalEvidenceStatus.SUPERSEDED:
        raise StaleRetrievalEvidenceError(
            "retrieval evidence was superseded by a newer corpus version"
        )
    return status


def restore_retrieval_provenance(
    store: ConnectionProvider,
    payload: str,
    *,
    require_current: bool = True,
) -> tuple[RetrievalEvidence, RetrievalEvidenceStatus]:
    """Decode and authoritatively revalidate durable retrieval evidence."""
    evidence = deserialize_retrieval_provenance(payload)
    status = verify_retrieval_provenance(
        store,
        evidence,
        require_current=require_current,
    )
    return evidence, status
