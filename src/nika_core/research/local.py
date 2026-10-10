from __future__ import annotations

import csv
import html
import io
import json
from html.parser import HTMLParser
from pathlib import Path

from nika_core.research.models import ExtractedDocument, ExtractionStatus


class LocalIngestionError(RuntimeError):
    pass


class UnsupportedLocalFormatError(LocalIngestionError):
    pass


class LocalPathPolicyError(LocalIngestionError):
    pass


class LocalFileTooLargeError(LocalIngestionError):
    pass


_TEXT_MEDIA_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".csv": "text/csv",
    ".json": "application/json",
}

_DOCUMENT_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_TEXT_MEDIA_TYPE_SET = frozenset(_TEXT_MEDIA_TYPES.values())
_DOCUMENT_MEDIA_TYPE_SET = frozenset(_DOCUMENT_MEDIA_TYPES.values())
_DEFAULT_MAX_LOGICAL_LINE_CHARS = 1_000_000


class _VisibleTextParser(HTMLParser):
    _BLOCKED = frozenset({"script", "style", "noscript", "template"})
    _BREAKS = frozenset(
        {
            "br",
            "p",
            "div",
            "li",
            "tr",
            "section",
            "article",
            "header",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._blocked_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCKED:
            self._blocked_depth += 1
        elif self._blocked_depth == 0 and tag in self._BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCKED and self._blocked_depth:
            self._blocked_depth -= 1
        elif self._blocked_depth == 0 and tag in self._BREAKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._blocked_depth == 0:
            self.parts.append(data)


def local_media_type(path: Path | str) -> str:
    suffix = Path(path).suffix.casefold()
    media_type = _TEXT_MEDIA_TYPES.get(suffix) or _DOCUMENT_MEDIA_TYPES.get(suffix)
    if media_type is None:
        raise UnsupportedLocalFormatError(f"unsupported local format: {suffix or '<none>'}")
    return media_type


def is_document_format(path: Path | str) -> bool:
    return Path(path).suffix.casefold() in _DOCUMENT_MEDIA_TYPES


def is_document_media_type(media_type: str) -> bool:
    return media_type.casefold() in _DOCUMENT_MEDIA_TYPE_SET


def resolve_local_file(
    path: Path | str,
    *,
    allowed_root: Path | str,
    max_bytes: int = 64 * 1024 * 1024,
) -> Path:
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    root = Path(allowed_root).resolve()
    candidate = Path(path).resolve()
    if not candidate.is_relative_to(root):
        raise LocalPathPolicyError("local source escapes the allowed root")
    if not candidate.is_file():
        raise LocalIngestionError("local source is not a regular file")
    size = candidate.stat().st_size
    if size > max_bytes:
        raise LocalFileTooLargeError(f"local source is {size} bytes; limit is {max_bytes}")
    return candidate


def resolve_local_folder(path: Path | str, *, allowed_root: Path | str) -> Path:
    root = Path(allowed_root).resolve()
    candidate = Path(path).resolve()
    if not candidate.is_relative_to(root):
        raise LocalPathPolicyError("local folder escapes the allowed root")
    if not candidate.is_dir():
        raise LocalIngestionError("local folder is not a directory")
    return candidate


def _decode_utf8(data: bytes, name: str) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise LocalIngestionError(f"{name}: expected UTF-8 text") from exc


def _ensure_bounded_logical_lines(
    text: str,
    *,
    name: str,
    max_logical_line_chars: int,
) -> None:
    if max_logical_line_chars < 1:
        raise ValueError("max_logical_line_chars must be positive")
    line_chars = 0
    for character in text:
        if character in "\r\n":
            line_chars = 0
            continue
        line_chars += 1
        if line_chars > max_logical_line_chars:
            raise LocalIngestionError(
                f"{name}: logical line exceeds {max_logical_line_chars} characters"
            )


def _extract_json(text: str) -> str:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LocalIngestionError(f"malformed JSON: {exc.msg}") from exc
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _extract_csv(text: str) -> str:
    rendered = io.StringIO()
    try:
        for index, row in enumerate(csv.reader(io.StringIO(text), strict=True)):
            if index:
                rendered.write("\n")
            rendered.write("\t".join(cell.strip() for cell in row))
    except csv.Error as exc:
        raise LocalIngestionError(f"malformed CSV: {exc}") from exc
    return rendered.getvalue()


def _extract_html(text: str) -> str:
    parser = _VisibleTextParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        raise LocalIngestionError("malformed HTML") from exc
    return html.unescape("".join(parser.parts))


def extract_text_payload(
    payload: bytes,
    *,
    title: str,
    media_type: str,
    max_logical_line_chars: int = _DEFAULT_MAX_LOGICAL_LINE_CHARS,
) -> ExtractedDocument:
    normalized_media_type = media_type.casefold()
    if normalized_media_type not in _TEXT_MEDIA_TYPE_SET:
        raise UnsupportedLocalFormatError(f"unsupported text media type: {media_type}")
    text = _decode_utf8(payload, title)
    _ensure_bounded_logical_lines(
        text,
        name=title,
        max_logical_line_chars=max_logical_line_chars,
    )
    if not text:
        status = ExtractionStatus.EMPTY
    else:
        if normalized_media_type == "text/html":
            text = _extract_html(text)
        elif normalized_media_type == "text/csv":
            text = _extract_csv(text)
        elif normalized_media_type == "application/json":
            text = _extract_json(text)
        status = ExtractionStatus.EXTRACTED if text.strip() else ExtractionStatus.EMPTY
    return ExtractedDocument(
        title=title,
        text=text,
        media_type=normalized_media_type,
        status=status,
        extractor="nika-stdlib",
        extractor_version="1",
    )


def extract_local_file(
    path: Path | str,
    *,
    allowed_root: Path | str,
    max_bytes: int = 16 * 1024 * 1024,
    max_logical_line_chars: int = _DEFAULT_MAX_LOGICAL_LINE_CHARS,
) -> ExtractedDocument:
    candidate = resolve_local_file(path, allowed_root=allowed_root, max_bytes=max_bytes)
    suffix = candidate.suffix.casefold()
    media_type = _TEXT_MEDIA_TYPES.get(suffix)
    if media_type is None:
        raise UnsupportedLocalFormatError(f"unsupported text format: {suffix or '<none>'}")
    return extract_text_payload(
        candidate.read_bytes(),
        title=candidate.name,
        media_type=media_type,
        max_logical_line_chars=max_logical_line_chars,
    )
