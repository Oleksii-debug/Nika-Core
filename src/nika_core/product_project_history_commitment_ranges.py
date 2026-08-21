from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from nika_core.product_project import ProductProjectError
from nika_core.product_project_history_generations import ProductProjectHistoryGeneration
from nika_core.product_project_history_sharded_commitments import (
    CommitmentShardSummary,
    ProductProjectHistoryCommitmentIndex,
    ProductProjectHistoryShardedCommitmentService,
)

_SCHEMA = "nika-product-project-history-range-index-v2"
_PROOF_SCHEMA = "nika-product-project-history-shard-range-proof-v1"
_SHARD_SCHEMA = "nika-product-project-history-commitment-shard-v1"
_SECTIONS = (
    "research_handoffs",
    "decisions",
    "creation_idempotency",
    "mutation_idempotency",
)
_IDENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "research_handoffs": ("package_id",),
    "decisions": ("decision_id", "decision_version"),
    "creation_idempotency": ("operation_key",),
    "mutation_idempotency": ("operation_key",),
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _combine(left: str, right: str) -> str:
    return _digest({"left": left, "right": right})


@dataclass(frozen=True, slots=True)
class SectionCommitmentRoot:
    section: str
    shard_count: int
    record_count: int
    first_identity_digest_sha256: str | None
    last_identity_digest_sha256: str | None
    merkle_root_sha256: str


@dataclass(frozen=True, slots=True)
class ProductProjectHistoryRangeCommitmentIndex:
    project_id: str
    generation: int
    spec_version: int
    row_version: int
    archive_digest_sha256: str
    generation_manifest_digest_sha256: str
    source_v1_descriptor_digest_sha256: str
    total_immutable_records: int
    sections: tuple[SectionCommitmentRoot, ...]
    descriptor_digest_sha256: str
    descriptor_bytes: bytes


@dataclass(frozen=True, slots=True)
class ProductProjectHistoryShardRangeProof:
    project_id: str
    generation: int
    section: str
    start_shard_index: int
    stop_shard_index: int
    record_count: int
    proof_digest_sha256: str
    proof_bytes: bytes


@dataclass(frozen=True, slots=True)
class ProductProjectHistoryShardRangeVerification:
    project_id: str
    anchor_generation: int
    head_generation: int
    section: str
    start_shard_index: int
    stop_shard_index: int
    verified_shards: int
    verified_records: int


class ProductProjectHistoryCommitmentRangeService:
    """Upgrade PF12 shard indexes to compact roots and prove selected shard ranges.

    V1 remains immutable historical truth. V2 is a deterministic derived descriptor:
    each immutable section is represented by one Merkle root plus bounded metadata.
    A verifier can receive only the requested contiguous shard range, O(log n) sibling
    paths per shard, and descendant generations. SHA-256 is tamper evidence, not PKI.
    """

    def __init__(self, store: Any) -> None:
        self.store = store
        self.shards = ProductProjectHistoryShardedCommitmentService(store)
        self.generations = self.shards.semantic.generations

    def upgrade_v1(self, descriptor_bytes: bytes) -> ProductProjectHistoryRangeCommitmentIndex:
        source = self.shards.verify_index(descriptor_bytes)
        sections = tuple(self._section_root(section, source.shards) for section in _SECTIONS)
        payload = {
            "schema": _SCHEMA,
            "project_id": source.project_id,
            "generation": source.generation,
            "spec_version": source.spec_version,
            "row_version": source.row_version,
            "archive_digest_sha256": source.archive_digest_sha256,
            "generation_manifest_digest_sha256": source.generation_manifest_digest_sha256,
            "source_v1_descriptor_digest_sha256": source.descriptor_digest_sha256,
            "total_immutable_records": source.total_immutable_records,
            "sections": [self._section_payload(item) for item in sections],
        }
        descriptor_digest = _digest(payload)
        raw = _canonical({"digest_sha256": descriptor_digest, "payload": payload}).encode(
            "utf-8"
        )
        return ProductProjectHistoryRangeCommitmentIndex(
            project_id=source.project_id,
            generation=source.generation,
            spec_version=source.spec_version,
            row_version=source.row_version,
            archive_digest_sha256=source.archive_digest_sha256,
            generation_manifest_digest_sha256=source.generation_manifest_digest_sha256,
            source_v1_descriptor_digest_sha256=source.descriptor_digest_sha256,
            total_immutable_records=source.total_immutable_records,
            sections=sections,
            descriptor_digest_sha256=descriptor_digest,
            descriptor_bytes=raw,
        )

    def verify_index(self, descriptor_bytes: bytes) -> ProductProjectHistoryRangeCommitmentIndex:
        payload, digest = self._decode_envelope(descriptor_bytes, _SCHEMA, "range index")
        self._validate_index_payload(payload)
        sections = tuple(self._section_from_payload(item) for item in payload["sections"])
        if tuple(item.section for item in sections) != _SECTIONS:
            raise ProductProjectError("range commitment section order is invalid")
        if sum(item.record_count for item in sections) != payload["total_immutable_records"]:
            raise ProductProjectError("range commitment immutable record count mismatch")
        return ProductProjectHistoryRangeCommitmentIndex(
            project_id=payload["project_id"],
            generation=payload["generation"],
            spec_version=payload["spec_version"],
            row_version=payload["row_version"],
            archive_digest_sha256=payload["archive_digest_sha256"],
            generation_manifest_digest_sha256=payload[
                "generation_manifest_digest_sha256"
            ],
            source_v1_descriptor_digest_sha256=payload[
                "source_v1_descriptor_digest_sha256"
            ],
            total_immutable_records=payload["total_immutable_records"],
            sections=sections,
            descriptor_digest_sha256=digest,
            descriptor_bytes=descriptor_bytes,
        )

    def build_range_proof(
        self,
        source_v1_descriptor_bytes: bytes,
        *,
        section: str,
        start_shard_index: int,
        stop_shard_index: int,
    ) -> ProductProjectHistoryShardRangeProof:
        source = self.shards.verify_index(source_v1_descriptor_bytes)
        compact = self.upgrade_v1(source_v1_descriptor_bytes)
        items = tuple(item for item in source.shards if item.section == section)
        if section not in _SECTIONS:
            raise ProductProjectError("range proof section is invalid")
        if (
            start_shard_index < 0
            or stop_shard_index <= start_shard_index
            or stop_shard_index > len(items)
        ):
            raise ProductProjectError("range proof shard interval is invalid")
        digests = tuple(self._leaf_digest(item) for item in items)
        leaves = []
        record_count = 0
        for index in range(start_shard_index, stop_shard_index):
            item = items[index]
            record_count += item.record_count
            leaves.append(
                {
                    "summary": self._summary_payload(item),
                    "path": self._merkle_path(digests, index),
                }
            )
        payload = {
            "schema": _PROOF_SCHEMA,
            "project_id": source.project_id,
            "generation": source.generation,
            "source_v1_descriptor_digest_sha256": source.descriptor_digest_sha256,
            "range_index_digest_sha256": compact.descriptor_digest_sha256,
            "section": section,
            "start_shard_index": start_shard_index,
            "stop_shard_index": stop_shard_index,
            "record_count": record_count,
            "leaves": leaves,
        }
        proof_digest = _digest(payload)
        raw = _canonical({"digest_sha256": proof_digest, "payload": payload}).encode("utf-8")
        return ProductProjectHistoryShardRangeProof(
            project_id=source.project_id,
            generation=source.generation,
            section=section,
            start_shard_index=start_shard_index,
            stop_shard_index=stop_shard_index,
            record_count=record_count,
            proof_digest_sha256=proof_digest,
            proof_bytes=raw,
        )

    def verify_range_proof(
        self,
        compact_descriptor_bytes: bytes,
        proof_bytes: bytes,
        shard_bytes: Sequence[bytes],
        descendants: Sequence[ProductProjectHistoryGeneration],
        *,
        require_live_head: bool = False,
    ) -> ProductProjectHistoryShardRangeVerification:
        compact = self.verify_index(compact_descriptor_bytes)
        proof, _ = self._decode_envelope(proof_bytes, _PROOF_SCHEMA, "range proof")
        self._validate_proof_payload(proof)
        if proof["project_id"] != compact.project_id or proof["generation"] != compact.generation:
            raise ProductProjectError("range proof index identity mismatch")
        if (
            proof["source_v1_descriptor_digest_sha256"]
            != compact.source_v1_descriptor_digest_sha256
            or proof["range_index_digest_sha256"] != compact.descriptor_digest_sha256
        ):
            raise ProductProjectError("range proof is bound to a different commitment index")
        section_root = next(
            (item for item in compact.sections if item.section == proof["section"]), None
        )
        if section_root is None or section_root.shard_count == 0:
            raise ProductProjectError("range proof section has no committed shards")
        start = proof["start_shard_index"]
        stop = proof["stop_shard_index"]
        if start < 0 or stop <= start or stop > section_root.shard_count:
            raise ProductProjectError("range proof shard interval is invalid")
        leaves = proof["leaves"]
        if len(leaves) != stop - start or len(shard_bytes) != len(leaves):
            raise ProductProjectError("range proof does not cover the declared interval")

        summaries: list[CommitmentShardSummary] = []
        expected_records = 0
        for expected_index, leaf in zip(range(start, stop), leaves, strict=True):
            summary = self._summary_from_payload(leaf.get("summary"))
            if summary.section != proof["section"] or summary.shard_index != expected_index:
                raise ProductProjectError("range proof shard identities are not contiguous")
            self._verify_merkle_path(summary, leaf.get("path"), section_root.merkle_root_sha256)
            summaries.append(summary)
            expected_records += summary.record_count
        if expected_records != proof["record_count"]:
            raise ProductProjectError("range proof record count mismatch")

        if not descendants:
            raise ProductProjectError("range proof has no descendant generations")
        generation_summaries = self.generations.verify_chain(descendants, require_genesis=False)
        first = generation_summaries[0]
        if first.project_id != compact.project_id:
            raise ProductProjectError("range proof descendant project mismatch")
        if first.generation != compact.generation + 1:
            raise ProductProjectError("range proof does not continue commitment generation")
        if (
            first.previous_generation_manifest_digest_sha256
            != compact.generation_manifest_digest_sha256
        ):
            raise ProductProjectError("range proof predecessor digest mismatch")
        if first.previous_archive_digest_sha256 != compact.archive_digest_sha256:
            raise ProductProjectError("range proof archive ancestry mismatch")
        if first.spec_version < compact.spec_version or first.row_version < compact.row_version:
            raise ProductProjectError("range proof descendant version rolled back")
        if require_live_head:
            head = descendants[-1]
            self.generations.segments.verify_against_live(
                head.segment_manifest_bytes,
                head.segment_bytes,
            )

        head_archive = self.generations.segments.reassemble(
            descendants[-1].segment_manifest_bytes,
            descendants[-1].segment_bytes,
        )
        history = self._archive_history(head_archive)
        verified_records = 0
        for summary, raw in zip(summaries, shard_bytes, strict=True):
            shard_payload, shard_digest = self._decode_envelope(raw, _SHARD_SCHEMA, "shard")
            self._validate_shard_payload(shard_payload)
            if (
                shard_payload["project_id"] != compact.project_id
                or shard_payload["generation"] != compact.generation
                or shard_payload["section"] != summary.section
                or shard_payload["shard_index"] != summary.shard_index
            ):
                raise ProductProjectError("range proof shard identity mismatch")
            if shard_digest != summary.shard_digest_sha256:
                raise ProductProjectError("range proof shard digest mismatch")
            records = shard_payload["records"]
            if len(records) != summary.record_count:
                raise ProductProjectError("range proof shard record count mismatch")
            if records[0]["identity_digest_sha256"] != summary.first_identity_digest_sha256:
                raise ProductProjectError("range proof shard first identity mismatch")
            if records[-1]["identity_digest_sha256"] != summary.last_identity_digest_sha256:
                raise ProductProjectError("range proof shard last identity mismatch")
            self._verify_records_preserved(summary.section, records, history)
            verified_records += len(records)

        return ProductProjectHistoryShardRangeVerification(
            project_id=compact.project_id,
            anchor_generation=compact.generation,
            head_generation=generation_summaries[-1].generation,
            section=proof["section"],
            start_shard_index=start,
            stop_shard_index=stop,
            verified_shards=len(summaries),
            verified_records=verified_records,
        )

    @classmethod
    def _section_root(
        cls,
        section: str,
        summaries: Sequence[CommitmentShardSummary],
    ) -> SectionCommitmentRoot:
        items = tuple(item for item in summaries if item.section == section)
        if not items:
            return SectionCommitmentRoot(
                section=section,
                shard_count=0,
                record_count=0,
                first_identity_digest_sha256=None,
                last_identity_digest_sha256=None,
                merkle_root_sha256=_digest({"section": section, "empty": True}),
            )
        return SectionCommitmentRoot(
            section=section,
            shard_count=len(items),
            record_count=sum(item.record_count for item in items),
            first_identity_digest_sha256=items[0].first_identity_digest_sha256,
            last_identity_digest_sha256=items[-1].last_identity_digest_sha256,
            merkle_root_sha256=cls._merkle_root(tuple(cls._leaf_digest(item) for item in items)),
        )

    @classmethod
    def _leaf_digest(cls, item: CommitmentShardSummary) -> str:
        return _digest(cls._summary_payload(item))

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
            raise ProductProjectError("range proof shard summary is invalid")
        section = payload.get("section")
        shard_index = payload.get("shard_index")
        record_count = payload.get("record_count")
        if section not in _SECTIONS:
            raise ProductProjectError("range proof shard summary section is invalid")
        if not isinstance(shard_index, int) or shard_index < 0:
            raise ProductProjectError("range proof shard summary index is invalid")
        if not isinstance(record_count, int) or record_count < 1:
            raise ProductProjectError("range proof shard summary count is invalid")
        for key in (
            "first_identity_digest_sha256",
            "last_identity_digest_sha256",
            "shard_digest_sha256",
        ):
            if not cls._is_digest(payload.get(key)):
                raise ProductProjectError(f"range proof shard summary {key} is invalid")
        return CommitmentShardSummary(
            section=section,
            shard_index=shard_index,
            record_count=record_count,
            first_identity_digest_sha256=payload["first_identity_digest_sha256"],
            last_identity_digest_sha256=payload["last_identity_digest_sha256"],
            shard_digest_sha256=payload["shard_digest_sha256"],
        )

    @classmethod
    def _merkle_root(cls, leaves: Sequence[str]) -> str:
        if not leaves:
            raise ProductProjectError("cannot build Merkle root without leaves")
        level = list(leaves)
        while len(level) > 1:
            next_level: list[str] = []
            for index in range(0, len(level), 2):
                left = level[index]
                right = level[index + 1] if index + 1 < len(level) else left
                next_level.append(_combine(left, right))
            level = next_level
        return level[0]

    @classmethod
    def _merkle_path(cls, leaves: Sequence[str], leaf_index: int) -> list[dict[str, str]]:
        if leaf_index < 0 or leaf_index >= len(leaves):
            raise ProductProjectError("Merkle leaf index is invalid")
        index = leaf_index
        level = list(leaves)
        path: list[dict[str, str]] = []
        while len(level) > 1:
            if index % 2:
                sibling_index = index - 1
                position = "left"
            else:
                sibling_index = index + 1 if index + 1 < len(level) else index
                position = "right"
            path.append({"position": position, "digest_sha256": level[sibling_index]})
            next_level: list[str] = []
            for position_index in range(0, len(level), 2):
                left = level[position_index]
                right = (
                    level[position_index + 1]
                    if position_index + 1 < len(level)
                    else left
                )
                next_level.append(_combine(left, right))
            index //= 2
            level = next_level
        return path

    @classmethod
    def _verify_merkle_path(
        cls,
        summary: CommitmentShardSummary,
        raw_path: Any,
        expected_root: str,
    ) -> None:
        if not isinstance(raw_path, list):
            raise ProductProjectError("range proof Merkle path is invalid")
        current = cls._leaf_digest(summary)
        for node in raw_path:
            if not isinstance(node, dict):
                raise ProductProjectError("range proof Merkle node is invalid")
            position = node.get("position")
            sibling = node.get("digest_sha256")
            if position not in {"left", "right"} or not cls._is_digest(sibling):
                raise ProductProjectError("range proof Merkle node is invalid")
            current = (
                _combine(sibling, current)
                if position == "left"
                else _combine(current, sibling)
            )
        if current != expected_root:
            raise ProductProjectError("range proof Merkle root mismatch")

    @staticmethod
    def _section_payload(item: SectionCommitmentRoot) -> dict[str, Any]:
        return {
            "section": item.section,
            "shard_count": item.shard_count,
            "record_count": item.record_count,
            "first_identity_digest_sha256": item.first_identity_digest_sha256,
            "last_identity_digest_sha256": item.last_identity_digest_sha256,
            "merkle_root_sha256": item.merkle_root_sha256,
        }

    @classmethod
    def _section_from_payload(cls, payload: Any) -> SectionCommitmentRoot:
        if not isinstance(payload, dict) or payload.get("section") not in _SECTIONS:
            raise ProductProjectError("range commitment section is invalid")
        shard_count = payload.get("shard_count")
        record_count = payload.get("record_count")
        if not isinstance(shard_count, int) or shard_count < 0:
            raise ProductProjectError("range commitment shard count is invalid")
        if not isinstance(record_count, int) or record_count < 0:
            raise ProductProjectError("range commitment record count is invalid")
        if not cls._is_digest(payload.get("merkle_root_sha256")):
            raise ProductProjectError("range commitment Merkle root is invalid")
        first = payload.get("first_identity_digest_sha256")
        last = payload.get("last_identity_digest_sha256")
        if shard_count == 0:
            if record_count != 0 or first is not None or last is not None:
                raise ProductProjectError("empty range commitment section is inconsistent")
        else:
            if record_count < shard_count or not cls._is_digest(first) or not cls._is_digest(last):
                raise ProductProjectError("range commitment identity bounds are invalid")
            if first > last:
                raise ProductProjectError("range commitment identity bounds are reversed")
        return SectionCommitmentRoot(
            section=payload["section"],
            shard_count=shard_count,
            record_count=record_count,
            first_identity_digest_sha256=first,
            last_identity_digest_sha256=last,
            merkle_root_sha256=payload["merkle_root_sha256"],
        )

    @classmethod
    def _validate_index_payload(cls, payload: dict[str, Any]) -> None:
        if not isinstance(payload.get("project_id"), str) or not payload["project_id"].strip():
            raise ProductProjectError("range commitment project identity is invalid")
        for key in ("generation", "spec_version"):
            if not isinstance(payload.get(key), int) or payload[key] < 1:
                raise ProductProjectError(f"range commitment {key} is invalid")
        if not isinstance(payload.get("row_version"), int) or payload["row_version"] < 0:
            raise ProductProjectError("range commitment row_version is invalid")
        if not isinstance(payload.get("total_immutable_records"), int) or payload[
            "total_immutable_records"
        ] < 0:
            raise ProductProjectError("range commitment total is invalid")
        for key in (
            "archive_digest_sha256",
            "generation_manifest_digest_sha256",
            "source_v1_descriptor_digest_sha256",
        ):
            if not cls._is_digest(payload.get(key)):
                raise ProductProjectError(f"range commitment {key} is invalid")
        if not isinstance(payload.get("sections"), list) or len(payload["sections"]) != len(
            _SECTIONS
        ):
            raise ProductProjectError("range commitment sections are invalid")

    @classmethod
    def _validate_proof_payload(cls, payload: dict[str, Any]) -> None:
        if not isinstance(payload.get("project_id"), str) or not payload["project_id"].strip():
            raise ProductProjectError("range proof project identity is invalid")
        if not isinstance(payload.get("generation"), int) or payload["generation"] < 1:
            raise ProductProjectError("range proof generation is invalid")
        if payload.get("section") not in _SECTIONS:
            raise ProductProjectError("range proof section is invalid")
        for key in ("start_shard_index", "stop_shard_index", "record_count"):
            if not isinstance(payload.get(key), int) or payload[key] < 0:
                raise ProductProjectError(f"range proof {key} is invalid")
        for key in (
            "source_v1_descriptor_digest_sha256",
            "range_index_digest_sha256",
        ):
            if not cls._is_digest(payload.get(key)):
                raise ProductProjectError(f"range proof {key} is invalid")
        if not isinstance(payload.get("leaves"), list) or not payload["leaves"]:
            raise ProductProjectError("range proof leaves are invalid")

    @classmethod
    def _validate_shard_payload(cls, payload: dict[str, Any]) -> None:
        if payload.get("section") not in _SECTIONS:
            raise ProductProjectError("range proof shard section is invalid")
        if not isinstance(payload.get("project_id"), str) or not payload["project_id"].strip():
            raise ProductProjectError("range proof shard project identity is invalid")
        for key in ("generation", "shard_index"):
            minimum = 1 if key == "generation" else 0
            if not isinstance(payload.get(key), int) or payload[key] < minimum:
                raise ProductProjectError(f"range proof shard {key} is invalid")
        records = payload.get("records")
        if not isinstance(records, list) or not records:
            raise ProductProjectError("range proof shard records are invalid")
        previous = ""
        for record in records:
            if not isinstance(record, dict):
                raise ProductProjectError("range proof shard record is invalid")
            identity = record.get("identity_digest_sha256")
            row_digest = record.get("row_digest_sha256")
            if not cls._is_digest(identity) or not cls._is_digest(row_digest):
                raise ProductProjectError("range proof shard record digest is invalid")
            if previous and identity <= previous:
                raise ProductProjectError("range proof shard records are not strictly ordered")
            previous = identity

    @classmethod
    def _verify_records_preserved(
        cls,
        section: str,
        records: list[dict[str, str]],
        history: dict[str, Any],
    ) -> None:
        fields = _IDENTITY_FIELDS[section]
        raw_rows = history.get(section)
        if not isinstance(raw_rows, list):
            raise ProductProjectError(f"range proof descendant history has invalid {section}")
        current: dict[str, str] = {}
        for row in raw_rows:
            if not isinstance(row, dict):
                raise ProductProjectError(f"range proof descendant has invalid {section} record")
            identity = tuple(row.get(field) for field in fields)
            if any(value is None for value in identity):
                raise ProductProjectError(
                    f"range proof descendant has incomplete {section} identity"
                )
            identity_digest = _digest({"section": section, "identity": identity})
            if identity_digest in current:
                raise ProductProjectError(
                    f"range proof descendant has duplicate {section} identity"
                )
            current[identity_digest] = _digest(row)
        for record in records:
            identity = record["identity_digest_sha256"]
            observed = current.get(identity)
            if observed is None:
                raise ProductProjectError(f"range proof descendant removed prior {section} record")
            if observed != record["row_digest_sha256"]:
                raise ProductProjectError(f"range proof descendant rewrote prior {section} record")

    @staticmethod
    def _archive_history(raw: bytes) -> dict[str, Any]:
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProductProjectError("invalid range proof descendant archive encoding") from exc
        if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict):
            raise ProductProjectError("invalid range proof descendant archive envelope")
        history = envelope["payload"].get("history")
        if not isinstance(history, dict):
            raise ProductProjectError("range proof descendant archive history is invalid")
        return history

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
            raise ProductProjectError(f"invalid {kind} encoding") from exc
        if not isinstance(envelope, dict):
            raise ProductProjectError(f"invalid {kind} envelope")
        payload = envelope.get("payload")
        digest = envelope.get("digest_sha256")
        if not isinstance(payload, dict) or not cls._is_digest(digest):
            raise ProductProjectError(f"incomplete {kind} envelope")
        if payload.get("schema") != schema:
            raise ProductProjectError(f"unsupported {kind} schema")
        if _digest(payload) != digest:
            raise ProductProjectError(f"{kind} digest mismatch")
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
