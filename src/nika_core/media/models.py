from __future__ import annotations

from nika_core.media.contracts import OCRPage

# Canonical public name requested by the media domain. OCRPage remains the compatibility
# spelling inside the first Batch A contract file; both names refer to the same immutable
# Nika-owned Pydantic contract and no upstream OCR type crosses this boundary.
Page = OCRPage

__all__ = ["Page"]
