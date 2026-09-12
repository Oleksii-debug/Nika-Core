from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

MAX_TRANSCRIPT_CHARS = 4096
MAX_WAKE_PHRASES = 16
MAX_WAKE_PHRASE_CHARS = 64
MAX_REQUEST_ID_CHARS = 128
_SAFE_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_TOKEN_RE = re.compile(r"[^\W_]+", flags=re.UNICODE)


class WakeActivationError(ValueError):
    pass


class WakeActivationOutcome(StrEnum):
    DETECTED = "detected"
    NOT_DETECTED = "not_detected"


@dataclass(frozen=True, slots=True)
class WakeActivationPolicy:
    phrases: tuple[str, ...] = ("ніка", "nika")

    def __post_init__(self) -> None:
        if type(self.phrases) is not tuple:
            raise WakeActivationError("wake phrases must be an immutable tuple")
        if not self.phrases or len(self.phrases) > MAX_WAKE_PHRASES:
            raise WakeActivationError(
                f"wake phrases must contain 1..{MAX_WAKE_PHRASES} entries"
            )

        normalized: set[tuple[str, ...]] = set()
        for phrase in self.phrases:
            tokens = _phrase_tokens(phrase)
            if tokens in normalized:
                raise WakeActivationError("wake phrases must be unique after normalization")
            normalized.add(tokens)


@dataclass(frozen=True, slots=True)
class WakeActivationEvidence:
    request_id: str
    outcome: WakeActivationOutcome
    transcript_sha256: str
    token_count: int
    matched_phrase_sha256: str | None
    match_start_token: int | None
    match_end_token_exclusive: int | None


class WakeActivationDetector:
    def __init__(self, policy: WakeActivationPolicy | None = None) -> None:
        self._policy = policy or WakeActivationPolicy()
        if not isinstance(self._policy, WakeActivationPolicy):
            raise WakeActivationError("policy must be a WakeActivationPolicy")
        self._phrases = tuple(
            (_phrase_tokens(phrase), _sha256_text(_normalized_text(phrase)))
            for phrase in self._policy.phrases
        )

    @property
    def policy(self) -> WakeActivationPolicy:
        return self._policy

    def detect(self, *, request_id: str, transcript: str) -> WakeActivationEvidence:
        _validate_request_id(request_id)
        normalized = _validated_transcript(transcript)
        tokens = tuple(_TOKEN_RE.findall(normalized))
        transcript_sha256 = _sha256_text(transcript)

        for start in range(len(tokens)):
            for phrase_tokens, phrase_sha256 in self._phrases:
                end = start + len(phrase_tokens)
                if tokens[start:end] == phrase_tokens:
                    return WakeActivationEvidence(
                        request_id=request_id,
                        outcome=WakeActivationOutcome.DETECTED,
                        transcript_sha256=transcript_sha256,
                        token_count=len(tokens),
                        matched_phrase_sha256=phrase_sha256,
                        match_start_token=start,
                        match_end_token_exclusive=end,
                    )

        return WakeActivationEvidence(
            request_id=request_id,
            outcome=WakeActivationOutcome.NOT_DETECTED,
            transcript_sha256=transcript_sha256,
            token_count=len(tokens),
            matched_phrase_sha256=None,
            match_start_token=None,
            match_end_token_exclusive=None,
        )


def _phrase_tokens(phrase: object) -> tuple[str, ...]:
    if type(phrase) is not str:
        raise WakeActivationError("each wake phrase must be a string")
    if not phrase or len(phrase) > MAX_WAKE_PHRASE_CHARS:
        raise WakeActivationError(
            f"each wake phrase must contain 1..{MAX_WAKE_PHRASE_CHARS} characters"
        )
    _reject_control_characters(phrase, field="wake phrase")
    tokens = tuple(_TOKEN_RE.findall(_normalized_text(phrase)))
    if not tokens:
        raise WakeActivationError("wake phrase must contain at least one word token")
    return tokens


def _validated_transcript(value: object) -> str:
    if type(value) is not str:
        raise WakeActivationError("transcript must be a string")
    if len(value) > MAX_TRANSCRIPT_CHARS:
        raise WakeActivationError(
            f"transcript must contain at most {MAX_TRANSCRIPT_CHARS} characters"
        )
    _reject_control_characters(value, field="transcript")
    return _normalized_text(value)


def _validate_request_id(value: object) -> None:
    if (
        type(value) is not str
        or len(value) > MAX_REQUEST_ID_CHARS
        or not _SAFE_REQUEST_ID_RE.fullmatch(value)
    ):
        raise WakeActivationError("request_id must be a bounded safe identifier")


def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _reject_control_characters(value: str, *, field: str) -> None:
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise WakeActivationError(f"{field} must not contain control characters")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
