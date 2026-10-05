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


@dataclass(frozen=True, slots=True)
class TextChunk:
    text: str
    start_char: int
    end_char: int

    def __post_init__(self) -> None:
        if self.start_char < 0 or self.end_char < self.start_char:
            raise ValueError("invalid chunk boundaries")
        if self.end_char - self.start_char != len(self.text):
            raise ValueError("chunk boundaries must describe the exact chunk text")


def chunk_text_spans(text: str, *, policy: ChunkPolicy | None = None) -> tuple[TextChunk, ...]:
    active = policy or ChunkPolicy()
    if not text:
        return ()

    chunks: list[TextChunk] = []
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

        raw_chunk = text[start:end]
        leading = len(raw_chunk) - len(raw_chunk.lstrip())
        trailing = len(raw_chunk) - len(raw_chunk.rstrip())
        chunk_start = start + leading
        chunk_end = end - trailing
        if chunk_start < chunk_end:
            chunk = text[chunk_start:chunk_end]
            chunks.append(TextChunk(text=chunk, start_char=chunk_start, end_char=chunk_end))
        if end >= length:
            break

        next_start = max(end - active.overlap_chars, start + 1)
        while next_start < end and text[next_start].isspace():
            next_start += 1
        start = next_start

    return tuple(chunks)


def chunk_text(text: str, *, policy: ChunkPolicy | None = None) -> tuple[str, ...]:
    return tuple(chunk.text for chunk in chunk_text_spans(text, policy=policy))
