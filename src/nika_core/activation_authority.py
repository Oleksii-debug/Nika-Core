from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol


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
        if not self.kind.strip() or not self.subject_id.strip() or not self.version.strip():
            raise ValueError("activation subject identity must not be empty")
        if len(self.payload_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.payload_sha256
        ):
            raise ValueError("activation payload_sha256 must be lowercase SHA-256")
        if len(self.permission_ids) != len(set(self.permission_ids)):
            raise ValueError("duplicate activation permission identity")
        if len(self.high_impact_ids) != len(set(self.high_impact_ids)):
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
