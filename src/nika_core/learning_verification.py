from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

_SCHEMA_VERSION = 1
_MAX_RECEIPT_BYTES = 1024 * 1024
_MAX_CHECKS = 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}\Z")
_ENVELOPE_KEYS = frozenset({"receipt", "receipt_sha256"})
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "candidate_material_sha256",
        "verification_policy_sha256",
        "required_check_ids",
        "checks",
    }
)
_CHECK_KEYS = frozenset(
    {
        "check_id",
        "checker_sha256",
        "evidence_sha256",
        "outcome",
    }
)


class LearningVerificationError(ValueError):
    """Base error for candidate-dataset verification evidence."""


class LearningVerificationValidationError(LearningVerificationError):
    """Raised when caller-supplied verification evidence violates the contract."""


class LearningVerificationIntegrityError(LearningVerificationError):
    """Raised when serialized verification evidence is malformed or tampered."""


class LearningVerificationRejectedError(LearningVerificationError):
    """Raised when a receipt does not prove every required check passed."""


class VerificationOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"


def _require_token(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise LearningVerificationValidationError(f"{field} must be a bounded machine token")
    return value


def _require_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise LearningVerificationValidationError(f"{field} must be lowercase SHA-256")
    return value


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest_payload(payload: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LearningVerificationIntegrityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _canonical_required_check_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(values))


def _canonical_checks(
    values: tuple[VerificationCheckEvidence, ...],
) -> tuple[VerificationCheckEvidence, ...]:
    return tuple(sorted(values, key=lambda item: item.check_id))


@dataclass(frozen=True, slots=True)
class VerificationCheckEvidence:
    check_id: str
    checker_sha256: str
    evidence_sha256: str
    outcome: VerificationOutcome

    def __post_init__(self) -> None:
        _require_token(self.check_id, field="check_id")
        _require_sha256(self.checker_sha256, field="checker_sha256")
        _require_sha256(self.evidence_sha256, field="evidence_sha256")
        if not isinstance(self.outcome, VerificationOutcome):
            raise LearningVerificationValidationError(
                "outcome must be a VerificationOutcome"
            )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "checker_sha256": self.checker_sha256,
            "evidence_sha256": self.evidence_sha256,
            "outcome": self.outcome.value,
        }

    @classmethod
    def from_payload(cls, payload: object) -> VerificationCheckEvidence:
        if not isinstance(payload, dict) or frozenset(payload) != _CHECK_KEYS:
            raise LearningVerificationIntegrityError("verification check keys are invalid")
        try:
            outcome = VerificationOutcome(payload.get("outcome"))
        except (TypeError, ValueError) as exc:
            raise LearningVerificationIntegrityError(
                "verification check outcome is invalid"
            ) from exc
        try:
            return cls(
                check_id=payload.get("check_id"),
                checker_sha256=payload.get("checker_sha256"),
                evidence_sha256=payload.get("evidence_sha256"),
                outcome=outcome,
            )
        except LearningVerificationValidationError as exc:
            raise LearningVerificationIntegrityError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class CandidateDatasetVerification:
    candidate_material_sha256: str
    verification_policy_sha256: str
    required_check_ids: tuple[str, ...]
    checks: tuple[VerificationCheckEvidence, ...]
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise LearningVerificationValidationError(
                "unsupported learning-verification schema"
            )
        _require_sha256(
            self.candidate_material_sha256,
            field="candidate_material_sha256",
        )
        _require_sha256(
            self.verification_policy_sha256,
            field="verification_policy_sha256",
        )
        if type(self.required_check_ids) is not tuple:
            raise LearningVerificationValidationError(
                "required_check_ids must be an immutable tuple"
            )
        if not 1 <= len(self.required_check_ids) <= _MAX_CHECKS:
            raise LearningVerificationValidationError(
                "required check count is outside the supported bound"
            )
        for check_id in self.required_check_ids:
            _require_token(check_id, field="required_check_id")
        if len(set(self.required_check_ids)) != len(self.required_check_ids):
            raise LearningVerificationValidationError(
                "required check identifiers must be unique"
            )
        if self.required_check_ids != _canonical_required_check_ids(
            self.required_check_ids
        ):
            raise LearningVerificationValidationError(
                "required check identifiers are not in canonical order"
            )

        if type(self.checks) is not tuple:
            raise LearningVerificationValidationError(
                "checks must be an immutable tuple"
            )
        if not 1 <= len(self.checks) <= _MAX_CHECKS:
            raise LearningVerificationValidationError(
                "verification check count is outside the supported bound"
            )
        if not all(type(item) is VerificationCheckEvidence for item in self.checks):
            raise LearningVerificationValidationError(
                "checks must contain exact VerificationCheckEvidence values"
            )
        if self.checks != _canonical_checks(self.checks):
            raise LearningVerificationValidationError(
                "verification checks are not in canonical order"
            )
        check_ids = tuple(item.check_id for item in self.checks)
        if len(set(check_ids)) != len(check_ids):
            raise LearningVerificationValidationError(
                "verification check identifiers must be unique"
            )
        if check_ids != self.required_check_ids:
            raise LearningVerificationValidationError(
                "verification checks must exactly match required_check_ids"
            )

    @classmethod
    def create(
        cls,
        *,
        candidate_material_sha256: str,
        verification_policy_sha256: str,
        required_check_ids: tuple[str, ...],
        checks: tuple[VerificationCheckEvidence, ...],
    ) -> CandidateDatasetVerification:
        if type(required_check_ids) is not tuple:
            raise LearningVerificationValidationError(
                "required_check_ids must be an immutable tuple"
            )
        if type(checks) is not tuple:
            raise LearningVerificationValidationError(
                "checks must be an immutable tuple"
            )
        return cls(
            candidate_material_sha256=candidate_material_sha256,
            verification_policy_sha256=verification_policy_sha256,
            required_check_ids=_canonical_required_check_ids(required_check_ids),
            checks=_canonical_checks(checks),
        )

    @property
    def is_verified(self) -> bool:
        return all(item.outcome is VerificationOutcome.PASS for item in self.checks)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "candidate_material_sha256": self.candidate_material_sha256,
            "checks": [item.canonical_payload() for item in self.checks],
            "required_check_ids": list(self.required_check_ids),
            "schema_version": self.schema_version,
            "verification_policy_sha256": self.verification_policy_sha256,
        }

    @property
    def receipt_sha256(self) -> str:
        return _digest_payload(self.canonical_payload())

    @property
    def verification_sha256(self) -> str:
        if not self.is_verified:
            raise LearningVerificationRejectedError(
                "candidate dataset verification did not pass every required check"
            )
        return self.receipt_sha256

    def to_json(self) -> str:
        payload = self.canonical_payload()
        envelope = {
            "receipt": payload,
            "receipt_sha256": _digest_payload(payload),
        }
        return _canonical_json_bytes(envelope).decode("utf-8")

    @classmethod
    def from_json(
        cls,
        raw: str | bytes,
        *,
        expected_receipt_sha256: str | None = None,
    ) -> CandidateDatasetVerification:
        if isinstance(raw, str):
            if not raw or len(raw) > _MAX_RECEIPT_BYTES:
                raise LearningVerificationIntegrityError(
                    "serialized receipt size is invalid"
                )
            decoded = raw
            try:
                encoded = raw.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise LearningVerificationIntegrityError(
                    "serialized receipt is not valid UTF-8"
                ) from exc
            if len(encoded) > _MAX_RECEIPT_BYTES:
                raise LearningVerificationIntegrityError(
                    "serialized receipt size is invalid"
                )
        elif isinstance(raw, bytes):
            if not raw or len(raw) > _MAX_RECEIPT_BYTES:
                raise LearningVerificationIntegrityError(
                    "serialized receipt size is invalid"
                )
            encoded = raw
            try:
                decoded = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise LearningVerificationIntegrityError(
                    "serialized receipt is not valid UTF-8"
                ) from exc
        else:
            raise LearningVerificationIntegrityError(
                "serialized receipt must be str or bytes"
            )
        try:
            parsed = json.loads(decoded, object_pairs_hook=_reject_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise LearningVerificationIntegrityError(
                "serialized receipt is not valid JSON"
            ) from exc
        if not isinstance(parsed, dict) or frozenset(parsed) != _ENVELOPE_KEYS:
            raise LearningVerificationIntegrityError(
                "learning-verification envelope keys are invalid"
            )

        declared_digest = parsed.get("receipt_sha256")
        if not isinstance(declared_digest, str) or not _SHA256_RE.fullmatch(
            declared_digest
        ):
            raise LearningVerificationIntegrityError(
                "learning-verification receipt digest format is invalid"
            )
        if expected_receipt_sha256 is not None:
            try:
                trusted_digest = _require_sha256(
                    expected_receipt_sha256,
                    field="expected_receipt_sha256",
                )
            except LearningVerificationValidationError as exc:
                raise LearningVerificationIntegrityError(str(exc)) from exc
            if declared_digest != trusted_digest:
                raise LearningVerificationIntegrityError(
                    "trusted learning-verification digest mismatch"
                )

        payload = parsed.get("receipt")
        if not isinstance(payload, dict) or frozenset(payload) != _RECEIPT_KEYS:
            raise LearningVerificationIntegrityError(
                "learning-verification receipt keys are invalid"
            )
        if _digest_payload(payload) != declared_digest:
            raise LearningVerificationIntegrityError(
                "learning-verification receipt digest mismatch"
            )

        required_ids_value = payload.get("required_check_ids")
        checks_value = payload.get("checks")
        if not isinstance(required_ids_value, list):
            raise LearningVerificationIntegrityError(
                "required_check_ids must be a list"
            )
        if not isinstance(checks_value, list):
            raise LearningVerificationIntegrityError("checks must be a list")
        try:
            receipt = cls(
                schema_version=payload.get("schema_version"),
                candidate_material_sha256=payload.get("candidate_material_sha256"),
                verification_policy_sha256=payload.get("verification_policy_sha256"),
                required_check_ids=tuple(required_ids_value),
                checks=tuple(
                    VerificationCheckEvidence.from_payload(item)
                    for item in checks_value
                ),
            )
        except LearningVerificationValidationError as exc:
            raise LearningVerificationIntegrityError(str(exc)) from exc

        if decoded != receipt.to_json():
            raise LearningVerificationIntegrityError(
                "learning-verification serialization is not canonical"
            )
        return receipt
