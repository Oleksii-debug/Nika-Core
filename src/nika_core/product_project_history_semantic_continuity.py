from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from nika_core.product_project import ProductProjectError
from nika_core.product_project_history_generations import (
    ProductProjectHistoryGeneration,
    ProductProjectHistoryGenerationService,
)

_SCHEMA = "nika-product-project-history-semantic-anchor-v1"
_ORDERED_SECTIONS = ("specs", "audit_events")
_IMMUTABLE_IDENTITIES: dict[str, tuple[str, ...]] = {
    "research_handoffs": ("package_id",),
    "decisions": ("decision_id", "decision_version"),
    "creation_idempotency": ("operation_key",),
    "mutation_idempotency": ("operation_key",),
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _rolling_digest(rows: Sequence[Any]) -> str:
    current = "0" * 64
    for row in rows:
        current = hashlib.sha256((current + _canonical(row)).encode("utf-8")).hexdigest()
    return current


@dataclass(frozen=True, slots=True)
class OrderedSectionCommitment:
    section: str
    record_count: int
    prefix_digest_sha256: str


@dataclass(frozen=True, slots=True)
class ImmutableRecordCommitment:
    identity_digest_sha256: str
    row_digest_sha256: str


@dataclass(frozen=True, slots=True)
class ImmutableSectionCommitment:
    section: str
    record_count: int
    records: tuple[ImmutableRecordCommitment, ...]
    commitment_digest_sha256: str


@dataclass(frozen=True, slots=True)
class ProductProjectHistorySemanticAnchor:
    project_id: str
    generation: int
    spec_version: int
    row_version: int
    archive_digest_sha256: str
    generation_manifest_digest_sha256: str
    ordered_sections: tuple[OrderedSectionCommitment, ...]
    immutable_sections: tuple[ImmutableSectionCommitment, ...]
    descriptor_digest_sha256: str
    descriptor_bytes: bytes


@dataclass(frozen=True, slots=True)
class ProductProjectHistorySemanticVerification:
    trusted_anchor: ProductProjectHistorySemanticAnchor
    verified_generations: tuple[int, ...]
    project_id: str
    head_generation: int
    head_spec_version: int
    head_row_version: int
    head_archive_digest_sha256: str
    preserved_ordered_records: int
    preserved_immutable_records: int


class ProductProjectHistorySemanticContinuityService:
    """Verify semantic append-only continuity from a compact trusted PF12 anchor.

    Ordered history sections use constant-size rolling-prefix commitments. Sections
    sorted by stable identity use hashed identity+row commitments, so the descriptor
    does not need the original row payloads. The descriptor is tamper-evident only;
    authentication remains the caller's policy/evidence responsibility.
    """

    def __init__(self, store: Any) -> None:
        self.store = store
        self.generations = ProductProjectHistoryGenerationService(store)

    def export(
        self,
        generation: ProductProjectHistoryGeneration,
    ) -> ProductProjectHistorySemanticAnchor:
        summary = self.generations.verify(generation)
        history = self._history_for_generation(generation)
        ordered = tuple(
            OrderedSectionCommitment(
                section=section,
                record_count=len(self._rows(history, section)),
                prefix_digest_sha256=_rolling_digest(self._rows(history, section)),
            )
            for section in _ORDERED_SECTIONS
        )
        immutable = tuple(
            self._immutable_commitment(section, self._rows(history, section), fields)
            for section, fields in _IMMUTABLE_IDENTITIES.items()
        )
        payload = {
            "schema": _SCHEMA,
            "project_id": summary.project_id,
            "generation": summary.generation,
            "spec_version": summary.spec_version,
            "row_version": summary.row_version,
            "archive_digest_sha256": summary.archive_digest_sha256,
            "generation_manifest_digest_sha256": (
                summary.generation_manifest_digest_sha256
            ),
            "ordered_sections": [
                {
                    "section": item.section,
                    "record_count": item.record_count,
                    "prefix_digest_sha256": item.prefix_digest_sha256,
                }
                for item in ordered
            ],
            "immutable_sections": [
                {
                    "section": item.section,
                    "record_count": item.record_count,
                    "records": [
                        {
                            "identity_digest_sha256": record.identity_digest_sha256,
                            "row_digest_sha256": record.row_digest_sha256,
                        }
                        for record in item.records
                    ],
                    "commitment_digest_sha256": item.commitment_digest_sha256,
                }
                for item in immutable
            ],
        }
        descriptor_digest = _digest(payload)
        descriptor_bytes = _canonical(
            {"digest_sha256": descriptor_digest, "payload": payload}
        ).encode("utf-8")
        return ProductProjectHistorySemanticAnchor(
            project_id=summary.project_id,
            generation=summary.generation,
            spec_version=summary.spec_version,
            row_version=summary.row_version,
            archive_digest_sha256=summary.archive_digest_sha256,
            generation_manifest_digest_sha256=(
                summary.generation_manifest_digest_sha256
            ),
            ordered_sections=ordered,
            immutable_sections=immutable,
            descriptor_digest_sha256=descriptor_digest,
            descriptor_bytes=descriptor_bytes,
        )

    def verify_descriptor(
        self,
        descriptor_bytes: bytes,
    ) -> ProductProjectHistorySemanticAnchor:
        payload, descriptor_digest = self._decode_descriptor(descriptor_bytes)
        ordered = tuple(
            self._ordered_from_payload(item) for item in payload["ordered_sections"]
        )
        immutable = tuple(
            self._immutable_from_payload(item)
            for item in payload["immutable_sections"]
        )
        if {item.section for item in ordered} != set(_ORDERED_SECTIONS):
            raise ProductProjectError("semantic anchor ordered section set is invalid")
        if {item.section for item in immutable} != set(_IMMUTABLE_IDENTITIES):
            raise ProductProjectError("semantic anchor immutable section set is invalid")
        return ProductProjectHistorySemanticAnchor(
            project_id=payload["project_id"],
            generation=payload["generation"],
            spec_version=payload["spec_version"],
            row_version=payload["row_version"],
            archive_digest_sha256=payload["archive_digest_sha256"],
            generation_manifest_digest_sha256=payload[
                "generation_manifest_digest_sha256"
            ],
            ordered_sections=ordered,
            immutable_sections=immutable,
            descriptor_digest_sha256=descriptor_digest,
            descriptor_bytes=descriptor_bytes,
        )

    def verify_window(
        self,
        trusted_anchor_bytes: bytes,
        generations: Sequence[ProductProjectHistoryGeneration],
        *,
        require_live_head: bool = False,
    ) -> ProductProjectHistorySemanticVerification:
        anchor = self.verify_descriptor(trusted_anchor_bytes)
        if not generations:
            raise ProductProjectError("semantic history window has no descendants")
        summaries = self.generations.verify_chain(generations, require_genesis=False)
        first = summaries[0]
        if first.project_id != anchor.project_id:
            raise ProductProjectError("semantic history window project mismatch")
        if first.generation != anchor.generation + 1:
            raise ProductProjectError("semantic history window does not continue anchor")
        if (
            first.previous_generation_manifest_digest_sha256
            != anchor.generation_manifest_digest_sha256
        ):
            raise ProductProjectError("semantic history predecessor digest mismatch")
        if first.previous_archive_digest_sha256 != anchor.archive_digest_sha256:
            raise ProductProjectError("semantic history archive ancestry mismatch")
        if first.spec_version < anchor.spec_version:
            raise ProductProjectError("semantic history spec version rolled back")
        if first.row_version < anchor.row_version:
            raise ProductProjectError("semantic history row version rolled back")
        if first.archive_digest_sha256 == anchor.archive_digest_sha256:
            raise ProductProjectError("semantic history replayed trusted anchor")

        history = self._history_for_generation(generations[0])
        preserved_ordered = self._verify_ordered(anchor, history)
        preserved_immutable = self._verify_immutable(anchor, history)
        for summary in summaries:
            if summary.project_id != anchor.project_id:
                raise ProductProjectError("semantic history window project mismatch")

        if require_live_head:
            head = generations[-1]
            self.generations.segments.verify_against_live(
                head.segment_manifest_bytes,
                head.segment_bytes,
            )

        head_summary = summaries[-1]
        return ProductProjectHistorySemanticVerification(
            trusted_anchor=anchor,
            verified_generations=tuple(item.generation for item in summaries),
            project_id=anchor.project_id,
            head_generation=head_summary.generation,
            head_spec_version=head_summary.spec_version,
            head_row_version=head_summary.row_version,
            head_archive_digest_sha256=head_summary.archive_digest_sha256,
            preserved_ordered_records=preserved_ordered,
            preserved_immutable_records=preserved_immutable,
        )

    def advance(
        self,
        trusted_anchor_bytes: bytes,
        generations: Sequence[ProductProjectHistoryGeneration],
        *,
        require_live_head: bool = True,
    ) -> ProductProjectHistorySemanticAnchor:
        self.verify_window(
            trusted_anchor_bytes,
            generations,
            require_live_head=require_live_head,
        )
        return self.export(generations[-1])

    def _history_for_generation(
        self,
        generation: ProductProjectHistoryGeneration,
    ) -> dict[str, Any]:
        raw = self.generations.segments.reassemble(
            generation.segment_manifest_bytes,
            generation.segment_bytes,
        )
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProductProjectError("invalid semantic history archive encoding") from exc
        if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict):
            raise ProductProjectError("invalid semantic history archive envelope")
        history = envelope["payload"].get("history")
        if not isinstance(history, dict):
            raise ProductProjectError("semantic history archive has no history")
        return history

    @classmethod
    def _verify_ordered(
        cls,
        anchor: ProductProjectHistorySemanticAnchor,
        history: dict[str, Any],
    ) -> int:
        total = 0
        commitments = {item.section: item for item in anchor.ordered_sections}
        for section in _ORDERED_SECTIONS:
            commitment = commitments[section]
            rows = cls._rows(history, section)
            if len(rows) < commitment.record_count:
                raise ProductProjectError(f"semantic history truncated prior {section}")
            if _rolling_digest(rows[: commitment.record_count]) != (
                commitment.prefix_digest_sha256
            ):
                raise ProductProjectError(f"semantic history rewrote prior {section}")
            total += commitment.record_count
        return total

    @classmethod
    def _verify_immutable(
        cls,
        anchor: ProductProjectHistorySemanticAnchor,
        history: dict[str, Any],
    ) -> int:
        total = 0
        commitments = {item.section: item for item in anchor.immutable_sections}
        for section, fields in _IMMUTABLE_IDENTITIES.items():
            commitment = commitments[section]
            current = cls._identity_row_digests(section, cls._rows(history, section), fields)
            if len(current) < commitment.record_count:
                raise ProductProjectError(f"semantic history truncated prior {section}")
            for record in commitment.records:
                observed = current.get(record.identity_digest_sha256)
                if observed is None:
                    raise ProductProjectError(
                        f"semantic history removed prior {section} record"
                    )
                if observed != record.row_digest_sha256:
                    raise ProductProjectError(
                        f"semantic history rewrote prior {section} record"
                    )
            total += commitment.record_count
        return total

    @classmethod
    def _immutable_commitment(
        cls,
        section: str,
        rows: list[Any],
        fields: tuple[str, ...],
    ) -> ImmutableSectionCommitment:
        digests = cls._identity_row_digests(section, rows, fields)
        records = tuple(
            ImmutableRecordCommitment(identity, row_digest)
            for identity, row_digest in sorted(digests.items())
        )
        commitment_digest = _digest(
            [
                {
                    "identity_digest_sha256": item.identity_digest_sha256,
                    "row_digest_sha256": item.row_digest_sha256,
                }
                for item in records
            ]
        )
        return ImmutableSectionCommitment(
            section=section,
            record_count=len(records),
            records=records,
            commitment_digest_sha256=commitment_digest,
        )

    @classmethod
    def _identity_row_digests(
        cls,
        section: str,
        rows: list[Any],
        fields: tuple[str, ...],
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ProductProjectError(f"semantic history has invalid {section} record")
            identity = tuple(row.get(field) for field in fields)
            if any(value is None for value in identity):
                raise ProductProjectError(
                    f"semantic history has incomplete {section} identity"
                )
            identity_digest = _digest({"section": section, "identity": identity})
            if identity_digest in result:
                raise ProductProjectError(
                    f"semantic history has duplicate {section} identity"
                )
            result[identity_digest] = _digest(row)
        return result

    @staticmethod
    def _rows(history: dict[str, Any], section: str) -> list[Any]:
        rows = history.get(section)
        if not isinstance(rows, list):
            raise ProductProjectError(f"semantic history archive has invalid {section}")
        return rows

    @classmethod
    def _decode_descriptor(cls, raw: bytes) -> tuple[dict[str, Any], str]:
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProductProjectError("invalid semantic history descriptor encoding") from exc
        if not isinstance(envelope, dict):
            raise ProductProjectError("invalid semantic history descriptor envelope")
        payload = envelope.get("payload")
        digest = envelope.get("digest_sha256")
        if not isinstance(payload, dict) or not cls._is_digest(digest):
            raise ProductProjectError("incomplete semantic history descriptor envelope")
        if payload.get("schema") != _SCHEMA:
            raise ProductProjectError("unsupported semantic history descriptor schema")
        if _digest(payload) != digest:
            raise ProductProjectError("semantic history descriptor digest mismatch")
        cls._validate_payload(payload)
        return payload, digest

    @staticmethod
    def _is_int(value: Any, *, minimum: int) -> bool:
        return type(value) is int and value >= minimum

    @classmethod
    def _validate_payload(cls, payload: dict[str, Any]) -> None:
        if not isinstance(payload.get("project_id"), str) or not payload["project_id"].strip():
            raise ProductProjectError("semantic anchor project identity is invalid")
        if not cls._is_int(payload.get("generation"), minimum=1):
            raise ProductProjectError("semantic anchor generation is invalid")
        if not cls._is_int(payload.get("spec_version"), minimum=1):
            raise ProductProjectError("semantic anchor spec version is invalid")
        if not cls._is_int(payload.get("row_version"), minimum=0):
            raise ProductProjectError("semantic anchor row version is invalid")
        for key in ("archive_digest_sha256", "generation_manifest_digest_sha256"):
            if not cls._is_digest(payload.get(key)):
                raise ProductProjectError(f"semantic anchor {key} is invalid")
        if not isinstance(payload.get("ordered_sections"), list):
            raise ProductProjectError("semantic anchor ordered sections are invalid")
        if not isinstance(payload.get("immutable_sections"), list):
            raise ProductProjectError("semantic anchor immutable sections are invalid")

    @classmethod
    def _ordered_from_payload(cls, payload: Any) -> OrderedSectionCommitment:
        if not isinstance(payload, dict):
            raise ProductProjectError("semantic ordered commitment is invalid")
        section = payload.get("section")
        count = payload.get("record_count")
        digest = payload.get("prefix_digest_sha256")
        if section not in _ORDERED_SECTIONS:
            raise ProductProjectError("semantic ordered commitment section is invalid")
        if not cls._is_int(count, minimum=0) or not cls._is_digest(digest):
            raise ProductProjectError("semantic ordered commitment value is invalid")
        return OrderedSectionCommitment(section, count, digest)

    @classmethod
    def _immutable_from_payload(cls, payload: Any) -> ImmutableSectionCommitment:
        if not isinstance(payload, dict):
            raise ProductProjectError("semantic immutable commitment is invalid")
        section = payload.get("section")
        count = payload.get("record_count")
        raw_records = payload.get("records")
        commitment_digest = payload.get("commitment_digest_sha256")
        if section not in _IMMUTABLE_IDENTITIES:
            raise ProductProjectError("semantic immutable commitment section is invalid")
        if not cls._is_int(count, minimum=0) or not isinstance(raw_records, list):
            raise ProductProjectError("semantic immutable commitment value is invalid")
        records: list[ImmutableRecordCommitment] = []
        seen: set[str] = set()
        for item in raw_records:
            if not isinstance(item, dict):
                raise ProductProjectError("semantic immutable record commitment is invalid")
            identity = item.get("identity_digest_sha256")
            row_digest = item.get("row_digest_sha256")
            if not cls._is_digest(identity) or not cls._is_digest(row_digest):
                raise ProductProjectError("semantic immutable record digest is invalid")
            if identity in seen:
                raise ProductProjectError("semantic immutable commitment has duplicate identity")
            seen.add(identity)
            records.append(ImmutableRecordCommitment(identity, row_digest))
        records.sort(key=lambda item: item.identity_digest_sha256)
        if len(records) != count:
            raise ProductProjectError("semantic immutable commitment count mismatch")
        calculated = _digest(
            [
                {
                    "identity_digest_sha256": item.identity_digest_sha256,
                    "row_digest_sha256": item.row_digest_sha256,
                }
                for item in records
            ]
        )
        if not cls._is_digest(commitment_digest) or calculated != commitment_digest:
            raise ProductProjectError("semantic immutable commitment digest mismatch")
        return ImmutableSectionCommitment(
            section=section,
            record_count=count,
            records=tuple(records),
            commitment_digest_sha256=commitment_digest,
        )

    @staticmethod
    def _is_digest(value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )
