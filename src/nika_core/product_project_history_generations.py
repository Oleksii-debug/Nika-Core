from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from nika_core.product_project import ProductProjectError
from nika_core.product_project_history_segments import ProductProjectHistorySegmentService

_GENERATION_SCHEMA = "nika-product-project-history-generation-v1"
_HISTORY_LIST_SECTIONS = (
    "specs",
    "research_handoffs",
    "decisions",
    "creation_idempotency",
    "mutation_idempotency",
    "audit_events",
)
_SORTED_IMMUTABLE_IDENTITIES: dict[str, tuple[str, ...]] = {
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
class ProductProjectHistoryGeneration:
    generation: int
    project_id: str
    spec_version: int
    row_version: int
    archive_digest_sha256: str
    segment_manifest_digest_sha256: str
    generation_manifest_digest_sha256: str
    generation_manifest_bytes: bytes
    segment_manifest_bytes: bytes
    segment_bytes: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class ProductProjectHistoryGenerationSummary:
    generation: int
    project_id: str
    spec_version: int
    row_version: int
    archive_digest_sha256: str
    segment_manifest_digest_sha256: str
    generation_manifest_digest_sha256: str
    previous_generation_manifest_digest_sha256: str | None
    previous_archive_digest_sha256: str | None


@dataclass(frozen=True, slots=True)
class ProductProjectHistoryGenerationRetentionPolicy:
    hot_generation_count: int
    allow_destructive_delete: bool = False

    def __post_init__(self) -> None:
        if type(self.hot_generation_count) is not int or self.hot_generation_count < 0:
            raise ProductProjectError("hot_generation_count must be a non-negative integer")
        if self.allow_destructive_delete:
            raise ProductProjectError(
                "PF1 history generation retention cannot authorize destructive deletion"
            )


@dataclass(frozen=True, slots=True)
class ProductProjectHistoryGenerationRetentionPlan:
    chain_head_digest_sha256: str
    hot_generations: tuple[int, ...]
    cold_generations: tuple[int, ...]
    destructive_delete_allowed: bool = False


class ProductProjectHistoryGenerationService:
    """Chain successive PF1 history checkpoints and reject rollback/fork ancestry."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.segments = ProductProjectHistorySegmentService(store)

    def build(
        self,
        project_id: str,
        *,
        previous: ProductProjectHistoryGeneration | None = None,
        target_entries_per_segment: int = 256,
        expected_spec_version: int | None = None,
        expected_row_version: int | None = None,
    ) -> ProductProjectHistoryGeneration:
        bundle = self.segments.build(
            project_id,
            target_entries_per_segment=target_entries_per_segment,
            expected_spec_version=expected_spec_version,
            expected_row_version=expected_row_version,
        )
        current_segments = tuple(segment.bytes for segment in bundle.segments)
        current_archive = self.segments.reassemble(bundle.manifest_bytes, current_segments)

        generation = 1
        previous_generation_digest: str | None = None
        previous_archive_digest: str | None = None
        if previous is not None:
            previous_summary = self.verify(previous)
            if previous_summary.project_id != project_id:
                raise ProductProjectError("history generation predecessor project mismatch")
            previous_archive = self.segments.reassemble(
                previous.segment_manifest_bytes,
                previous.segment_bytes,
            )
            self._require_append_only(previous_archive, current_archive)
            if bundle.archive_digest_sha256 == previous.archive_digest_sha256:
                raise ProductProjectError("history generation must advance durable history")
            if bundle.spec_version < previous.spec_version:
                raise ProductProjectError("history generation spec version rolled back")
            if bundle.row_version < previous.row_version:
                raise ProductProjectError("history generation row version rolled back")
            generation = previous.generation + 1
            previous_generation_digest = previous.generation_manifest_digest_sha256
            previous_archive_digest = previous.archive_digest_sha256

        payload = {
            "schema": _GENERATION_SCHEMA,
            "project_id": project_id,
            "generation": generation,
            "spec_version": bundle.spec_version,
            "row_version": bundle.row_version,
            "archive_digest_sha256": bundle.archive_digest_sha256,
            "segment_manifest_digest_sha256": bundle.manifest_digest_sha256,
            "previous_generation_manifest_digest_sha256": previous_generation_digest,
            "previous_archive_digest_sha256": previous_archive_digest,
        }
        manifest_digest = _digest(payload)
        manifest_bytes = _canonical(
            {"digest_sha256": manifest_digest, "payload": payload}
        ).encode("utf-8")
        return ProductProjectHistoryGeneration(
            generation=generation,
            project_id=project_id,
            spec_version=bundle.spec_version,
            row_version=bundle.row_version,
            archive_digest_sha256=bundle.archive_digest_sha256,
            segment_manifest_digest_sha256=bundle.manifest_digest_sha256,
            generation_manifest_digest_sha256=manifest_digest,
            generation_manifest_bytes=manifest_bytes,
            segment_manifest_bytes=bundle.manifest_bytes,
            segment_bytes=current_segments,
        )

    def verify(
        self,
        generation: ProductProjectHistoryGeneration,
    ) -> ProductProjectHistoryGenerationSummary:
        self._validate_generation_identity(generation)
        payload, manifest_digest = self._decode_generation_manifest(
            generation.generation_manifest_bytes
        )
        segment_summary = self.segments.verify(
            generation.segment_manifest_bytes,
            generation.segment_bytes,
        )
        expected = {
            "generation": generation.generation,
            "project_id": generation.project_id,
            "spec_version": generation.spec_version,
            "row_version": generation.row_version,
            "archive_digest_sha256": generation.archive_digest_sha256,
            "segment_manifest_digest_sha256": generation.segment_manifest_digest_sha256,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                raise ProductProjectError(f"history generation {key} mismatch")
        if manifest_digest != generation.generation_manifest_digest_sha256:
            raise ProductProjectError("history generation manifest digest mismatch")
        if segment_summary.project_id != generation.project_id:
            raise ProductProjectError("history generation segment project mismatch")
        if segment_summary.spec_version != generation.spec_version:
            raise ProductProjectError("history generation segment spec version mismatch")
        if segment_summary.row_version != generation.row_version:
            raise ProductProjectError("history generation segment row version mismatch")
        if segment_summary.archive_digest_sha256 != generation.archive_digest_sha256:
            raise ProductProjectError("history generation archive digest mismatch")
        if (
            segment_summary.manifest_digest_sha256
            != generation.segment_manifest_digest_sha256
        ):
            raise ProductProjectError("history generation segment manifest digest mismatch")
        previous_generation = payload.get("previous_generation_manifest_digest_sha256")
        previous_archive = payload.get("previous_archive_digest_sha256")
        if generation.generation == 1:
            if previous_generation is not None or previous_archive is not None:
                raise ProductProjectError("genesis history generation has a predecessor")
        else:
            if not self._optional_digest(previous_generation) or not self._optional_digest(
                previous_archive
            ):
                raise ProductProjectError("history generation predecessor identity is missing")
        return ProductProjectHistoryGenerationSummary(
            generation=generation.generation,
            project_id=generation.project_id,
            spec_version=generation.spec_version,
            row_version=generation.row_version,
            archive_digest_sha256=generation.archive_digest_sha256,
            segment_manifest_digest_sha256=generation.segment_manifest_digest_sha256,
            generation_manifest_digest_sha256=manifest_digest,
            previous_generation_manifest_digest_sha256=previous_generation,
            previous_archive_digest_sha256=previous_archive,
        )

    def verify_chain(
        self,
        generations: Sequence[ProductProjectHistoryGeneration],
        *,
        require_genesis: bool = True,
    ) -> tuple[ProductProjectHistoryGenerationSummary, ...]:
        if not generations:
            raise ProductProjectError("history generation chain is empty")
        summaries: list[ProductProjectHistoryGenerationSummary] = []
        previous_summary: ProductProjectHistoryGenerationSummary | None = None
        previous_archive: bytes | None = None
        for item in generations:
            summary = self.verify(item)
            current_archive = self.segments.reassemble(
                item.segment_manifest_bytes,
                item.segment_bytes,
            )
            if previous_summary is None:
                if require_genesis and summary.generation != 1:
                    raise ProductProjectError("history generation chain does not start at genesis")
            else:
                assert previous_archive is not None
                if summary.project_id != previous_summary.project_id:
                    raise ProductProjectError("history generation chain project mismatch")
                if summary.generation != previous_summary.generation + 1:
                    raise ProductProjectError("history generation sequence is not contiguous")
                if (
                    summary.previous_generation_manifest_digest_sha256
                    != previous_summary.generation_manifest_digest_sha256
                ):
                    raise ProductProjectError("history generation chain predecessor is broken")
                if (
                    summary.previous_archive_digest_sha256
                    != previous_summary.archive_digest_sha256
                ):
                    raise ProductProjectError("history generation archive ancestry is broken")
                if summary.spec_version < previous_summary.spec_version:
                    raise ProductProjectError("history generation chain spec version rolled back")
                if summary.row_version < previous_summary.row_version:
                    raise ProductProjectError("history generation chain row version rolled back")
                if summary.archive_digest_sha256 == previous_summary.archive_digest_sha256:
                    raise ProductProjectError("history generation chain replayed an old head")
                self._require_append_only(previous_archive, current_archive)
            summaries.append(summary)
            previous_summary = summary
            previous_archive = current_archive
        return tuple(summaries)

    def verify_chain_against_live(
        self,
        generations: Sequence[ProductProjectHistoryGeneration],
    ) -> tuple[ProductProjectHistoryGenerationSummary, ...]:
        summaries = self.verify_chain(generations)
        head = generations[-1]
        self.segments.verify_against_live(
            head.segment_manifest_bytes,
            head.segment_bytes,
        )
        return summaries

    def plan_retention(
        self,
        generations: Sequence[ProductProjectHistoryGeneration],
        policy: ProductProjectHistoryGenerationRetentionPolicy,
    ) -> ProductProjectHistoryGenerationRetentionPlan:
        summaries = self.verify_chain(generations)
        hot_count = min(policy.hot_generation_count, len(summaries))
        first_hot_index = len(summaries) - hot_count
        hot = tuple(summary.generation for summary in summaries[first_hot_index:])
        cold = tuple(summary.generation for summary in summaries[:first_hot_index])
        return ProductProjectHistoryGenerationRetentionPlan(
            chain_head_digest_sha256=summaries[-1].generation_manifest_digest_sha256,
            hot_generations=hot,
            cold_generations=cold,
            destructive_delete_allowed=False,
        )

    @classmethod
    def _require_append_only(cls, older_archive: bytes, newer_archive: bytes) -> None:
        older = cls._archive_payload(older_archive)
        newer = cls._archive_payload(newer_archive)
        if older.get("project_id") != newer.get("project_id"):
            raise ProductProjectError("history generation archive project mismatch")
        older_history = older.get("history")
        newer_history = newer.get("history")
        if not isinstance(older_history, dict) or not isinstance(newer_history, dict):
            raise ProductProjectError("history generation archive history is invalid")
        for section in _HISTORY_LIST_SECTIONS:
            older_rows = older_history.get(section)
            newer_rows = newer_history.get(section)
            if not isinstance(older_rows, list) or not isinstance(newer_rows, list):
                raise ProductProjectError(
                    f"history generation archive has invalid {section}"
                )
            if len(newer_rows) < len(older_rows):
                raise ProductProjectError(f"history generation archive truncated {section}")
            identity_fields = _SORTED_IMMUTABLE_IDENTITIES.get(section)
            if identity_fields is None:
                if newer_rows[: len(older_rows)] != older_rows:
                    raise ProductProjectError(
                        f"history generation archive rewrote prior {section}"
                    )
                continue
            cls._require_immutable_rows_preserved(
                section,
                older_rows,
                newer_rows,
                identity_fields,
            )

    @classmethod
    def _require_immutable_rows_preserved(
        cls,
        section: str,
        older_rows: list[Any],
        newer_rows: list[Any],
        identity_fields: tuple[str, ...],
    ) -> None:
        older_by_identity = cls._rows_by_identity(section, older_rows, identity_fields)
        newer_by_identity = cls._rows_by_identity(section, newer_rows, identity_fields)
        for identity, older_row in older_by_identity.items():
            newer_row = newer_by_identity.get(identity)
            if newer_row is None:
                raise ProductProjectError(
                    f"history generation archive removed prior {section} record"
                )
            if newer_row != older_row:
                raise ProductProjectError(
                    f"history generation archive rewrote prior {section}"
                )

    @staticmethod
    def _rows_by_identity(
        section: str,
        rows: list[Any],
        identity_fields: tuple[str, ...],
    ) -> dict[tuple[Any, ...], dict[str, Any]]:
        result: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ProductProjectError(
                    f"history generation archive has invalid {section} record"
                )
            identity = tuple(row.get(field) for field in identity_fields)
            if any(value is None for value in identity):
                raise ProductProjectError(
                    f"history generation archive has incomplete {section} identity"
                )
            if identity in result:
                raise ProductProjectError(
                    f"history generation archive has duplicate {section} identity"
                )
            result[identity] = row
        return result

    @staticmethod
    def _archive_payload(raw: bytes) -> dict[str, Any]:
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProductProjectError("invalid history generation archive encoding") from exc
        if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict):
            raise ProductProjectError("invalid history generation archive envelope")
        return envelope["payload"]

    @staticmethod
    def _is_int(value: Any, *, minimum: int) -> bool:
        return type(value) is int and value >= minimum

    @classmethod
    def _validate_generation_identity(
        cls,
        generation: ProductProjectHistoryGeneration,
    ) -> None:
        if not cls._is_int(generation.generation, minimum=1):
            raise ProductProjectError("history generation number is invalid")
        if not cls._is_int(generation.spec_version, minimum=1):
            raise ProductProjectError("history generation spec version is invalid")
        if not cls._is_int(generation.row_version, minimum=0):
            raise ProductProjectError("history generation row version is invalid")
        if not isinstance(generation.project_id, str) or not generation.project_id.strip():
            raise ProductProjectError("history generation project identity is invalid")

    @classmethod
    def _decode_generation_manifest(cls, raw: bytes) -> tuple[dict[str, Any], str]:
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProductProjectError("invalid history generation manifest encoding") from exc
        if not isinstance(envelope, dict):
            raise ProductProjectError("invalid history generation manifest envelope")
        payload = envelope.get("payload")
        digest = envelope.get("digest_sha256")
        if not isinstance(payload, dict) or not isinstance(digest, str) or not digest.strip():
            raise ProductProjectError("incomplete history generation manifest envelope")
        if payload.get("schema") != _GENERATION_SCHEMA:
            raise ProductProjectError("unsupported history generation manifest schema")
        if _digest(payload) != digest:
            raise ProductProjectError("history generation manifest digest mismatch")
        generation = payload.get("generation")
        spec_version = payload.get("spec_version")
        row_version = payload.get("row_version")
        project_id = payload.get("project_id")
        if not cls._is_int(generation, minimum=1):
            raise ProductProjectError("history generation number is invalid")
        if not cls._is_int(spec_version, minimum=1):
            raise ProductProjectError("history generation spec version is invalid")
        if not cls._is_int(row_version, minimum=0):
            raise ProductProjectError("history generation row version is invalid")
        if not isinstance(project_id, str) or not project_id.strip():
            raise ProductProjectError("history generation project identity is invalid")
        for key in ("archive_digest_sha256", "segment_manifest_digest_sha256"):
            value = payload.get(key)
            if not isinstance(value, str) or len(value) != 64:
                raise ProductProjectError(f"history generation {key} is invalid")
        return payload, digest

    @staticmethod
    def _optional_digest(value: Any) -> bool:
        return isinstance(value, str) and len(value) == 64
