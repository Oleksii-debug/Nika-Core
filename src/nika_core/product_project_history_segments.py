from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from nika_core.product_project import ProductProjectError
from nika_core.product_project_history_archive import ProductProjectHistoryArchiveService

_SEGMENT_SCHEMA = "nika-product-project-history-segment-v1"
_MANIFEST_SCHEMA = "nika-product-project-history-segment-manifest-v1"
_HISTORY_SECTIONS = (
    "project",
    "specs",
    "research_handoffs",
    "decisions",
    "creation_idempotency",
    "mutation_idempotency",
    "audit_events",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProductProjectHistorySegment:
    sequence: int
    digest_sha256: str
    bytes: bytes


@dataclass(frozen=True, slots=True)
class ProductProjectHistorySegmentBundle:
    project_id: str
    spec_version: int
    row_version: int
    archive_digest_sha256: str
    manifest_digest_sha256: str
    manifest_bytes: bytes
    segments: tuple[ProductProjectHistorySegment, ...]


@dataclass(frozen=True, slots=True)
class ProductProjectHistorySegmentSummary:
    project_id: str
    spec_version: int
    row_version: int
    archive_digest_sha256: str
    manifest_digest_sha256: str
    total_records: int
    segment_count: int
    section_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class ProductProjectHistoryRetentionPolicy:
    hot_segment_count: int
    allow_destructive_delete: bool = False

    def __post_init__(self) -> None:
        if type(self.hot_segment_count) is not int or self.hot_segment_count < 0:
            raise ProductProjectError("hot_segment_count must be a non-negative integer")
        if self.allow_destructive_delete:
            raise ProductProjectError(
                "PF1 history retention cannot authorize destructive deletion"
            )


@dataclass(frozen=True, slots=True)
class ProductProjectHistoryRetentionPlan:
    manifest_digest_sha256: str
    hot_sequences: tuple[int, ...]
    cold_sequences: tuple[int, ...]
    destructive_delete_allowed: bool = False


class ProductProjectHistorySegmentService:
    """Segment and verify canonical PF1 history without deleting durable live rows."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.archives = ProductProjectHistoryArchiveService(store)

    def build(
        self,
        project_id: str,
        *,
        target_entries_per_segment: int = 256,
        expected_spec_version: int | None = None,
        expected_row_version: int | None = None,
    ) -> ProductProjectHistorySegmentBundle:
        if type(target_entries_per_segment) is not int or target_entries_per_segment < 1:
            raise ProductProjectError("target_entries_per_segment must be a positive integer")
        archive = self.archives.build(
            project_id,
            expected_spec_version=expected_spec_version,
            expected_row_version=expected_row_version,
        )
        envelope = self._json_object(archive.bytes, "history archive")
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise ProductProjectError("history archive payload is missing")
        records, section_counts = self._flatten_history(payload)
        segment_refs: list[dict[str, Any]] = []
        segments: list[ProductProjectHistorySegment] = []
        previous_digest: str | None = None
        for sequence, start in enumerate(
            range(0, len(records), target_entries_per_segment),
            start=1,
        ):
            chunk = records[start : start + target_entries_per_segment]
            segment_payload = {
                "schema": _SEGMENT_SCHEMA,
                "project_id": archive.summary.project_id,
                "archive_digest_sha256": archive.summary.digest_sha256,
                "sequence": sequence,
                "previous_segment_digest_sha256": previous_digest,
                "records": chunk,
            }
            digest = _digest(segment_payload)
            segment_bytes = _canonical(
                {"digest_sha256": digest, "payload": segment_payload}
            ).encode("utf-8")
            segments.append(ProductProjectHistorySegment(sequence, digest, segment_bytes))
            segment_refs.append(
                {
                    "sequence": sequence,
                    "digest_sha256": digest,
                    "previous_segment_digest_sha256": previous_digest,
                    "record_count": len(chunk),
                }
            )
            previous_digest = digest
        manifest_payload = {
            "schema": _MANIFEST_SCHEMA,
            "project_id": archive.summary.project_id,
            "spec_version": archive.summary.spec_version,
            "row_version": archive.summary.row_version,
            "archive_digest_sha256": archive.summary.digest_sha256,
            "target_entries_per_segment": target_entries_per_segment,
            "total_records": len(records),
            "section_counts": section_counts,
            "segments": segment_refs,
        }
        manifest_digest = _digest(manifest_payload)
        manifest_bytes = _canonical(
            {"digest_sha256": manifest_digest, "payload": manifest_payload}
        ).encode("utf-8")
        return ProductProjectHistorySegmentBundle(
            project_id=archive.summary.project_id,
            spec_version=archive.summary.spec_version,
            row_version=archive.summary.row_version,
            archive_digest_sha256=archive.summary.digest_sha256,
            manifest_digest_sha256=manifest_digest,
            manifest_bytes=manifest_bytes,
            segments=tuple(segments),
        )

    def verify(
        self,
        manifest_bytes: bytes,
        segment_bytes: Iterable[bytes],
    ) -> ProductProjectHistorySegmentSummary:
        manifest_payload, manifest_digest = self._decode_envelope(
            manifest_bytes,
            expected_schema=_MANIFEST_SCHEMA,
            label="history segment manifest",
        )
        project_id = self._required_text(manifest_payload, "project_id")
        archive_digest = self._required_text(
            manifest_payload,
            "archive_digest_sha256",
        )
        spec_version = self._required_int(manifest_payload, "spec_version", minimum=1)
        row_version = self._required_int(manifest_payload, "row_version", minimum=0)
        total_records = self._required_int(manifest_payload, "total_records", minimum=1)
        target_entries = self._required_int(
            manifest_payload,
            "target_entries_per_segment",
            minimum=1,
        )
        section_counts = self._validate_section_counts(manifest_payload.get("section_counts"))
        refs = manifest_payload.get("segments")
        if not isinstance(refs, list) or not refs:
            raise ProductProjectError("history segment manifest has no segments")

        supplied = list(segment_bytes)
        if len(supplied) != len(refs):
            raise ProductProjectError("history segment count does not match manifest")
        records: list[dict[str, Any]] = []
        previous_digest: str | None = None
        for expected_sequence, (ref, raw) in enumerate(zip(refs, supplied), start=1):
            if not isinstance(ref, dict):
                raise ProductProjectError("history segment manifest has invalid segment reference")
            ref_sequence = self._required_int(ref, "sequence", minimum=1)
            if ref_sequence != expected_sequence:
                raise ProductProjectError("history segment sequence is not contiguous")
            ref_digest = self._required_text(ref, "digest_sha256")
            ref_previous = ref.get("previous_segment_digest_sha256")
            if ref_previous != previous_digest:
                raise ProductProjectError("history segment manifest chain is broken")
            ref_count = self._required_int(ref, "record_count", minimum=1)
            payload, digest = self._decode_envelope(
                raw,
                expected_schema=_SEGMENT_SCHEMA,
                label="history segment",
            )
            if digest != ref_digest:
                raise ProductProjectError("history segment digest does not match manifest")
            if payload.get("project_id") != project_id:
                raise ProductProjectError("history segment project identity mismatch")
            if payload.get("archive_digest_sha256") != archive_digest:
                raise ProductProjectError("history segment archive identity mismatch")
            payload_sequence = payload.get("sequence")
            if type(payload_sequence) is not int or payload_sequence != expected_sequence:
                raise ProductProjectError("history segment payload sequence mismatch")
            if payload.get("previous_segment_digest_sha256") != previous_digest:
                raise ProductProjectError("history segment payload chain is broken")
            chunk = payload.get("records")
            if not isinstance(chunk, list) or not chunk:
                raise ProductProjectError("history segment has no records")
            if len(chunk) != ref_count:
                raise ProductProjectError("history segment record count mismatch")
            if len(chunk) > target_entries:
                raise ProductProjectError("history segment exceeds manifest target size")
            records.extend(chunk)
            previous_digest = digest

        if len(records) != total_records:
            raise ProductProjectError("history segment manifest total record count mismatch")
        archive_bytes = self._reassemble_archive_bytes(
            project_id=project_id,
            spec_version=spec_version,
            row_version=row_version,
            archive_digest_sha256=archive_digest,
            records=records,
            expected_section_counts=dict(section_counts),
        )
        archive_summary = self.archives.verify(archive_bytes)
        if archive_summary.digest_sha256 != archive_digest:
            raise ProductProjectError("history segment archive digest mismatch")
        return ProductProjectHistorySegmentSummary(
            project_id=project_id,
            spec_version=spec_version,
            row_version=row_version,
            archive_digest_sha256=archive_digest,
            manifest_digest_sha256=manifest_digest,
            total_records=total_records,
            segment_count=len(refs),
            section_counts=section_counts,
        )

    def reassemble(
        self,
        manifest_bytes: bytes,
        segment_bytes: Iterable[bytes],
    ) -> bytes:
        supplied = tuple(segment_bytes)
        summary = self.verify(manifest_bytes, supplied)
        records: list[dict[str, Any]] = []
        for raw in supplied:
            payload, _ = self._decode_envelope(
                raw,
                expected_schema=_SEGMENT_SCHEMA,
                label="history segment",
            )
            records.extend(payload["records"])
        return self._reassemble_archive_bytes(
            project_id=summary.project_id,
            spec_version=summary.spec_version,
            row_version=summary.row_version,
            archive_digest_sha256=summary.archive_digest_sha256,
            records=records,
            expected_section_counts=dict(summary.section_counts),
        )

    def verify_against_live(
        self,
        manifest_bytes: bytes,
        segment_bytes: Iterable[bytes],
    ) -> ProductProjectHistorySegmentSummary:
        supplied = tuple(segment_bytes)
        summary = self.verify(manifest_bytes, supplied)
        archive_bytes = self.reassemble(manifest_bytes, supplied)
        live = self.archives.verify_against_live(archive_bytes)
        if live.digest_sha256 != summary.archive_digest_sha256:
            raise ProductProjectError("history segment manifest differs from live history")
        return summary

    def plan_retention(
        self,
        manifest_bytes: bytes,
        segment_bytes: Iterable[bytes],
        policy: ProductProjectHistoryRetentionPolicy,
    ) -> ProductProjectHistoryRetentionPlan:
        supplied = tuple(segment_bytes)
        summary = self.verify(manifest_bytes, supplied)
        hot_count = min(policy.hot_segment_count, summary.segment_count)
        first_hot = summary.segment_count - hot_count + 1
        hot = tuple(range(first_hot, summary.segment_count + 1)) if hot_count else ()
        cold = tuple(range(1, first_hot))
        return ProductProjectHistoryRetentionPlan(
            manifest_digest_sha256=summary.manifest_digest_sha256,
            hot_sequences=hot,
            cold_sequences=cold,
            destructive_delete_allowed=False,
        )

    @staticmethod
    def _flatten_history(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
        history = payload.get("history")
        if not isinstance(history, dict):
            raise ProductProjectError("history archive has no history object")
        records: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        project = history.get("project")
        if not isinstance(project, dict):
            raise ProductProjectError("history archive has invalid project row")
        records.append({"section": "project", "ordinal": 0, "value": project})
        counts["project"] = 1
        for section in _HISTORY_SECTIONS[1:]:
            values = history.get(section)
            if not isinstance(values, list):
                raise ProductProjectError(f"history archive has invalid {section}")
            counts[section] = len(values)
            records.extend(
                {"section": section, "ordinal": ordinal, "value": value}
                for ordinal, value in enumerate(values)
            )
        return records, counts

    @classmethod
    def _reassemble_archive_bytes(
        cls,
        *,
        project_id: str,
        spec_version: int,
        row_version: int,
        archive_digest_sha256: str,
        records: list[dict[str, Any]],
        expected_section_counts: dict[str, int],
    ) -> bytes:
        history: dict[str, Any] = {section: [] for section in _HISTORY_SECTIONS[1:]}
        project: dict[str, Any] | None = None
        seen: set[tuple[str, int]] = set()
        observed_counts = {section: 0 for section in _HISTORY_SECTIONS}
        for record in records:
            if not isinstance(record, dict):
                raise ProductProjectError("history segment record is invalid")
            section = record.get("section")
            ordinal = record.get("ordinal")
            if section not in _HISTORY_SECTIONS or type(ordinal) is not int or ordinal < 0:
                raise ProductProjectError("history segment record identity is invalid")
            identity = (section, ordinal)
            if identity in seen:
                raise ProductProjectError("duplicate history segment record identity")
            seen.add(identity)
            value = record.get("value")
            if section == "project":
                if ordinal != 0 or project is not None or not isinstance(value, dict):
                    raise ProductProjectError("history segment project record is invalid")
                project = value
            else:
                target = history[section]
                if ordinal != len(target):
                    raise ProductProjectError("history segment record ordinal is not contiguous")
                target.append(value)
            observed_counts[section] += 1
        if project is None:
            raise ProductProjectError("history segment project record is missing")
        if observed_counts != expected_section_counts:
            raise ProductProjectError("history segment section counts do not match manifest")
        history["project"] = project
        payload = {
            "schema": "nika-product-project-history-archive-v1",
            "project_id": project_id,
            "spec_version": spec_version,
            "row_version": row_version,
            "history": history,
        }
        if _digest(payload) != archive_digest_sha256:
            raise ProductProjectError("reassembled history archive digest mismatch")
        return _canonical(
            {"digest_sha256": archive_digest_sha256, "payload": payload}
        ).encode("utf-8")

    @classmethod
    def _decode_envelope(
        cls,
        raw: bytes,
        *,
        expected_schema: str,
        label: str,
    ) -> tuple[dict[str, Any], str]:
        envelope = cls._json_object(raw, label)
        payload = envelope.get("payload")
        digest = envelope.get("digest_sha256")
        if not isinstance(payload, dict) or not isinstance(digest, str) or not digest.strip():
            raise ProductProjectError(f"incomplete {label} envelope")
        if payload.get("schema") != expected_schema:
            raise ProductProjectError(f"unsupported {label} schema")
        if _digest(payload) != digest:
            raise ProductProjectError(f"{label} digest mismatch")
        return payload, digest

    @staticmethod
    def _json_object(raw: bytes, label: str) -> dict[str, Any]:
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProductProjectError(f"invalid {label} encoding") from exc
        if not isinstance(value, dict):
            raise ProductProjectError(f"invalid {label} envelope")
        return value

    @staticmethod
    def _required_text(value: dict[str, Any], key: str) -> str:
        item = value.get(key)
        if not isinstance(item, str) or not item.strip():
            raise ProductProjectError(f"history segment manifest has invalid {key}")
        return item

    @staticmethod
    def _required_int(value: dict[str, Any], key: str, *, minimum: int) -> int:
        item = value.get(key)
        if type(item) is not int or item < minimum:
            raise ProductProjectError(f"history segment manifest has invalid {key}")
        return item

    @staticmethod
    def _validate_section_counts(value: Any) -> tuple[tuple[str, int], ...]:
        if not isinstance(value, dict) or set(value) != set(_HISTORY_SECTIONS):
            raise ProductProjectError("history segment manifest has invalid section_counts")
        counts: list[tuple[str, int]] = []
        for section in _HISTORY_SECTIONS:
            count = value.get(section)
            minimum = 1 if section == "project" else 0
            if type(count) is not int or count < minimum:
                raise ProductProjectError("history segment manifest has invalid section_counts")
            counts.append((section, count))
        if dict(counts)["project"] != 1:
            raise ProductProjectError("history segment manifest must contain one project record")
        return tuple(counts)
