from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol


def _exact_text_tuple(value: object, *, label: str) -> tuple[str, ...]:
    if type(value) is not tuple or any(type(item) is not str or not item for item in value):
        raise ValueError(f"{label} must be an exact tuple of non-empty strings")
    return value


def canonical_approval_refs(value: object) -> tuple[str, ...]:
    refs = _exact_text_tuple(value, label="approval_refs")
    if len(refs) != len(set(refs)):
        raise ValueError("duplicate approval reference")
    return refs


@dataclass(frozen=True, slots=True)
class ActivationSubject:
    """Exact Nika-owned activation statement presented to a trusted host verifier."""

    kind: str
    subject_id: str
    version: str
    payload_sha256: str
    permission_ids: tuple[str, ...] = ()
    high_impact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(
            type(value) is not str
            for value in (self.kind, self.subject_id, self.version, self.payload_sha256)
        ):
            raise ValueError("activation subject scalar fields must be exact strings")
        if not self.kind.strip() or not self.subject_id.strip() or not self.version.strip():
            raise ValueError("activation subject identity must not be empty")
        if len(self.payload_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.payload_sha256
        ):
            raise ValueError("activation payload_sha256 must be lowercase SHA-256")
        permission_ids = _exact_text_tuple(
            self.permission_ids,
            label="activation permission_ids",
        )
        high_impact_ids = _exact_text_tuple(
            self.high_impact_ids,
            label="activation high_impact_ids",
        )
        if len(permission_ids) != len(set(permission_ids)):
            raise ValueError("duplicate activation permission identity")
        if len(high_impact_ids) != len(set(high_impact_ids)):
            raise ValueError("duplicate high-impact activation identity")

    @property
    def requires_authority(self) -> bool:
        return bool(self.permission_ids or self.high_impact_ids)

    @classmethod
    def from_payload(
        cls,
        *,
        kind: str,
        subject_id: str,
        version: str,
        payload: object,
        permission_ids: tuple[str, ...] = (),
        high_impact_ids: tuple[str, ...] = (),
    ) -> ActivationSubject:
        if any(type(value) is not str for value in (kind, subject_id, version)):
            raise ValueError("activation subject identity must use exact strings")
        permission_ids = _exact_text_tuple(
            permission_ids,
            label="activation permission_ids",
        )
        high_impact_ids = _exact_text_tuple(
            high_impact_ids,
            label="activation high_impact_ids",
        )
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return cls(
            kind=kind,
            subject_id=subject_id,
            version=version,
            payload_sha256=hashlib.sha256(encoded).hexdigest(),
            permission_ids=tuple(sorted(permission_ids)),
            high_impact_ids=tuple(sorted(high_impact_ids)),
        )


class ActivationAuthorityPort(Protocol):
    """Trusted host boundary; approval references are evidence, never authority by themselves."""

    def verify(self, subject: ActivationSubject, approval_refs: tuple[str, ...]) -> None: ...
