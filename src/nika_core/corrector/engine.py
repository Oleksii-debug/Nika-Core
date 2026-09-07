from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from nika_core.corrector.contracts import (
    CorrectionEvidence,
    CorrectionProfile,
    CorrectionResult,
    CorrectorError,
    NormalizationForm,
    ProtectedTerm,
    ReplacementRule,
    RuleChange,
    sha256_text,
)


def _normalize_text(text: str, profile: CorrectionProfile) -> str:
    if not isinstance(text, str):
        raise CorrectorError("text must be a string")
    result = text
    if profile.options.normalize_newlines:
        result = result.replace("\r\n", "\n").replace("\r", "\n")
    if profile.options.normalization is not NormalizationForm.NONE:
        result = unicodedata.normalize(profile.options.normalization.value, result)
    return result


def _compile_literal_pattern(
    text: str,
    *,
    case_sensitive: bool,
    whole_word: bool = False,
) -> re.Pattern[str]:
    escaped = re.escape(text)
    if whole_word:
        escaped = rf"(?<!\w){escaped}(?!\w)"
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(escaped, flags)


def _protected_ranges(
    text: str,
    terms: Iterable[ProtectedTerm],
    profile: CorrectionProfile,
) -> tuple[tuple[tuple[int, int], ...], int]:
    ranges: list[tuple[int, int]] = []
    occurrence_count = 0
    for term in terms:
        protected_text = _normalize_text(term.text, profile)
        pattern = _compile_literal_pattern(
            protected_text, case_sensitive=term.case_sensitive
        )
        matches = tuple(pattern.finditer(text))
        occurrence_count += len(matches)
        ranges.extend((match.start(), match.end()) for match in matches)
    if not ranges:
        return (), occurrence_count
    ranges.sort()
    merged: list[tuple[int, int]] = [ranges[0]]
    for start, end in ranges[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return tuple(merged), occurrence_count


def _intersects(start: int, end: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(
        start < protected_end and end > protected_start
        for protected_start, protected_end in ranges
    )


def _apply_rule(
    text: str,
    rule: ReplacementRule,
    protected: tuple[ProtectedTerm, ...],
    profile: CorrectionProfile,
) -> tuple[str, int]:
    ranges, _ = _protected_ranges(text, protected, profile)
    needle = _normalize_text(rule.needle, profile)
    replacement = _normalize_text(rule.replacement, profile)
    pattern = _compile_literal_pattern(
        needle,
        case_sensitive=rule.case_sensitive,
        whole_word=rule.whole_word,
    )
    pieces: list[str] = []
    cursor = 0
    replacements = 0
    for match in pattern.finditer(text):
        if _intersects(match.start(), match.end(), ranges):
            continue
        pieces.append(text[cursor : match.start()])
        pieces.append(replacement)
        cursor = match.end()
        replacements += 1
    if replacements == 0:
        return text, 0
    pieces.append(text[cursor:])
    return "".join(pieces), replacements


def _apply_once(
    text: str,
    profile: CorrectionProfile,
) -> tuple[str, dict[str, int], bool, int]:
    normalized = _normalize_text(text, profile)
    normalized_changed = normalized != text
    _, protected_occurrences = _protected_ranges(
        normalized, profile.protected_terms, profile
    )
    current = normalized
    counts: dict[str, int] = {}
    for rule in sorted(profile.rules, key=lambda item: (item.priority, item.rule_id)):
        current, count = _apply_rule(current, rule, profile.protected_terms, profile)
        counts[rule.rule_id] = count
    return current, counts, normalized_changed, protected_occurrences


def correct_text(text: str, profile: CorrectionProfile) -> CorrectionResult:
    """Apply one deterministic correction pass and require stable second-pass output."""
    if not isinstance(profile, CorrectionProfile):
        raise CorrectorError("profile must be a CorrectionProfile")
    after, counts, normalized_changed, protected_occurrences = _apply_once(text, profile)
    second_after, _, _, _ = _apply_once(after, profile)
    if second_after != after:
        raise CorrectorError("correction profile is not idempotent for this input")
    evidence = CorrectionEvidence(
        profile_digest=profile.digest,
        input_digest=sha256_text(text),
        output_digest=sha256_text(after),
        normalized_changed=normalized_changed,
        protected_occurrences=protected_occurrences,
        rule_changes=tuple(
            RuleChange(rule_id=rule_id, replacements=count)
            for rule_id, count in sorted(counts.items())
        ),
    )
    return CorrectionResult(before_text=text, after_text=after, evidence=evidence)
