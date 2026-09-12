from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_MAX_SCOPE_ID_CHARS = 256
_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024


class LearningUpdateTarget(StrEnum):
    """Typed semantic target; mutation authority stays with the target owner."""

    MEMORY = "memory"
    WORLD_MODEL = "world_model"
    SELF_MODEL = "self_model"
    SKILL = "skill"


def _require_exact_str(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be an exact built-in str")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_exact_str(value, name)
    if _SHA256_RE.fullmatch(text) is None:
        raise ValueError(f"{name} must be a lowercase 64-character SHA-256 digest")
    return text


def _require_machine_id(value: object, name: str) -> str:
    text = _require_exact_str(value, name)
    if _MACHINE_ID_RE.fullmatch(text) is None:
        raise ValueError(f"{name} must be a bounded machine-safe identifier")
    return text


def _require_scope_id(value: object, name: str) -> str:
    text = _require_exact_str(value, name)
    if not text or len(text) > _MAX_SCOPE_ID_CHARS:
        raise ValueError(f"{name} must contain 1..{_MAX_SCOPE_ID_CHARS} characters")
    if text != text.strip():
        raise ValueError(f"{name} must not contain surrounding whitespace")
    if unicodedata.normalize("NFC", text) != text:
        raise ValueError(f"{name} must be NFC-normalized")
    if any(unicodedata.category(char).startswith("C") for char in text):
        raise ValueError(f"{name} must not contain control characters")
    return text


def _require_payload(payload: object) -> bytes:
    if type(payload) is not bytes:
        raise TypeError("payload must be exact built-in bytes")
    if not payload:
        raise ValueError("payload must not be empty")
    if len(payload) > _MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload exceeds {_MAX_PAYLOAD_BYTES} bytes")
    return payload


def _scope_sha256(*, workspace_id: str, agent_id: str) -> str:
    payload = {
        "agent_id": agent_id,
        "schema": "nika-loop-b-scope:v1",
        "workspace_id": workspace_id,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class LearningUpdateEvidence:
    """Privacy-minimized, non-authoritative evidence for one semantic update intent."""

    intent_sha256: str
    scope_sha256: str
    target: LearningUpdateTarget
    target_ref_sha256: str
    candidate_sha256: str
    verification_sha256: str
    update_schema: str
    payload_sha256: str
    payload_bytes: int
    expected_revision_sha256: str | None

    def __post_init__(self) -> None:
        _require_sha256(self.intent_sha256, "intent_sha256")
        _require_sha256(self.scope_sha256, "scope_sha256")
        if type(self.target) is not LearningUpdateTarget:
            raise TypeError("target must be LearningUpdateTarget")
        _require_sha256(self.target_ref_sha256, "target_ref_sha256")
        _require_sha256(self.candidate_sha256, "candidate_sha256")
        _require_sha256(self.verification_sha256, "verification_sha256")
        _require_machine_id(self.update_schema, "update_schema")
        _require_sha256(self.payload_sha256, "payload_sha256")
        if type(self.payload_bytes) is not int:
            raise TypeError("payload_bytes must be an exact built-in int")
        if not 1 <= self.payload_bytes <= _MAX_PAYLOAD_BYTES:
            raise ValueError("payload_bytes is outside the bounded payload range")
        if self.expected_revision_sha256 is not None:
            _require_sha256(self.expected_revision_sha256, "expected_revision_sha256")


@dataclass(frozen=True, slots=True)
class LearningUpdateIntent:
    """Evidence-bound Loop-B update intent with no mutation authority."""

    intent_id: str
    workspace_id: str
    agent_id: str
    target: LearningUpdateTarget
    target_ref_sha256: str
    candidate_sha256: str
    verification_sha256: str
    update_schema: str
    payload_sha256: str
    payload_bytes: int
    expected_revision_sha256: str | None = None

    def __post_init__(self) -> None:
        _require_machine_id(self.intent_id, "intent_id")
        _require_scope_id(self.workspace_id, "workspace_id")
        _require_scope_id(self.agent_id, "agent_id")
        if type(self.target) is not LearningUpdateTarget:
            raise TypeError("target must be LearningUpdateTarget")
        _require_sha256(self.target_ref_sha256, "target_ref_sha256")
        _require_sha256(self.candidate_sha256, "candidate_sha256")
        _require_sha256(self.verification_sha256, "verification_sha256")
        _require_machine_id(self.update_schema, "update_schema")
        _require_sha256(self.payload_sha256, "payload_sha256")
        if type(self.payload_bytes) is not int:
            raise TypeError("payload_bytes must be an exact built-in int")
        if not 1 <= self.payload_bytes <= _MAX_PAYLOAD_BYTES:
            raise ValueError("payload_bytes is outside the bounded payload range")
        if self.expected_revision_sha256 is not None:
            _require_sha256(self.expected_revision_sha256, "expected_revision_sha256")

    @classmethod
    def bind_payload(
        cls,
        *,
        intent_id: str,
        workspace_id: str,
        agent_id: str,
        target: LearningUpdateTarget,
        target_ref_sha256: str,
        candidate_sha256: str,
        verification_sha256: str,
        update_schema: str,
        payload: bytes,
        expected_revision_sha256: str | None = None,
    ) -> LearningUpdateIntent:
        """Bind transient canonical payload bytes without retaining those bytes."""

        raw = _require_payload(payload)
        return cls(
            intent_id=intent_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            target=target,
            target_ref_sha256=target_ref_sha256,
            candidate_sha256=candidate_sha256,
            verification_sha256=verification_sha256,
            update_schema=update_schema,
            payload_sha256=hashlib.sha256(raw).hexdigest(),
            payload_bytes=len(raw),
            expected_revision_sha256=expected_revision_sha256,
        )

    def assert_payload_matches(self, payload: bytes) -> None:
        """Fail closed if transient application bytes differ from the bound intent."""

        raw = _require_payload(payload)
        if len(raw) != self.payload_bytes:
            raise ValueError("payload length does not match bound learning update intent")
        if hashlib.sha256(raw).hexdigest() != self.payload_sha256:
            raise ValueError("payload digest does not match bound learning update intent")

    @property
    def intent_sha256(self) -> str:
        canonical = {
            "agent_id": self.agent_id,
            "candidate_sha256": self.candidate_sha256,
            "expected_revision_sha256": self.expected_revision_sha256,
            "intent_id": self.intent_id,
            "payload_bytes": self.payload_bytes,
            "payload_sha256": self.payload_sha256,
            "schema": "nika-loop-b-learning-update-intent:v1",
            "target": self.target.value,
            "target_ref_sha256": self.target_ref_sha256,
            "update_schema": self.update_schema,
            "verification_sha256": self.verification_sha256,
            "workspace_id": self.workspace_id,
        }
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def reportable_evidence(self) -> LearningUpdateEvidence:
        """Return evidence without raw scope identifiers or semantic payload bytes."""

        return LearningUpdateEvidence(
            intent_sha256=self.intent_sha256,
            scope_sha256=_scope_sha256(
                workspace_id=self.workspace_id,
                agent_id=self.agent_id,
            ),
            target=self.target,
            target_ref_sha256=self.target_ref_sha256,
            candidate_sha256=self.candidate_sha256,
            verification_sha256=self.verification_sha256,
            update_schema=self.update_schema,
            payload_sha256=self.payload_sha256,
            payload_bytes=self.payload_bytes,
            expected_revision_sha256=self.expected_revision_sha256,
        )
