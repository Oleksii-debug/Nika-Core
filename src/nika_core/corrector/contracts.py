from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


MAX_IDENTIFIER_LENGTH = 128
MAX_PATTERN_LENGTH = 4096
MAX_REPLACEMENT_LENGTH = 16384
PROFILE_SCHEMA = "nika.corrector.profile.v1"
EVIDENCE_SCHEMA = "nika.corrector.evidence.v1"


class CorrectorError(ValueError):
    """Base error for fail-closed Corrector contracts."""


class CorrectorConflict(CorrectorError):
    """Raised when an idempotency key or optimistic revision conflicts."""


class CorrectorIntegrityError(CorrectorError):
    """Raised when durable or portable Corrector state fails validation."""


class NormalizationForm(str, Enum):
    NONE = "none"
    NFC = "NFC"
    NFKC = "NFKC"


def _validate_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise CorrectorError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise CorrectorError(f"{field_name} must not be empty")
    if len(cleaned) > MAX_IDENTIFIER_LENGTH:
        raise CorrectorError(f"{field_name} is too long")
    if any(ord(char) < 32 for char in cleaned):
        raise CorrectorError(f"{field_name} must not contain control characters")
    return cleaned


def sha256_text(text: str) -> str:
    if not isinstance(text, str):
        raise CorrectorError("text must be a string")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class CorrectionOptions:
    normalization: NormalizationForm = NormalizationForm.NFC
    normalize_newlines: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.normalization, NormalizationForm):
            raise CorrectorError("normalization must be a NormalizationForm")
        if type(self.normalize_newlines) is not bool:
            raise CorrectorError("normalize_newlines must be a bool")


@dataclass(frozen=True, slots=True)
class ReplacementRule:
    rule_id: str
    needle: str = field(repr=False)
    replacement: str = field(repr=False)
    case_sensitive: bool = True
    whole_word: bool = False
    priority: int = 100

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_id", _validate_identifier(self.rule_id, "rule_id"))
        if not isinstance(self.needle, str) or not self.needle:
            raise CorrectorError("needle must not be empty")
        if len(self.needle) > MAX_PATTERN_LENGTH:
            raise CorrectorError("needle is too long")
        if not isinstance(self.replacement, str):
            raise CorrectorError("replacement must be a string")
        if len(self.replacement) > MAX_REPLACEMENT_LENGTH:
            raise CorrectorError("replacement is too long")
        if type(self.case_sensitive) is not bool or type(self.whole_word) is not bool:
            raise CorrectorError("rule flags must be bool values")
        if type(self.priority) is not int:
            raise CorrectorError("priority must be an integer")
        if not -1_000_000 <= self.priority <= 1_000_000:
            raise CorrectorError("priority is outside the supported range")
        if self.needle == self.replacement:
            raise CorrectorError("replacement rule must change text")


@dataclass(frozen=True, slots=True)
class ProtectedTerm:
    term_id: str
    text: str = field(repr=False)
    case_sensitive: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "term_id", _validate_identifier(self.term_id, "term_id"))
        if not isinstance(self.text, str) or not self.text:
            raise CorrectorError("protected text must not be empty")
        if len(self.text) > MAX_PATTERN_LENGTH:
            raise CorrectorError("protected text is too long")
        if type(self.case_sensitive) is not bool:
            raise CorrectorError("case_sensitive must be a bool")


@dataclass(frozen=True, slots=True)
class CorrectionProfile:
    profile_id: str
    rules: tuple[ReplacementRule, ...] = ()
    protected_terms: tuple[ProtectedTerm, ...] = ()
    options: CorrectionOptions = field(default_factory=CorrectionOptions)

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _validate_identifier(self.profile_id, "profile_id"))
        rules = tuple(self.rules)
        protected = tuple(self.protected_terms)
        if any(not isinstance(rule, ReplacementRule) for rule in rules):
            raise CorrectorError("rules must contain ReplacementRule values")
        if any(not isinstance(term, ProtectedTerm) for term in protected):
            raise CorrectorError("protected_terms must contain ProtectedTerm values")
        if not isinstance(self.options, CorrectionOptions):
            raise CorrectorError("options must be CorrectionOptions")
        rule_ids = [rule.rule_id for rule in rules]
        term_ids = [term.term_id for term in protected]
        if len(rule_ids) != len(set(rule_ids)):
            raise CorrectorError("rule_id values must be unique")
        if len(term_ids) != len(set(term_ids)):
            raise CorrectorError("term_id values must be unique")
        object.__setattr__(self, "rules", rules)
        object.__setattr__(self, "protected_terms", protected)

    def canonical_payload(self) -> dict[str, Any]:
        ordered_rules = sorted(self.rules, key=lambda item: (item.priority, item.rule_id))
        ordered_terms = sorted(self.protected_terms, key=lambda item: item.term_id)
        return {
            "schema": PROFILE_SCHEMA,
            "profile_id": self.profile_id,
            "options": {
                "normalization": self.options.normalization.value,
                "normalize_newlines": self.options.normalize_newlines,
            },
            "rules": [
                {
                    "rule_id": rule.rule_id,
                    "needle": rule.needle,
                    "replacement": rule.replacement,
                    "case_sensitive": rule.case_sensitive,
                    "whole_word": rule.whole_word,
                    "priority": rule.priority,
                }
                for rule in ordered_rules
            ],
            "protected_terms": [
                {
                    "term_id": term.term_id,
                    "text": term.text,
                    "case_sensitive": term.case_sensitive,
                }
                for term in ordered_terms
            ],
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.canonical_payload())

    @property
    def digest(self) -> str:
        return sha256_text(self.canonical_json())

    @classmethod
    def from_canonical_json(cls, payload: str) -> CorrectionProfile:
        try:
            raw = json.loads(payload)
        except (json.JSONDecodeError, TypeError) as exc:
            raise CorrectorIntegrityError("invalid profile JSON") from exc
        if type(raw) is not dict or set(raw) != {
            "schema",
            "profile_id",
            "options",
            "rules",
            "protected_terms",
        }:
            raise CorrectorIntegrityError("invalid profile schema fields")
        if raw["schema"] != PROFILE_SCHEMA:
            raise CorrectorIntegrityError("unsupported profile schema")
        options_raw = raw["options"]
        if type(options_raw) is not dict or set(options_raw) != {
            "normalization",
            "normalize_newlines",
        }:
            raise CorrectorIntegrityError("invalid profile options")
        if type(options_raw["normalize_newlines"]) is not bool:
            raise CorrectorIntegrityError("invalid newline normalization flag")
        try:
            options = CorrectionOptions(
                normalization=NormalizationForm(options_raw["normalization"]),
                normalize_newlines=options_raw["normalize_newlines"],
            )
        except (ValueError, TypeError, CorrectorError) as exc:
            raise CorrectorIntegrityError("invalid profile options") from exc

        rules_raw = raw["rules"]
        terms_raw = raw["protected_terms"]
        if type(rules_raw) is not list or type(terms_raw) is not list:
            raise CorrectorIntegrityError("invalid profile rule containers")
        rules: list[ReplacementRule] = []
        for item in rules_raw:
            if type(item) is not dict or set(item) != {
                "rule_id",
                "needle",
                "replacement",
                "case_sensitive",
                "whole_word",
                "priority",
            }:
                raise CorrectorIntegrityError("invalid replacement rule fields")
            if type(item["case_sensitive"]) is not bool or type(item["whole_word"]) is not bool:
                raise CorrectorIntegrityError("invalid replacement rule flags")
            if type(item["priority"]) is not int:
                raise CorrectorIntegrityError("invalid replacement rule priority")
            try:
                rules.append(ReplacementRule(**item))
            except (TypeError, CorrectorError) as exc:
                raise CorrectorIntegrityError("invalid replacement rule") from exc
        terms: list[ProtectedTerm] = []
        for item in terms_raw:
            if type(item) is not dict or set(item) != {"term_id", "text", "case_sensitive"}:
                raise CorrectorIntegrityError("invalid protected term fields")
            if type(item["case_sensitive"]) is not bool:
                raise CorrectorIntegrityError("invalid protected term flag")
            try:
                terms.append(ProtectedTerm(**item))
            except (TypeError, CorrectorError) as exc:
                raise CorrectorIntegrityError("invalid protected term") from exc
        try:
            profile = cls(
                profile_id=raw["profile_id"],
                rules=tuple(rules),
                protected_terms=tuple(terms),
                options=options,
            )
        except CorrectorError as exc:
            raise CorrectorIntegrityError("invalid profile contract") from exc
        if profile.canonical_json() != payload:
            raise CorrectorIntegrityError("profile JSON is not canonical")
        return profile


@dataclass(frozen=True, slots=True)
class RuleChange:
    rule_id: str
    replacements: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_id", _validate_identifier(self.rule_id, "rule_id"))
        if type(self.replacements) is not int or self.replacements < 0:
            raise CorrectorError("replacements must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class CorrectionEvidence:
    profile_digest: str
    input_digest: str
    output_digest: str
    normalized_changed: bool
    protected_occurrences: int
    rule_changes: tuple[RuleChange, ...]

    def __post_init__(self) -> None:
        for field_name in ("profile_digest", "input_digest", "output_digest"):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise CorrectorError(f"{field_name} must be a lowercase SHA-256 digest")
        if type(self.normalized_changed) is not bool:
            raise CorrectorError("normalized_changed must be a bool")
        if type(self.protected_occurrences) is not int or self.protected_occurrences < 0:
            raise CorrectorError("protected_occurrences must be a non-negative integer")
        changes = tuple(self.rule_changes)
        if any(not isinstance(change, RuleChange) for change in changes):
            raise CorrectorError("rule_changes must contain RuleChange values")
        ids = [change.rule_id for change in changes]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise CorrectorError("rule_changes must be uniquely sorted by rule_id")
        object.__setattr__(self, "rule_changes", changes)

    @property
    def total_replacements(self) -> int:
        return sum(change.replacements for change in self.rule_changes)

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema": EVIDENCE_SCHEMA,
            "profile_digest": self.profile_digest,
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
            "normalized_changed": self.normalized_changed,
            "protected_occurrences": self.protected_occurrences,
            "rule_changes": [
                {"rule_id": change.rule_id, "replacements": change.replacements}
                for change in self.rule_changes
            ],
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.canonical_payload())

    @classmethod
    def from_canonical_json(cls, payload: str) -> CorrectionEvidence:
        try:
            raw = json.loads(payload)
        except (json.JSONDecodeError, TypeError) as exc:
            raise CorrectorIntegrityError("invalid evidence JSON") from exc
        expected = {
            "schema",
            "profile_digest",
            "input_digest",
            "output_digest",
            "normalized_changed",
            "protected_occurrences",
            "rule_changes",
        }
        if type(raw) is not dict or set(raw) != expected or raw["schema"] != EVIDENCE_SCHEMA:
            raise CorrectorIntegrityError("invalid evidence schema")
        if type(raw["normalized_changed"]) is not bool:
            raise CorrectorIntegrityError("invalid evidence normalization flag")
        if type(raw["protected_occurrences"]) is not int:
            raise CorrectorIntegrityError("invalid protected occurrence count")
        changes_raw = raw["rule_changes"]
        if type(changes_raw) is not list:
            raise CorrectorIntegrityError("invalid rule_changes container")
        changes: list[RuleChange] = []
        for item in changes_raw:
            if type(item) is not dict or set(item) != {"rule_id", "replacements"}:
                raise CorrectorIntegrityError("invalid rule change fields")
            if type(item["replacements"]) is not int:
                raise CorrectorIntegrityError("invalid replacement count")
            try:
                changes.append(RuleChange(**item))
            except (TypeError, CorrectorError) as exc:
                raise CorrectorIntegrityError("invalid rule change") from exc
        try:
            evidence = cls(
                profile_digest=raw["profile_digest"],
                input_digest=raw["input_digest"],
                output_digest=raw["output_digest"],
                normalized_changed=raw["normalized_changed"],
                protected_occurrences=raw["protected_occurrences"],
                rule_changes=tuple(changes),
            )
        except CorrectorError as exc:
            raise CorrectorIntegrityError("invalid evidence contract") from exc
        if evidence.canonical_json() != payload:
            raise CorrectorIntegrityError("evidence JSON is not canonical")
        return evidence


@dataclass(frozen=True, slots=True)
class CorrectionResult:
    before_text: str = field(repr=False)
    after_text: str = field(repr=False)
    evidence: CorrectionEvidence = field(repr=True)

    def __post_init__(self) -> None:
        if not isinstance(self.before_text, str) or not isinstance(self.after_text, str):
            raise CorrectorError("correction result text must be strings")
        if not isinstance(self.evidence, CorrectionEvidence):
            raise CorrectorError("evidence must be CorrectionEvidence")
        if sha256_text(self.before_text) != self.evidence.input_digest:
            raise CorrectorIntegrityError("before_text does not match evidence")
        if sha256_text(self.after_text) != self.evidence.output_digest:
            raise CorrectorIntegrityError("after_text does not match evidence")


@dataclass(frozen=True, slots=True)
class SessionRevision:
    session_id: str
    revision: int
    text: str = field(repr=False)
    text_digest: str
    parent_digest: str | None
    profile: CorrectionProfile = field(repr=False)
    evidence: CorrectionEvidence
    operation_id: str
    created_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _validate_identifier(self.session_id, "session_id"))
        object.__setattr__(
            self, "operation_id", _validate_identifier(self.operation_id, "operation_id")
        )
        if type(self.revision) is not int or self.revision < 0:
            raise CorrectorIntegrityError("revision must be a non-negative integer")
        if not isinstance(self.text, str) or sha256_text(self.text) != self.text_digest:
            raise CorrectorIntegrityError("revision text digest mismatch")
        if self.parent_digest is not None and (
            len(self.parent_digest) != 64
            or any(char not in "0123456789abcdef" for char in self.parent_digest)
        ):
            raise CorrectorIntegrityError("invalid parent digest")
        if not isinstance(self.profile, CorrectionProfile):
            raise CorrectorIntegrityError("revision profile is invalid")
        if not isinstance(self.evidence, CorrectionEvidence):
            raise CorrectorIntegrityError("revision evidence is invalid")
        if self.evidence.output_digest != self.text_digest:
            raise CorrectorIntegrityError("revision evidence does not bind output")
        if self.evidence.profile_digest != self.profile.digest:
            raise CorrectorIntegrityError("revision evidence does not bind profile")
        if not isinstance(self.created_at, str) or not self.created_at:
            raise CorrectorIntegrityError("created_at must not be empty")


@dataclass(frozen=True, slots=True)
class CorrectionSession:
    session_id: str
    current_revision: int
    text: str = field(repr=False)
    text_digest: str
    profile: CorrectionProfile = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _validate_identifier(self.session_id, "session_id"))
        if type(self.current_revision) is not int or self.current_revision < 0:
            raise CorrectorIntegrityError("current_revision must be a non-negative integer")
        if not isinstance(self.text, str) or sha256_text(self.text) != self.text_digest:
            raise CorrectorIntegrityError("session text digest mismatch")
        if not isinstance(self.profile, CorrectionProfile):
            raise CorrectorIntegrityError("session profile is invalid")
