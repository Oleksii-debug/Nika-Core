from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChunkPolicy:
    max_chars: int = 4000
    overlap_chars: int = 200

    def __post_init__(self) -> None:
        if self.max_chars < 32:
            raise ValueError("max_chars must be at least 32")
        if self.overlap_chars < 0 or self.overlap_chars >= self.max_chars:
            raise ValueError("overlap_chars must be between 0 and max_chars - 1")


def chunk_text(text: str, *, policy: ChunkPolicy | None = None) -> tuple[str, ...]:
    active = policy or ChunkPolicy()
    if not text:
        return ()

    chunks: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + active.max_chars, length)
        if end < length:
            minimum_boundary = start + active.max_chars // 2
            newline = text.rfind("\n", minimum_boundary, end + 1)
            space = text.rfind(" ", minimum_boundary, end + 1)
            boundary = max(newline, space)
            if boundary > start:
                end = boundary

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= length:
            break

        next_start = max(end - active.overlap_chars, start + 1)
        while next_start < end and text[next_start].isspace():
            next_start += 1
        start = next_start

    return tuple(chunks)
