from __future__ import annotations

import json
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit


@dataclass(frozen=True, slots=True)
class PaginationPolicy:
    max_pages: int = 50
    max_discovered_links_per_page: int = 8
    same_origin_only: bool = True
    json_next_fields: tuple[str, ...] = ("next", "next_url", "nextPage")

    def __post_init__(self) -> None:
        if self.max_pages < 1:
            raise ValueError("max_pages must be positive")
        if self.max_discovered_links_per_page < 1:
            raise ValueError("max_discovered_links_per_page must be positive")
        if not self.json_next_fields:
            raise ValueError("json_next_fields must not be empty")


@dataclass(frozen=True, slots=True)
class PaginationDiscovery:
    page_url: str
    next_urls: tuple[str, ...]


class _RelNextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() not in {"a", "link"}:
            return
        values = {name.lower(): value for name, value in attrs}
        rel = values.get("rel") or ""
        rel_tokens = {token.casefold() for token in rel.split()}
        href = values.get("href")
        if "next" in rel_tokens and href:
            self.hrefs.append(href)


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parts = urlsplit(url)
    scheme = parts.scheme.casefold()
    port = parts.port
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return (
        scheme,
        parts.hostname.casefold() if parts.hostname else None,
        port,
    )


def _same_origin(left: str, right: str) -> bool:
    return _origin(left) == _origin(right)


def _normalize_candidate(page_url: str, candidate: str) -> str | None:
    candidate = candidate.strip()
    if not candidate:
        return None
    absolute = urljoin(page_url, candidate)
    absolute, _fragment = urldefrag(absolute)
    parts = urlsplit(absolute)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        return None
    return absolute


def _bounded_unique(
    page_url: str,
    candidates: list[str],
    policy: PaginationPolicy,
) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = _normalize_candidate(page_url, candidate)
        if normalized is None or normalized == page_url or normalized in seen:
            continue
        if policy.same_origin_only and not _same_origin(page_url, normalized):
            continue
        seen.add(normalized)
        output.append(normalized)
        if len(output) >= policy.max_discovered_links_per_page:
            break
    return tuple(output)


def discover_html_pagination(
    page_url: str,
    html: str,
    *,
    policy: PaginationPolicy | None = None,
) -> PaginationDiscovery:
    effective_policy = policy or PaginationPolicy()
    parser = _RelNextParser()
    parser.feed(html)
    parser.close()
    return PaginationDiscovery(
        page_url=page_url,
        next_urls=_bounded_unique(page_url, parser.hrefs, effective_policy),
    )


def discover_json_pagination(
    page_url: str,
    payload: bytes | str,
    *,
    policy: PaginationPolicy | None = None,
) -> PaginationDiscovery:
    effective_policy = policy or PaginationPolicy()
    text = payload.decode("utf-8-sig", errors="strict") if isinstance(payload, bytes) else payload
    document = json.loads(text)
    candidates: list[str] = []
    if isinstance(document, dict):
        for field in effective_policy.json_next_fields:
            value = document.get(field)
            if isinstance(value, str):
                candidates.append(value)
    return PaginationDiscovery(
        page_url=page_url,
        next_urls=_bounded_unique(page_url, candidates, effective_policy),
    )


def extend_pagination_frontier(
    *,
    visited_urls: tuple[str, ...],
    queued_urls: tuple[str, ...],
    discovery: PaginationDiscovery,
    policy: PaginationPolicy | None = None,
) -> tuple[str, ...]:
    """Extend a deterministic breadth-first frontier without silently exceeding bounds."""
    effective_policy = policy or PaginationPolicy()
    seen = set(visited_urls) | set(queued_urls)
    remaining_slots = max(effective_policy.max_pages - len(seen), 0)
    if remaining_slots == 0:
        return queued_urls
    additions = tuple(url for url in discovery.next_urls if url not in seen)[:remaining_slots]
    return queued_urls + additions
