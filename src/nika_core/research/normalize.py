from __future__ import annotations

import re
import unicodedata

_HORIZONTAL_WHITESPACE = re.compile(r"[\t\f\v ]+")
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """Return stable Unicode text for hashing, deduplication and deterministic search."""
    normalized = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [_HORIZONTAL_WHITESPACE.sub(" ", line).strip() for line in normalized.split("\n")]
    normalized = "\n".join(lines)
    normalized = _EXCESS_BLANK_LINES.sub("\n\n", normalized)
    return normalized.strip()
