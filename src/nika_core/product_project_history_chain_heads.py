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
    ProductProjectHistoryGenerationSummary,
)

_CHAIN_HEAD_SCHEMA = "nika-product-project-history-chain-head-v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProductProjectHistoryChainHead:
    project_id: str
    generation: int
    spec_version: int
    row_version: int
    archive_digest_sha256: str
    segment_manifest_digest_sha256: str
    generation_manifest_digest_sha256: str
    previous_generation_manifest_digest_sha256: str | None
    previous_archive_digest_sha256: str | None
    descriptor_digest_sha256: str
    descriptor_bytes: bytes


@dataclass(frozen=True, slots=True)
class ProductProjectHistoryWindowVerification:
    trusted_anchor: ProductProjectHistoryChainHead
    verified_generations: tuple[ProductProjectHistoryGenerationSummary, ...]
    project_id: str
    head_generation: int
    head_spec_version: int
    head_row_version: int
    head_generation_manifest_digest_sha256: str
    head_archive_digest_sha256: str


class ProductProjectHistoryChainHeadService:
    """Verify partial PF12 history windows from an externally trusted checkpoint identity.

    The descriptor is digest-bound and portable, but it is not an authentication signature.
    Trust in the descriptor must come from the caller's protected policy/evidence channel.
    """

    def __init__(self, store: Any) -> None:
        self.store = store
        self.generations = ProductProjectHistoryGenerationService(store)

    def export(
        self,
        generation: ProductProjectHistoryGeneration,
    ) -> ProductProjectHistoryChainHead:
        summary = self.generations.verify(generation)
        payload = self._payload_from_summary(summary)
        descriptor_digest = _digest(payload)
        descriptor_bytes = _canonical(
            {"digest_sha256": descriptor_digest, "payload": payload}
        ).encode("utf-8")
        return self._from_payload(payload, descriptor_digest, descriptor_bytes)

    def verify_descriptor(self, descriptor_bytes: bytes) -> ProductProjectHistoryChainHead:
        payload, descriptor_digest = self._decode_descriptor(descriptor_bytes)
        return self._from_payload(payload, descriptor_digest, descriptor_bytes)

    def verify_window(
        self,
        trusted_anchor_bytes: bytes,
        generations: Sequence[ProductProjectHistoryGeneration],
        *,
        require_live_head: bool = False,
    ) -> ProductProjectHistoryWindowVerification:
        anchor = self.verify_descriptor(trusted_anchor_bytes)
        if not generations:
            raise ProductProjectError("history checkpoint window has no descendant generations")

        summaries = self.generations.verify_chain(generations, require_genesis=False)
        first = summaries[0]
        if first.project_id != anchor.project_id:
            raise ProductProjectError("history checkpoint window project mismatch")
        if first.generation != anchor.generation + 1:
            raise ProductProjectError(
                "history checkpoint window does not continue anchor generation"
            )
        if (
            first.previous_generation_manifest_digest_sha256
            != anchor.generation_manifest_digest_sha256
        ):
            raise ProductProjectError("history checkpoint window predecessor digest mismatch")
        if first.previous_archive_digest_sha256 != anchor.archive_digest_sha256:
            raise ProductProjectError("history checkpoint window archive ancestry mismatch")
        if first.spec_version < anchor.spec_version:
            raise ProductProjectError("history checkpoint window spec version rolled back")
        if first.row_version < anchor.row_version:
            raise ProductProjectError("history checkpoint window row version rolled back")
        if first.archive_digest_sha256 == anchor.archive_digest_sha256:
            raise ProductProjectError("history checkpoint window replayed trusted anchor head")

        for summary in summaries:
            if summary.project_id != anchor.project_id:
                raise ProductProjectError("history checkpoint window project mismatch")

        if require_live_head:
            head = generations[-1]
            self.generations.segments.verify_against_live(
                head.segment_manifest_bytes,
                head.segment_bytes,
            )

        head_summary = summaries[-1]
        return ProductProjectHistoryWindowVerification(
            trusted_anchor=anchor,
            verified_generations=summaries,
            project_id=anchor.project_id,
            head_generation=head_summary.generation,
            head_spec_version=head_summary.spec_version,
            head_row_version=head_summary.row_version,
            head_generation_manifest_digest_sha256=(
                head_summary.generation_manifest_digest_sha256
            ),
            head_archive_digest_sha256=head_summary.archive_digest_sha256,
        )

    def advance(
        self,
        trusted_anchor_bytes: bytes,
        generations: Sequence[ProductProjectHistoryGeneration],
        *,
        require_live_head: bool = True,
    ) -> ProductProjectHistoryChainHead:
        self.verify_window(
            trusted_anchor_bytes,
            generations,
            require_live_head=require_live_head,
        )
        return self.export(generations[-1])

    @staticmethod
    def _payload_from_summary(
        summary: ProductProjectHistoryGenerationSummary,
    ) -> dict[str, Any]:
        return {
            "schema": _CHAIN_HEAD_SCHEMA,
            "project_id": summary.project_id,
            "generation": summary.generation,
            "spec_version": summary.spec_version,
            "row_version": summary.row_version,
            "archive_digest_sha256": summary.archive_digest_sha256,
            "segment_manifest_digest_sha256": summary.segment_manifest_digest_sha256,
            "generation_manifest_digest_sha256": (
                summary.generation_manifest_digest_sha256
            ),
            "previous_generation_manifest_digest_sha256": (
                summary.previous_generation_manifest_digest_sha256
            ),
            "previous_archive_digest_sha256": summary.previous_archive_digest_sha256,
        }

    @classmethod
    def _decode_descriptor(cls, raw: bytes) -> tuple[dict[str, Any], str]:
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProductProjectError("invalid history chain-head descriptor encoding") from exc
        if not isinstance(envelope, dict):
            raise ProductProjectError("invalid history chain-head descriptor envelope")
        payload = envelope.get("payload")
        digest = envelope.get("digest_sha256")
        if not isinstance(payload, dict) or not cls._is_digest(digest):
            raise ProductProjectError("incomplete history chain-head descriptor envelope")
        if payload.get("schema") != _CHAIN_HEAD_SCHEMA:
            raise ProductProjectError("unsupported history chain-head descriptor schema")
        if _digest(payload) != digest:
            raise ProductProjectError("history chain-head descriptor digest mismatch")
        cls._validate_payload(payload)
        return payload, digest

    @classmethod
    def _validate_payload(cls, payload: dict[str, Any]) -> None:
        project_id = payload.get("project_id")
        generation = payload.get("generation")
        spec_version = payload.get("spec_version")
        row_version = payload.get("row_version")
        if not isinstance(project_id, str) or not project_id.strip():
            raise ProductProjectError("history chain-head project identity is invalid")
        if not isinstance(generation, int) or generation < 1:
            raise ProductProjectError("history chain-head generation is invalid")
        if not isinstance(spec_version, int) or spec_version < 1:
            raise ProductProjectError("history chain-head spec version is invalid")
        if not isinstance(row_version, int) or row_version < 0:
            raise ProductProjectError("history chain-head row version is invalid")
        for key in (
            "archive_digest_sha256",
            "segment_manifest_digest_sha256",
            "generation_manifest_digest_sha256",
        ):
            if not cls._is_digest(payload.get(key)):
                raise ProductProjectError(f"history chain-head {key} is invalid")

        previous_generation = payload.get("previous_generation_manifest_digest_sha256")
        previous_archive = payload.get("previous_archive_digest_sha256")
        if generation == 1:
            if previous_generation is not None or previous_archive is not None:
                raise ProductProjectError("genesis history chain-head has a predecessor")
        elif not cls._is_digest(previous_generation) or not cls._is_digest(previous_archive):
            raise ProductProjectError("history chain-head predecessor identity is invalid")

    @classmethod
    def _from_payload(
        cls,
        payload: dict[str, Any],
        descriptor_digest: str,
        descriptor_bytes: bytes,
    ) -> ProductProjectHistoryChainHead:
        cls._validate_payload(payload)
        return ProductProjectHistoryChainHead(
            project_id=payload["project_id"],
            generation=payload["generation"],
            spec_version=payload["spec_version"],
            row_version=payload["row_version"],
            archive_digest_sha256=payload["archive_digest_sha256"],
            segment_manifest_digest_sha256=payload["segment_manifest_digest_sha256"],
            generation_manifest_digest_sha256=payload[
                "generation_manifest_digest_sha256"
            ],
            previous_generation_manifest_digest_sha256=payload[
                "previous_generation_manifest_digest_sha256"
            ],
            previous_archive_digest_sha256=payload["previous_archive_digest_sha256"],
            descriptor_digest_sha256=descriptor_digest,
            descriptor_bytes=descriptor_bytes,
        )

    @staticmethod
    def _is_digest(value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )
