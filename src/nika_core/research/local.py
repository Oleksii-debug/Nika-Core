from __future__ import annotations

import csv
import html
import io
import json
from html.parser import HTMLParser
from pathlib import Path

from nika_core.research.models import ExtractedDocument


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


def _decode_utf8(data: bytes, path: Path) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise LocalIngestionError(f"{path.name}: expected UTF-8 text") from exc


def _extract_json(text: str) -> str:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LocalIngestionError(f"malformed JSON: {exc.msg}") from exc
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _extract_csv(text: str) -> str:
    try:
        rows = list(csv.reader(io.StringIO(text), strict=True))
    except csv.Error as exc:
        raise LocalIngestionError(f"malformed CSV: {exc}") from exc
    return "\n".join("\t".join(cell.strip() for cell in row) for row in rows)


def _extract_html(text: str) -> str:
    parser = _VisibleTextParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        raise LocalIngestionError("malformed HTML") from exc
    return html.unescape("".join(parser.parts))


def extract_local_file(
    path: Path | str,
    *,
    allowed_root: Path | str,
    max_bytes: int = 16 * 1024 * 1024,
) -> ExtractedDocument:
    candidate = resolve_local_file(path, allowed_root=allowed_root, max_bytes=max_bytes)
    suffix = candidate.suffix.casefold()
    media_type = _TEXT_MEDIA_TYPES.get(suffix)
    if media_type is None:
        raise UnsupportedLocalFormatError(f"unsupported text format: {suffix or '<none>'}")

    text = _decode_utf8(candidate.read_bytes(), candidate)
    if suffix in {".html", ".htm"}:
        text = _extract_html(text)
    elif suffix == ".csv":
        text = _extract_csv(text)
    elif suffix == ".json":
        text = _extract_json(text)

    return ExtractedDocument(
        title=candidate.name,
        text=text,
        media_type=media_type,
        extractor="nika-stdlib",
        extractor_version="1",
    )
