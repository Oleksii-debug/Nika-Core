from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class BusinessAuthorizationUse(StrEnum):
    ONE_TIME = "one_time"
    STANDING_POLICY = "standing_policy"


@dataclass(frozen=True, slots=True)
class BusinessAuthorizationIntent:
    """Exact PF9 authorization subject presented to a trusted host authority."""

    objective_id: str
    purpose: str
    subject_id: str
    bindings: tuple[tuple[str, str], ...]
    use: BusinessAuthorizationUse = BusinessAuthorizationUse.ONE_TIME

    def __post_init__(self) -> None:
        _text(self.objective_id, "business authorization objective_id")
        _text(self.purpose, "business authorization purpose")
        _text(self.subject_id, "business authorization subject_id")
        if not isinstance(self.use, BusinessAuthorizationUse):
            raise ValueError("business authorization use is invalid")
        if not isinstance(self.bindings, tuple):
            raise ValueError("business authorization bindings must be a tuple")
        normalized: list[tuple[str, str]] = []
        names: set[str] = set()
        for item in self.bindings:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError("business authorization binding must be a key/value tuple")
            key, value = item
            _text(key, "business authorization binding key")
            _text(value, f"business authorization binding {key}")
            if key in names:
                raise ValueError(f"duplicate business authorization binding: {key}")
            names.add(key)
            normalized.append((key, value))
        object.__setattr__(self, "bindings", tuple(sorted(normalized)))

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "schema": "nika-pf9-business-authorization-v1",
                "objective_id": self.objective_id,
                "purpose": self.purpose,
                "subject_id": self.subject_id,
                "use": self.use.value,
                "bindings": [list(item) for item in self.bindings],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class BusinessAuthorizationAuthorityPort(Protocol):
    """Trusted-host boundary for PF9 approval and standing-policy evidence.

    ONE_TIME evidence must be current, bound to the exact intent fingerprint, and protected
    from reuse for a different intent. An implementation may allow idempotent replay of the
    same evidence/fingerprint pair so a crash after authority validation can be reconciled.

    STANDING_POLICY evidence may be reusable only while the host-owned policy remains active
    and its scope covers the exact intent. The authority must never let the business worker
    mint or widen that policy.
    """

    def authorize(
        self,
        *,
        intent: BusinessAuthorizationIntent,
        evidence_ref: str,
    ) -> bool: ...


def trusted_business_authorization(
    authority: BusinessAuthorizationAuthorityPort | None,
    *,
    intent: BusinessAuthorizationIntent,
    evidence_ref: str,
) -> bool:
    """Fail closed unless a trusted host authority positively validates the exact intent."""

    _text(evidence_ref, "business authorization evidence_ref")
    if authority is None:
        return False
    try:
        result = authority.authorize(intent=intent, evidence_ref=evidence_ref)
    except (LookupError, PermissionError, RuntimeError, TypeError, ValueError):
        return False
    return result is True


def _text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
