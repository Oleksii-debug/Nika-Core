from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from nika_core.product_project import ProductProjectError
from nika_core.product_project_history_generations import ProductProjectHistoryGeneration
from nika_core.product_project_history_semantic_continuity import (
    ProductProjectHistorySemanticContinuityService,
)

_SCHEMA = "nika-product-project-history-sharded-commitments-v1"
_SHARD_SCHEMA = "nika-product-project-history-commitment-shard-v1"
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


@dataclass(frozen=True, slots=True)
class CommitmentShardSummary:
    section: str
    shard_index: int
    record_count: int
    first_identity_digest_sha256: str
    last_identity_digest_sha256: str
    shard_digest_sha256: str


@dataclass(frozen=True, slots=True)
class ProductProjectHistoryCommitmentIndex:
    project_id: str
    generation: int
    spec_version: int
    row_version: int
    archive_digest_sha256: str
    generation_manifest_digest_sha256: str
    target_records_per_shard: int
    total_immutable_records: int
    shards: tuple[CommitmentShardSummary, ...]
    descriptor_digest_sha256: str
    descriptor_bytes: bytes
    shard_bytes: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class CommitmentShardVerification:
    project_id: str
    anchor_generation: int
    head_generation: int
    verified_shards: tuple[tuple[str, int], ...]
    verified_records: int


class ProductProjectHistoryShardedCommitmentService:
    """Bound PF12 commitment transport by moving record commitments into shards.

    The descriptor carries only fixed-size metadata per shard. Individual shard blobs
    can be transported and verified selectively. SHA-256 provides tamper evidence,
    not authentication; trust in an index descriptor remains an external policy concern.
    """

    def __init__(self, store: Any) -> None:
        self.store = store
        self.semantic = ProductProjectHistorySemanticContinuityService(store)

    def export(
        self,
        generation: ProductProjectHistoryGeneration,
        *,
        target_records_per_shard: int = 256,
    ) -> ProductProjectHistoryCommitmentIndex:
        if type(target_records_per_shard) is not int or target_records_per_shard < 1:
            raise ProductProjectError("target_records_per_shard must be a positive integer")
        summary = self.semantic.generations.verify(generation)
        history = self.semantic._history_for_generation(generation)
        summaries: list[CommitmentShardSummary] = []
        blobs: list[bytes] = []
        total = 0
        for section, fields in _IMMUTABLE_IDENTITIES.items():
            records = self._records(section, history.get(section), fields)
            total += len(records)
            for shard_index, start in enumerate(range(0, len(records), target_records_per_shard)):
                rows = records[start : start + target_records_per_shard]
                payload = {
                    "schema": _SHARD_SCHEMA,
                    "project_id": summary.project_id,
                    "generation": summary.generation,
                    "section": section,
                    "shard_index": shard_index,
                    "records": [
                        {"identity_digest_sha256": identity, "row_digest_sha256": row_digest}
                        for identity, row_digest in rows
                    ],
                }
                shard_digest = _digest(payload)
                blob = _canonical(
                    {"digest_sha256": shard_digest, "payload": payload}
                ).encode("utf-8")
                summaries.append(
                    CommitmentShardSummary(
                        section=section,
                        shard_index=shard_index,
                        record_count=len(rows),
                        first_identity_digest_sha256=rows[0][0],
                        last_identity_digest_sha256=rows[-1][0],
                        shard_digest_sha256=shard_digest,
                    )
                )
                blobs.append(blob)
        payload = {
            "schema": _SCHEMA,
            "project_id": summary.project_id,
            "generation": summary.generation,
            "spec_version": summary.spec_version,
            "row_version": summary.row_version,
            "archive_digest_sha256": summary.archive_digest_sha256,
            "generation_manifest_digest_sha256": summary.generation_manifest_digest_sha256,
            "target_records_per_shard": target_records_per_shard,
            "total_immutable_records": total,
            "shards": [self._summary_payload(item) for item in summaries],
        }
        descriptor_digest = _digest(payload)
        descriptor = _canonical(
            {"digest_sha256": descriptor_digest, "payload": payload}
        ).encode("utf-8")
        return ProductProjectHistoryCommitmentIndex(
            project_id=summary.project_id,
            generation=summary.generation,
            spec_version=summary.spec_version,
            row_version=summary.row_version,
            archive_digest_sha256=summary.archive_digest_sha256,
            generation_manifest_digest_sha256=summary.generation_manifest_digest_sha256,
            target_records_per_shard=target_records_per_shard,
            total_immutable_records=total,
            shards=tuple(summaries),
            descriptor_digest_sha256=descriptor_digest,
            descriptor_bytes=descriptor,
            shard_bytes=tuple(blobs),
        )

    def verify_index(self, descriptor_bytes: bytes) -> ProductProjectHistoryCommitmentIndex:
        payload, descriptor_digest = self._decode_envelope(descriptor_bytes, _SCHEMA, "index")
        self._validate_index_payload(payload)
        summaries = tuple(self._summary_from_payload(item) for item in payload["shards"])
        self._validate_summary_layout(summaries, payload["target_records_per_shard"])
        if sum(item.record_count for item in summaries) != payload["total_immutable_records"]:
            raise ProductProjectError("commitment index immutable record count mismatch")
        return ProductProjectHistoryCommitmentIndex(
            project_id=payload["project_id"],
            generation=payload["generation"],
            spec_version=payload["spec_version"],
            row_version=payload["row_version"],
            archive_digest_sha256=payload["archive_digest_sha256"],
            generation_manifest_digest_sha256=payload[
                "generation_manifest_digest_sha256"
            ],
            target_records_per_shard=payload["target_records_per_shard"],
            total_immutable_records=payload["total_immutable_records"],
            shards=summaries,
            descriptor_digest_sha256=descriptor_digest,
            descriptor_bytes=descriptor_bytes,
            shard_bytes=(),
        )

    def verify_selected_shards(
        self,
        descriptor_bytes: bytes,
        shard_bytes: Sequence[bytes],
        descendants: Sequence[ProductProjectHistoryGeneration],
    ) -> CommitmentShardVerification:
        index = self.verify_index(descriptor_bytes)
        if not descendants:
            raise ProductProjectError("commitment shard proof has no descendant generations")
        summaries = self.semantic.generations.verify_chain(descendants, require_genesis=False)
        first = summaries[0]
        if first.project_id != index.project_id:
            raise ProductProjectError("commitment shard descendant project mismatch")
        if first.generation != index.generation + 1:
            raise ProductProjectError("commitment shard proof does not continue index generation")
        if (
            first.previous_generation_manifest_digest_sha256
            != index.generation_manifest_digest_sha256
        ):
            raise ProductProjectError("commitment shard predecessor digest mismatch")
        if first.previous_archive_digest_sha256 != index.archive_digest_sha256:
            raise ProductProjectError("commitment shard archive ancestry mismatch")
        history = self.semantic._history_for_generation(descendants[-1])
        expected = {(item.section, item.shard_index): item for item in index.shards}
        seen: set[tuple[str, int]] = set()
        verified_records = 0
        for raw in shard_bytes:
            payload, digest = self._decode_envelope(raw, _SHARD_SCHEMA, "shard")
            self._validate_shard_payload(payload)
            key = (payload["section"], payload["shard_index"])
            if key in seen:
                raise ProductProjectError("duplicate commitment shard supplied")
            summary = expected.get(key)
            if summary is None:
                raise ProductProjectError("commitment shard is not declared by index")
            if (
                payload["project_id"] != index.project_id
                or payload["generation"] != index.generation
            ):
                raise ProductProjectError("commitment shard index identity mismatch")
            if digest != summary.shard_digest_sha256:
                raise ProductProjectError("commitment shard digest does not match index")
            records = payload["records"]
            if len(records) != summary.record_count:
                raise ProductProjectError("commitment shard record count mismatch")
            if records[0]["identity_digest_sha256"] != summary.first_identity_digest_sha256:
                raise ProductProjectError("commitment shard first identity mismatch")
            if records[-1]["identity_digest_sha256"] != summary.last_identity_digest_sha256:
                raise ProductProjectError("commitment shard last identity mismatch")
            self._verify_records_preserved(payload["section"], records, history)
            verified_records += len(records)
            seen.add(key)
        if not seen:
            raise ProductProjectError("no commitment shards supplied")
        return CommitmentShardVerification(
            project_id=index.project_id,
            anchor_generation=index.generation,
            head_generation=summaries[-1].generation,
            verified_shards=tuple(sorted(seen)),
            verified_records=verified_records,
        )

    @classmethod
    def _records(
        cls,
        section: str,
        raw_rows: Any,
        fields: tuple[str, ...],
    ) -> list[tuple[str, str]]:
        if not isinstance(raw_rows, list):
            raise ProductProjectError(f"commitment history has invalid {section}")
        result: dict[str, str] = {}
        for row in raw_rows:
            if not isinstance(row, dict):
                raise ProductProjectError(f"commitment history has invalid {section} record")
            identity = tuple(row.get(field) for field in fields)
            if any(value is None for value in identity):
                raise ProductProjectError(f"commitment history has incomplete {section} identity")
            identity_digest = _digest({"section": section, "identity": identity})
            if identity_digest in result:
                raise ProductProjectError(f"commitment history has duplicate {section} identity")
            result[identity_digest] = _digest(row)
        return sorted(result.items())

    @classmethod
    def _verify_records_preserved(
        cls,
        section: str,
        records: list[dict[str, str]],
        history: dict[str, Any],
    ) -> None:
        fields = _IMMUTABLE_IDENTITIES.get(section)
        if fields is None:
            raise ProductProjectError("unknown commitment shard section")
        current = dict(cls._records(section, history.get(section), fields))
        for record in records:
            identity = record["identity_digest_sha256"]
            observed = current.get(identity)
            if observed is None:
                raise ProductProjectError(f"commitment history removed prior {section} record")
            if observed != record["row_digest_sha256"]:
                raise ProductProjectError(f"commitment history rewrote prior {section} record")

    @staticmethod
    def _summary_payload(item: CommitmentShardSummary) -> dict[str, Any]:
        return {
            "section": item.section,
            "shard_index": item.shard_index,
            "record_count": item.record_count,
            "first_identity_digest_sha256": item.first_identity_digest_sha256,
            "last_identity_digest_sha256": item.last_identity_digest_sha256,
            "shard_digest_sha256": item.shard_digest_sha256,
        }

    @classmethod
    def _summary_from_payload(cls, payload: Any) -> CommitmentShardSummary:
        if not isinstance(payload, dict):
            raise ProductProjectError("commitment shard summary is invalid")
        section = payload.get("section")
        shard_index = payload.get("shard_index")
        record_count = payload.get("record_count")
        if section not in _IMMUTABLE_IDENTITIES:
            raise ProductProjectError("commitment shard summary section is invalid")
        if not cls._is_int(shard_index, minimum=0):
            raise ProductProjectError("commitment shard summary index is invalid")
        if not cls._is_int(record_count, minimum=1):
            raise ProductProjectError("commitment shard summary count is invalid")
        for key in (
            "first_identity_digest_sha256",
            "last_identity_digest_sha256",
            "shard_digest_sha256",
        ):
            if not cls._is_digest(payload.get(key)):
                raise ProductProjectError(f"commitment shard summary {key} is invalid")
        return CommitmentShardSummary(
            section=section,
            shard_index=shard_index,
            record_count=record_count,
            first_identity_digest_sha256=payload["first_identity_digest_sha256"],
            last_identity_digest_sha256=payload["last_identity_digest_sha256"],
            shard_digest_sha256=payload["shard_digest_sha256"],
        )

    @classmethod
    def _validate_summary_layout(
        cls,
        summaries: tuple[CommitmentShardSummary, ...],
        target: int,
    ) -> None:
        by_section: dict[str, list[CommitmentShardSummary]] = {
            section: [] for section in _IMMUTABLE_IDENTITIES
        }
        for item in summaries:
            by_section[item.section].append(item)
            if item.record_count > target:
                raise ProductProjectError("commitment shard exceeds declared target size")
        for section, items in by_section.items():
            for expected_index, item in enumerate(items):
                if item.shard_index != expected_index:
                    raise ProductProjectError(
                        f"commitment shard indices are not contiguous: {section}"
                    )
                if item.first_identity_digest_sha256 > item.last_identity_digest_sha256:
                    raise ProductProjectError("commitment shard identity range is reversed")
                if expected_index and (
                    items[expected_index - 1].last_identity_digest_sha256
                    >= item.first_identity_digest_sha256
                ):
                    raise ProductProjectError("commitment shard identity ranges overlap")

    @staticmethod
    def _is_int(value: Any, *, minimum: int) -> bool:
        return type(value) is int and value >= minimum

    @classmethod
    def _validate_index_payload(cls, payload: dict[str, Any]) -> None:
        if not isinstance(payload.get("project_id"), str) or not payload["project_id"].strip():
            raise ProductProjectError("commitment index project identity is invalid")
        for key in ("generation", "spec_version"):
            if not cls._is_int(payload.get(key), minimum=1):
                raise ProductProjectError(f"commitment index {key} is invalid")
        if not cls._is_int(payload.get("row_version"), minimum=0):
            raise ProductProjectError("commitment index row_version is invalid")
        target = payload.get("target_records_per_shard")
        total = payload.get("total_immutable_records")
        if not cls._is_int(target, minimum=1):
            raise ProductProjectError("commitment index shard target is invalid")
        if not cls._is_int(total, minimum=0):
            raise ProductProjectError("commitment index record total is invalid")
        for key in ("archive_digest_sha256", "generation_manifest_digest_sha256"):
            if not cls._is_digest(payload.get(key)):
                raise ProductProjectError(f"commitment index {key} is invalid")
        if not isinstance(payload.get("shards"), list):
            raise ProductProjectError("commitment index shards are invalid")

    @classmethod
    def _validate_shard_payload(cls, payload: dict[str, Any]) -> None:
        if payload.get("section") not in _IMMUTABLE_IDENTITIES:
            raise ProductProjectError("commitment shard section is invalid")
        if not isinstance(payload.get("project_id"), str) or not payload["project_id"].strip():
            raise ProductProjectError("commitment shard project identity is invalid")
        if not cls._is_int(payload.get("generation"), minimum=1):
            raise ProductProjectError("commitment shard generation is invalid")
        if not cls._is_int(payload.get("shard_index"), minimum=0):
            raise ProductProjectError("commitment shard shard_index is invalid")
        records = payload.get("records")
        if not isinstance(records, list) or not records:
            raise ProductProjectError("commitment shard records are invalid")
        previous = ""
        for record in records:
            if not isinstance(record, dict):
                raise ProductProjectError("commitment shard record is invalid")
            identity = record.get("identity_digest_sha256")
            row_digest = record.get("row_digest_sha256")
            if not cls._is_digest(identity) or not cls._is_digest(row_digest):
                raise ProductProjectError("commitment shard record digest is invalid")
            if previous and identity <= previous:
                raise ProductProjectError("commitment shard records are not strictly ordered")
            previous = identity

    @classmethod
    def _decode_envelope(
        cls,
        raw: bytes,
        schema: str,
        kind: str,
    ) -> tuple[dict[str, Any], str]:
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProductProjectError(f"invalid commitment {kind} encoding") from exc
        if not isinstance(envelope, dict):
            raise ProductProjectError(f"invalid commitment {kind} envelope")
        payload = envelope.get("payload")
        digest = envelope.get("digest_sha256")
        if not isinstance(payload, dict) or not cls._is_digest(digest):
            raise ProductProjectError(f"incomplete commitment {kind} envelope")
        if payload.get("schema") != schema:
            raise ProductProjectError(f"unsupported commitment {kind} schema")
        if _digest(payload) != digest:
            raise ProductProjectError(f"commitment {kind} digest mismatch")
        return payload, digest

    @staticmethod
    def _is_digest(value: Any) -> bool:
        if not isinstance(value, str) or len(value) != 64:
            return False
        try:
            int(value, 16)
        except ValueError:
            return False
        return True
